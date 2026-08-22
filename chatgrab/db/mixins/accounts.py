"""Telegram accounts (multi-account collection, see telegram/accounts.py)
— which chats/bots belong to which login, and which one is «основной»."""
from __future__ import annotations

import sqlite3
from typing import Any

from ..timeutil import now_iso


class AccountsMixin:
    def add_account(self, name: str, session_file: str, phone: str | None = None,
                    make_default: bool = False) -> int:
        cur = self.execute(
            "INSERT INTO account(name, phone, session_file, enabled, is_default, created_at) "
            "VALUES (?, ?, ?, 1, 0, ?)",
            (name, phone, session_file, now_iso()),
        )
        account_id = cur.lastrowid
        if make_default or not self.query_one("SELECT count(*) AS c FROM account WHERE is_default = 1")["c"]:
            self.set_default_account(account_id)
        return account_id

    def list_accounts(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM account ORDER BY is_default DESC, id")

    def get_account(self, account_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM account WHERE id = ?", (account_id,))

    def default_account(self) -> sqlite3.Row | None:
        """The flagged one if there is one, otherwise the oldest — never
        None while any account exists, so callers have one branch fewer."""
        return self.query_one(
            "SELECT * FROM account ORDER BY is_default DESC, id LIMIT 1")

    def set_default_account(self, account_id: int) -> None:
        # One flag, flipped in a single statement pair — a half-applied
        # change here would leave the app with no account to fall back on.
        self.execute("UPDATE account SET is_default = 0")
        self.execute("UPDATE account SET is_default = 1 WHERE id = ?", (account_id,))

    def set_account_field(self, account_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE account SET {cols} WHERE id = ?",
                     (*fields.values(), account_id))

    def delete_account(self, account_id: int) -> None:
        """Chats and bots that pointed at it fall back to «основной»
        rather than becoming unreachable rows nothing can collect."""
        self.execute("UPDATE chats SET account_id = NULL WHERE account_id = ?", (account_id,))
        self.execute("UPDATE bots SET account_id = NULL WHERE account_id = ?", (account_id,))
        was_default = bool((self.get_account(account_id) or {"is_default": 0})["is_default"])
        self.execute("DELETE FROM account WHERE id = ?", (account_id,))
        if was_default:
            row = self.query_one("SELECT id FROM account ORDER BY id LIMIT 1")
            if row:
                self.set_default_account(row["id"])

    def account_usage(self, account_id: int) -> dict:
        chats = self.query_one(
            "SELECT count(*) AS c FROM chats WHERE account_id = ?", (account_id,))["c"]
        bots = self.query_one(
            "SELECT count(*) AS c FROM bots WHERE account_id = ?", (account_id,))["c"]
        return {"chats": chats, "bots": bots}
