"""П1: mailboxes, per-folder sync state, messages, and attachments.

Deliberately no join, no foreign key, and no shared identifier with any
Telegram table here — see PLAN.md's П-2 invariant ("Почта и Telegram не
смешиваются"). This mixin's only relationship to the rest of the schema
is that it lives in the same file as everything else, same as every other
mixin composed onto Database.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from ..timeutil import now_iso

# Header fields an upsert refreshes on every sync. Deliberately not
# body_text/body_html_path/has_attachments/body_fetched/is_read: those are
# set once, on demand, by set_mail_message_body() — re-upserting a
# message's headers (e.g. a defensive re-fetch) must not silently wipe an
# already-downloaded body back to NULL.
_MESSAGE_HEADER_COLUMNS = [
    "thread_id", "message_id", "in_reply_to", "refs", "subject",
    "sender_name", "sender_address", "to_addresses", "date", "is_outgoing",
]


class MailMixin:
    # ---- mailboxes -------------------------------------------------
    def add_mailbox(self, address: str, imap_host: str, imap_port: int = 993,
                     smtp_host: str | None = None, smtp_port: int = 465,
                     password_enc: str | None = None, display_name: str | None = None) -> int:
        cur = self.execute(
            "INSERT INTO mailbox"
            "(address, display_name, imap_host, imap_port, smtp_host, smtp_port, "
            " auth_kind, password_enc, enabled, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'password', ?, 1, ?)",
            (address.strip(), display_name, imap_host, imap_port, smtp_host, smtp_port,
             password_enc, now_iso()),
        )
        return cur.lastrowid

    def list_mailboxes(self, enabled_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM mailbox"
        if enabled_only:
            sql += " WHERE enabled = 1"
        return self.query(sql + " ORDER BY address")

    def get_mailbox(self, mailbox_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM mailbox WHERE id = ?", (mailbox_id,))

    def get_mailbox_by_address(self, address: str) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM mailbox WHERE address = ?", (address.strip(),))

    def set_mailbox_field(self, mailbox_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE mailbox SET {cols} WHERE id = ?", (*fields.values(), mailbox_id))

    def delete_mailbox(self, mailbox_id: int) -> None:
        """No ON DELETE CASCADE — foreign_keys stays OFF for the whole
        database (see Database.__init__), same as every other table here.
        Attachments before messages: they reference message rows this
        would otherwise orphan."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM mail_attachment WHERE message_id IN "
                "(SELECT id FROM mail_message WHERE mailbox_id = ?)", (mailbox_id,))
            self._conn.execute("DELETE FROM mail_message WHERE mailbox_id = ?", (mailbox_id,))
            self._conn.execute("DELETE FROM mail_thread WHERE mailbox_id = ?", (mailbox_id,))
            self._conn.execute("DELETE FROM mail_folder WHERE mailbox_id = ?", (mailbox_id,))
            self._conn.execute("DELETE FROM mailbox WHERE id = ?", (mailbox_id,))
            self._conn.commit()

    # ---- folder sync state -------------------------------------------
    def upsert_mail_folder(self, mailbox_id: int, name: str, enabled: bool = False) -> int:
        """Ensures a state row exists for this folder — called once per
        folder LIST discovers, so a folder that already has one (and may
        already have a nonzero last_uid) keeps it rather than resetting
        progress on every connect."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO mail_folder(mailbox_id, name, last_uid, enabled) "
                "VALUES (?, ?, 0, ?) "
                "ON CONFLICT(mailbox_id, name) DO NOTHING",
                (mailbox_id, name, 1 if enabled else 0),
            )
            self._conn.commit()
        row = self.get_mail_folder(mailbox_id, name)
        return row["id"]

    def list_mail_folders(self, mailbox_id: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM mail_folder WHERE mailbox_id = ? ORDER BY name", (mailbox_id,))

    def get_mail_folder(self, mailbox_id: int, name: str) -> sqlite3.Row | None:
        return self.query_one(
            "SELECT * FROM mail_folder WHERE mailbox_id = ? AND name = ?", (mailbox_id, name))

    def set_mail_folder_state(self, mailbox_id: int, name: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(
            f"UPDATE mail_folder SET {cols} WHERE mailbox_id = ? AND name = ?",
            (*fields.values(), mailbox_id, name),
        )

    def reset_mail_folder(self, mailbox_id: int, name: str, uidvalidity: int | None) -> None:
        """A changed UIDVALIDITY makes every previously stored UID in this
        folder meaningless (see imap_client.py's module docstring) — the
        one case a folder is fully re-read rather than incrementally
        fetched. Wipes both the sync cursor and the messages it produced,
        so the next tick starts this folder from UID 1 with a clean slate."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM mail_attachment WHERE message_id IN "
                "(SELECT id FROM mail_message WHERE mailbox_id = ? AND folder = ?)",
                (mailbox_id, name),
            )
            self._conn.execute(
                "DELETE FROM mail_message WHERE mailbox_id = ? AND folder = ?",
                (mailbox_id, name),
            )
            self._conn.execute(
                "UPDATE mail_folder SET uidvalidity = ?, last_uid = 0 "
                "WHERE mailbox_id = ? AND name = ?",
                (uidvalidity, mailbox_id, name),
            )
            self._conn.commit()

    # ---- messages -------------------------------------------------
    def upsert_mail_message(self, mailbox_id: int, folder: str, uid: int, **fields: Any) -> int:
        """Insert a message's header fields, or refresh them in place if
        this (mailbox, folder, uid) was already stored — body/read state
        set by set_mail_message_body() is never touched here, see
        _MESSAGE_HEADER_COLUMNS."""
        cols = [c for c in _MESSAGE_HEADER_COLUMNS if c in fields]
        col_names = ", ".join(cols)
        placeholders = ", ".join("?" for _ in cols)
        update_clause = ", ".join(f"{c} = excluded.{c}" for c in cols) or "uid = excluded.uid"
        with self._lock:
            self._conn.execute(
                f"INSERT INTO mail_message(mailbox_id, folder, uid, {col_names}, created_at) "
                f"VALUES (?, ?, ?, {placeholders}, ?) "
                f"ON CONFLICT(mailbox_id, folder, uid) DO UPDATE SET {update_clause}",
                (mailbox_id, folder, uid, *(fields[c] for c in cols), now_iso()),
            )
            self._conn.commit()
        # Not cur.lastrowid: on the ON CONFLICT DO UPDATE path SQLite
        # leaves last_insert_rowid() at whatever the connection's most
        # recent real INSERT was — which, mid-batch, is very often a
        # *different* message's id, not this row's. Every existing upsert
        # in this codebase (chats.upsert_chat, search_preset, app_settings)
        # avoids the same trap by never reading it back this way.
        return self.get_mail_message_by_uid(mailbox_id, folder, uid)["id"]

    def get_mail_message(self, message_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM mail_message WHERE id = ?", (message_id,))

    def get_mail_message_by_uid(self, mailbox_id: int, folder: str, uid: int) -> sqlite3.Row | None:
        return self.query_one(
            "SELECT * FROM mail_message WHERE mailbox_id = ? AND folder = ? AND uid = ?",
            (mailbox_id, folder, uid),
        )

    def list_mail_messages(self, mailbox_id: int, folder: str | None = None,
                            limit: int = 200) -> list[sqlite3.Row]:
        sql = "SELECT * FROM mail_message WHERE mailbox_id = ?"
        params: list[Any] = [mailbox_id]
        if folder is not None:
            sql += " AND folder = ?"
            params.append(folder)
        sql += " ORDER BY date DESC LIMIT ?"
        params.append(limit)
        return self.query(sql, params)

    def count_mail_messages(self, mailbox_id: int) -> int:
        row = self.query_one(
            "SELECT count(*) AS c FROM mail_message WHERE mailbox_id = ?", (mailbox_id,))
        return row["c"] if row else 0

    def set_mail_message_body(self, message_id: int, body_text: str | None,
                               body_html_path: str | None, has_attachments: bool) -> None:
        self.execute(
            "UPDATE mail_message SET body_text = ?, body_html_path = ?, "
            "has_attachments = ?, body_fetched = 1 WHERE id = ?",
            (body_text, body_html_path, 1 if has_attachments else 0, message_id),
        )

    # ---- attachments -------------------------------------------------
    def add_mail_attachment(self, message_id: int, filename: str, content_type: str | None,
                             size_bytes: int | None, path: str | None,
                             extracted_text: str | None = None) -> int:
        cur = self.execute(
            "INSERT INTO mail_attachment(message_id, filename, content_type, size_bytes, path, extracted_text) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (message_id, filename, content_type, size_bytes, path, extracted_text),
        )
        return cur.lastrowid

    def list_mail_attachments(self, message_id: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM mail_attachment WHERE message_id = ? ORDER BY id", (message_id,))
