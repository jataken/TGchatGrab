"""П10: mail_contact — the address book. Two ways a row gets here:
upsert_mail_contact_from_message() (source='auto', called from
MailService as mail syncs — "собирается из истории переписки") and a
human adding or editing one by hand, or importing a CSV, from the
address-book screen (source='manual'). Both live in the same table and
the same list — there's no second, parallel "manual contacts" view.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from ..timeutil import now_iso


class MailContactsMixin:
    def upsert_mail_contact_from_message(self, address: str | None, display_name: str | None) -> None:
        """Called once per incoming message's sender (MailService, at
        sync time) — bumps message_count/last_seen_at on an existing
        row, or inserts a fresh source='auto' one. Never overwrites a
        display_name a human already set by hand (manual or auto — once
        someone's edited the name, a later message's From: header
        shouldn't silently revert it); a still-empty display_name does
        get filled in from whatever the message carries, since "no name
        yet" is exactly the gap this is meant to close."""
        address = (address or "").strip().lower()
        if not address or "@" not in address:
            return
        now = now_iso()
        existing = self.query_one("SELECT * FROM mail_contact WHERE address = ?", (address,))
        if existing is None:
            self.execute(
                "INSERT INTO mail_contact"
                "(address, display_name, source, message_count, last_seen_at, created_at, updated_at) "
                "VALUES (?, ?, 'auto', 1, ?, ?, ?)",
                (address, (display_name or "").strip() or None, now, now, now),
            )
            return
        name = existing["display_name"] or (display_name or "").strip() or None
        self.execute(
            "UPDATE mail_contact SET display_name = ?, message_count = message_count + 1, "
            "last_seen_at = ?, updated_at = ? WHERE id = ?",
            (name, now, now, existing["id"]),
        )

    def create_mail_contact(self, address: str, display_name: str | None = None,
                             group_name: str | None = None, source: str = "manual") -> int:
        address = address.strip().lower()
        now = now_iso()
        cur = self.execute(
            "INSERT INTO mail_contact(address, display_name, group_name, source, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(address) DO UPDATE SET display_name = excluded.display_name, "
            "group_name = excluded.group_name, source = excluded.source, updated_at = excluded.updated_at",
            (address, (display_name or "").strip() or None, (group_name or "").strip() or None,
             source, now, now),
        )
        # Same last_insert_rowid() caveat as upsert_mail_message — an
        # ON CONFLICT UPDATE doesn't reliably leave lastrowid pointing at
        # this row, so it's read back by the key that's actually unique.
        return self.get_mail_contact_by_address(address)["id"]

    def get_mail_contact(self, contact_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM mail_contact WHERE id = ?", (contact_id,))

    def get_mail_contact_by_address(self, address: str) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM mail_contact WHERE address = ?", (address.strip().lower(),))

    def list_mail_contacts(self, query: str | None = None, group_name: str | None = None,
                            limit: int = 500) -> list[sqlite3.Row]:
        sql = "SELECT * FROM mail_contact"
        clauses, params = [], []
        if query:
            clauses.append("(LOWER(address) LIKE ? OR LOWER(display_name) LIKE ?)")
            like = f"%{query.strip().lower()}%"
            params.extend([like, like])
        if group_name is not None:
            clauses.append("group_name = ?")
            params.append(group_name)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY COALESCE(display_name, address) LIMIT ?"
        params.append(limit)
        return self.query(sql, params)

    def list_mail_contact_groups(self) -> list[str]:
        return [r["group_name"] for r in self.query(
            "SELECT DISTINCT group_name FROM mail_contact "
            "WHERE group_name IS NOT NULL AND group_name != '' ORDER BY group_name")]

    def update_mail_contact(self, contact_id: int, **fields: Any) -> None:
        cols = {k: v for k, v in fields.items() if k in ("display_name", "group_name")}
        if not cols:
            return
        for key in cols:
            cols[key] = (cols[key] or "").strip() or None
        cols["updated_at"] = now_iso()
        set_clause = ", ".join(f"{k} = ?" for k in cols)
        self.execute(f"UPDATE mail_contact SET {set_clause} WHERE id = ?", (*cols.values(), contact_id))

    def delete_mail_contact(self, contact_id: int) -> None:
        self.execute("DELETE FROM mail_contact WHERE id = ?", (contact_id,))
