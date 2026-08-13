"""Bot-token encryption, wired through the same master-password vault that
protects the Telegram session and api_hash (security.py) — no separate
crypto implementation. Gating matches api_hash: encrypted at rest only
while protection is on, plaintext otherwise."""
from __future__ import annotations

import logging

from ..db.database import Database
from ..security import SecurityService

_logger = logging.getLogger("chatgrab")


def register_bot_token_rotation(db: Database, security: SecurityService) -> None:
    """Keep stored bot tokens valid across the vault's password lifecycle:
    turning protection on/off, changing the password, or giving up on a
    forgotten one all change (or remove) the key bot tokens were encrypted
    under. Without this, a bot_api bot's token would silently become
    undecryptable the next time the vault's password changed."""

    def _on_rotate(old_password, old_salt_b64, old_iterations, new_password, new_salt_b64, new_iterations) -> None:
        for bot in db.list_bots():
            if bot["type"] != "bot_api" or not bot["token_encrypted"]:
                continue
            stored = bot["token_encrypted"]
            try:
                if old_password and old_salt_b64:
                    plain = SecurityService.decrypt_with(stored, old_password, old_salt_b64, old_iterations)
                else:
                    plain = stored  # was plaintext before this rotation
            except Exception:
                # Ciphertext under a key we no longer have (e.g. the
                # password was forgotten) — nothing to migrate; the token
                # will need re-entering, same as the Telegram session does.
                _logger.warning("bot %s token unrecoverable during key rotation", bot["id"])
                continue
            if new_password and new_salt_b64:
                new_stored = SecurityService.encrypt_with(plain, new_password, new_salt_b64, new_iterations)
            else:
                new_stored = plain
            db.set_bot_field(bot["id"], token_encrypted=new_stored)

    security.add_rotation_listener(_on_rotate)


def encrypt_token(security: SecurityService, plaintext: str) -> str:
    return security.encrypt_secret(plaintext)


def decrypt_token(security: SecurityService, stored: str) -> str:
    return security.decrypt_secret(stored)
