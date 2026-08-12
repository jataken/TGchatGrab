"""Master-password protection for the Telegram session file and
api_hash: encrypted at rest, decrypted only in memory (api_hash) or to a
transient plaintext file (the session, since Telethon needs a real
SQLite file path) for the duration the app is running and unlocked.

The password itself is never stored anywhere, in any form — not even a
hash of it. Correctness of a guess is verified implicitly: Fernet tokens
are authenticated, so decrypting api_hash with the wrong key simply
raises InvalidToken. There is no recovery if it's forgotten; the account
would need to be signed into again from scratch.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from pathlib import Path
from typing import Callable

from cryptography.fernet import Fernet, InvalidToken

from .config import AppConfig
from .paths import Paths

_logger = logging.getLogger("chatgrab")

# (old_password, old_kdf_salt_b64, new_password, new_kdf_salt_b64) -> None.
# A value is None on the "no vault" side of a transition: no old vault
# existed yet (protection just turned on) or no new one exists (turned
# off, or the password was forgotten and the vault is being wiped).
RotationListener = Callable[[str | None, str | None, str | None, str | None], None]

PBKDF2_ITERATIONS = 390_000


class WrongPasswordError(Exception):
    pass


def _derive_key(password: str, salt: bytes) -> bytes:
    raw = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS, dklen=32)
    return base64.urlsafe_b64encode(raw)


def _encrypt(data: bytes, password: str, salt: bytes) -> bytes:
    return Fernet(_derive_key(password, salt)).encrypt(data)


def _decrypt(token: bytes, password: str, salt: bytes) -> bytes:
    try:
        return Fernet(_derive_key(password, salt)).decrypt(token)
    except InvalidToken as e:
        raise WrongPasswordError("Неверный пароль.") from e


def _session_enc_path(session_path: Path) -> Path:
    return session_path.parent / (session_path.name + ".enc")


class SecurityService:
    def __init__(self, config: AppConfig, paths: Paths):
        self.config = config
        self.paths = paths
        self._password: str | None = None
        self._rotation_listeners: list[RotationListener] = []

    @property
    def enabled(self) -> bool:
        return self.config.master_password_enabled

    # ---- key rotation notifications -------------------------------------
    def add_rotation_listener(self, listener: RotationListener) -> None:
        """Register a callback invoked whenever the vault's password/salt
        changes (turned on, turned off, changed, or reset-forgotten) — lets
        other encrypted-secret stores (bot tokens) re-encrypt under the new
        key instead of silently going stale. Never raises out of a
        listener: one broken callback shouldn't block the others or the
        lifecycle operation that triggered it."""
        self._rotation_listeners.append(listener)

    def _notify_rotation(self, old_password: str | None, old_salt_b64: str | None,
                          new_password: str | None, new_salt_b64: str | None) -> None:
        for listener in self._rotation_listeners:
            try:
                listener(old_password, old_salt_b64, new_password, new_salt_b64)
            except Exception:
                _logger.warning("secret rotation listener failed", exc_info=True)

    # ---- secrets other than api_hash/session (e.g. bot tokens) ----------
    def encrypt_secret(self, plaintext: str) -> str:
        """Encrypt with the vault's current key. Returns the plaintext
        unchanged if protection isn't on — callers persist whichever comes
        back and don't need to branch on `enabled` themselves."""
        if not self.enabled or self._password is None:
            return plaintext
        return self.encrypt_with(plaintext, self._password, self.config.kdf_salt)

    def decrypt_secret(self, stored: str) -> str:
        if not self.enabled or self._password is None:
            return stored
        return self.decrypt_with(stored, self._password, self.config.kdf_salt)

    @staticmethod
    def encrypt_with(plaintext: str, password: str, salt_b64: str) -> str:
        salt = base64.b64decode(salt_b64)
        return base64.b64encode(_encrypt(plaintext.encode("utf-8"), password, salt)).decode("ascii")

    @staticmethod
    def decrypt_with(ciphertext_b64: str, password: str, salt_b64: str) -> str:
        salt = base64.b64decode(salt_b64)
        return _decrypt(base64.b64decode(ciphertext_b64), password, salt).decode("utf-8")

    # ---- lifecycle -----------------------------------------------------
    def enable(self, password: str) -> None:
        """Turn protection on: encrypt the current api_hash and session
        file, then wipe their plaintext. Requires being unlocked already
        (i.e. config.api_hash holds the real value) if re-enabling."""
        old_password, old_salt_b64 = self._password, (self.config.kdf_salt or None)
        salt = secrets.token_bytes(16)
        api_hash_enc = _encrypt(self.config.api_hash.encode("utf-8"), password, salt)

        session_path = Path(self.config.session_path)
        if session_path.exists():
            enc_path = _session_enc_path(session_path)
            enc_path.write_bytes(_encrypt(session_path.read_bytes(), password, salt))
            session_path.unlink()

        self.config.kdf_salt = base64.b64encode(salt).decode("ascii")
        self.config.api_hash_enc = base64.b64encode(api_hash_enc).decode("ascii")
        self.config.master_password_enabled = True
        self._password = password
        self.config.save(self.paths)
        self._notify_rotation(old_password, old_salt_b64, password, self.config.kdf_salt)

    def unlock(self, password: str) -> None:
        """Decrypt api_hash into memory and the session into a plaintext
        file Telethon can open directly. Raises WrongPasswordError on a
        bad guess — nothing is modified in that case."""
        salt = base64.b64decode(self.config.kdf_salt)
        api_hash = _decrypt(base64.b64decode(self.config.api_hash_enc), password, salt)

        session_path = Path(self.config.session_path)
        enc_path = _session_enc_path(session_path)
        if not session_path.exists() and enc_path.exists():
            session_path.parent.mkdir(parents=True, exist_ok=True)
            session_path.write_bytes(_decrypt(enc_path.read_bytes(), password, salt))
        # If a plaintext session already exists here, it's a leftover from
        # a run that didn't shut down cleanly (crash / force-kill) — keep
        # it as-is rather than overwrite it with a possibly older
        # encrypted copy; the next clean lock() re-encrypts its current
        # state and the vault heals itself.

        self.config.api_hash = api_hash.decode("utf-8")
        self._password = password

    def lock(self) -> None:
        """Re-encrypt the session file and remove the plaintext copy.
        Call on clean shutdown. A no-op if protection isn't on or the
        vault was never unlocked this run."""
        if not self.enabled or self._password is None:
            return
        session_path = Path(self.config.session_path)
        if session_path.exists():
            salt = base64.b64decode(self.config.kdf_salt)
            enc_path = _session_enc_path(session_path)
            enc_path.write_bytes(_encrypt(session_path.read_bytes(), self._password, salt))
            session_path.unlink()

    def disable(self, password: str) -> None:
        """Turn protection off, leaving api_hash/session as plaintext
        again (the pre-master-password default)."""
        self.unlock(password)  # raises WrongPasswordError on a bad guess
        old_salt_b64 = self.config.kdf_salt
        self.config.master_password_enabled = False
        self.config.kdf_salt = ""
        self.config.api_hash_enc = ""
        self._password = None
        self.config.save(self.paths)
        self._notify_rotation(password, old_salt_b64, None, None)

    def change_password(self, old_password: str, new_password: str) -> None:
        self.unlock(old_password)
        self.enable(new_password)

    def reset_forgotten(self) -> None:
        """Give up on recovering a forgotten password: discard the
        encrypted session and api_hash entirely rather than leave the
        app permanently locked out. The account will need signing into
        again and api_id/api_hash re-entering (still available anytime
        at my.telegram.org — nothing there is lost). Anything else that
        was encrypted under this vault (e.g. bot tokens) is equally
        unrecoverable — listeners are notified with no new key at all,
        rather than guess at silently discarding vs. keeping ciphertext
        that can never be opened again."""
        session_path = Path(self.config.session_path)
        enc_path = _session_enc_path(session_path)
        if enc_path.exists():
            enc_path.unlink()
        if session_path.exists():
            session_path.unlink()
        old_salt_b64 = self.config.kdf_salt or None
        self.config.master_password_enabled = False
        self.config.kdf_salt = ""
        self.config.api_hash_enc = ""
        self.config.api_hash = ""
        self._password = None
        self.config.save(self.paths)
        self._notify_rotation(None, old_salt_b64, None, None)
