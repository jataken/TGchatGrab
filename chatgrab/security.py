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
import secrets
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .config import AppConfig
from .paths import Paths

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

    @property
    def enabled(self) -> bool:
        return self.config.master_password_enabled

    # ---- lifecycle -----------------------------------------------------
    def enable(self, password: str) -> None:
        """Turn protection on: encrypt the current api_hash and session
        file, then wipe their plaintext. Requires being unlocked already
        (i.e. config.api_hash holds the real value) if re-enabling."""
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
        self.config.master_password_enabled = False
        self.config.kdf_salt = ""
        self.config.api_hash_enc = ""
        self._password = None
        self.config.save(self.paths)

    def change_password(self, old_password: str, new_password: str) -> None:
        self.unlock(old_password)
        self.enable(new_password)

    def reset_forgotten(self) -> None:
        """Give up on recovering a forgotten password: discard the
        encrypted session and api_hash entirely rather than leave the
        app permanently locked out. The account will need signing into
        again and api_id/api_hash re-entering (still available anytime
        at my.telegram.org — nothing there is lost)."""
        session_path = Path(self.config.session_path)
        enc_path = _session_enc_path(session_path)
        if enc_path.exists():
            enc_path.unlink()
        if session_path.exists():
            session_path.unlink()
        self.config.master_password_enabled = False
        self.config.kdf_salt = ""
        self.config.api_hash_enc = ""
        self.config.api_hash = ""
        self._password = None
        self.config.save(self.paths)
