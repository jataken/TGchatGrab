"""Mailbox password encryption + rotation across the master-password
vault's lifecycle.

Not security.py's register_setting_rotation() (Р3): that helper covers a
single app_settings key (Bitrix's webhook URL, the LLM API key). A
mailbox password lives on its own row of `mailbox`, one per mailbox, so
this needs the same per-row rotation shape bots/crypto.py's bot-token
rotation already uses, not Р3's shared single-key helper — see that
file's register_bot_token_rotation for the identical reasoning.
"""
from __future__ import annotations

import logging

from ...db.database import Database
from ...security import SecurityService

_logger = logging.getLogger("chatgrab")


def register_mailbox_rotation(db: Database, security: SecurityService) -> None:
    """Keep every stored mailbox password valid across the vault's
    password lifecycle — without this, a mailbox's password would
    silently become undecryptable the next time the master password's
    key changed, and the next sync tick would just look like a wrong
    password."""

    def _on_rotate(old_password, old_salt_b64, old_iterations,
                    new_password, new_salt_b64, new_iterations) -> None:
        for mailbox in db.list_mailboxes():
            stored = mailbox["password_enc"]
            if not stored:
                continue
            try:
                if old_password and old_salt_b64:
                    plain = SecurityService.decrypt_with(
                        stored, old_password, old_salt_b64, old_iterations)
                else:
                    plain = stored  # was plaintext before this rotation
            except Exception:
                _logger.warning("mailbox %s password unrecoverable during key rotation", mailbox["id"])
                continue
            if new_password and new_salt_b64:
                new_stored = SecurityService.encrypt_with(
                    plain, new_password, new_salt_b64, new_iterations)
            else:
                new_stored = plain
            db.set_mailbox_field(mailbox["id"], password_enc=new_stored)

    security.add_rotation_listener(_on_rotate)


def encrypt_password(security: SecurityService, plaintext: str) -> str:
    return security.encrypt_secret(plaintext)


def decrypt_password(security: SecurityService, stored: str) -> str:
    return security.decrypt_secret(stored)
