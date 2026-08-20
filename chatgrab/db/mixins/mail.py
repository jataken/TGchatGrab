"""П1: mailboxes, per-folder sync state, messages, and attachments.

Deliberately no join, no foreign key, and no shared identifier with any
Telegram table here — see PLAN.md's П-2 invariant ("Почта и Telegram не
смешиваются"). This mixin's only relationship to the rest of the schema
is that it lives in the same file as everything else, same as every other
mixin composed onto Database.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..timeutil import now_iso
from .search import _fts_query

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

    def rename_mail_folder_record(self, mailbox_id: int, old_name: str, new_name: str) -> None:
        """Local half of a server-confirmed rename (see MailService) —
        the folder row and every message already synced under its old
        name both follow, so open threads/lists don't quietly point at
        a name that no longer exists on the server."""
        with self._lock:
            self._conn.execute(
                "UPDATE mail_folder SET name = ? WHERE mailbox_id = ? AND name = ?",
                (new_name, mailbox_id, old_name))
            self._conn.execute(
                "UPDATE mail_message SET folder = ? WHERE mailbox_id = ? AND folder = ?",
                (new_name, mailbox_id, old_name))
            self._conn.commit()

    def delete_mail_folder_record(self, mailbox_id: int, name: str) -> None:
        """Local half of a server-confirmed folder deletion — same
        cleanup order as delete_mailbox()/reset_mail_folder(): attachments
        before messages, since nothing here has ON DELETE CASCADE."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM mail_attachment WHERE message_id IN "
                "(SELECT id FROM mail_message WHERE mailbox_id = ? AND folder = ?)",
                (mailbox_id, name))
            self._conn.execute(
                "DELETE FROM mail_message WHERE mailbox_id = ? AND folder = ?",
                (mailbox_id, name))
            self._conn.execute(
                "DELETE FROM mail_folder WHERE mailbox_id = ? AND name = ?", (mailbox_id, name))
            self._conn.commit()

    def get_mail_folder_by_special_use(self, mailbox_id: int, special_use: str) -> sqlite3.Row | None:
        return self.query_one(
            "SELECT * FROM mail_folder WHERE mailbox_id = ? AND special_use = ?",
            (mailbox_id, special_use))

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

    # ---- threads (П2) --------------------------------------------------
    # Assembly itself is core/mail_thread.py's job — everything here is
    # just the queries that logic needs (candidate lookup) or produces
    # (creating/assigning a thread). See services/mail_service.py for the
    # orchestration that calls both.
    def create_mail_thread(self, mailbox_id: int, subject_norm: str) -> int:
        cur = self.execute(
            "INSERT INTO mail_thread(mailbox_id, subject_norm, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (mailbox_id, subject_norm, now_iso(), now_iso()),
        )
        return cur.lastrowid

    def list_mail_threads_by_subject(self, mailbox_id: int, subject_norm: str) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM mail_thread WHERE mailbox_id = ? AND subject_norm = ?",
            (mailbox_id, subject_norm),
        )

    def thread_participants(self, thread_id: int, exclude: str | None = None) -> set[str]:
        """exclude is the mailbox's own address, lowercased. Without
        excluding it, every message in the mailbox trivially "shares a
        participant" with every other one (the mailbox owner is on both
        sides of every conversation), which would make the overlap check
        in core.mail_thread.find_subject_fallback_thread match on nothing
        but the mailbox's own address — i.e. match everything with the
        same subject, regardless of who it's actually with."""
        addrs: set[str] = set()
        for row in self.query(
            "SELECT sender_address, to_addresses FROM mail_message WHERE thread_id = ?", (thread_id,)
        ):
            if row["sender_address"]:
                addrs.add(row["sender_address"].strip().lower())
            try:
                to_list = json.loads(row["to_addresses"] or "[]")
            except (json.JSONDecodeError, TypeError):
                to_list = []
            addrs.update(a.strip().lower() for a in to_list if a)
        if exclude:
            addrs.discard(exclude.strip().lower())
        return addrs

    def thread_last_date(self, thread_id: int) -> str | None:
        row = self.query_one("SELECT MAX(date) AS d FROM mail_message WHERE thread_id = ?", (thread_id,))
        return row["d"] if row else None

    def set_message_thread(self, message_id: int, thread_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE mail_message SET thread_id = ? WHERE id = ?", (thread_id, message_id))
            self._conn.execute("UPDATE mail_thread SET updated_at = ? WHERE id = ?", (now_iso(), thread_id))
            self._conn.commit()

    def list_mail_threads(self, mailbox_id: int, folder: str | None = None,
                           unread_only: bool = False, with_attachments_only: bool = False,
                           limit: int = 200) -> list[sqlite3.Row]:
        """One row per thread that has at least one message, newest
        activity first — subject/sender shown are the *latest* message's,
        not the thread's own (a normalized, lowercased subject_norm isn't
        fit to display)."""
        clauses = ["t.mailbox_id = ?"]
        params: list[Any] = [mailbox_id]
        if folder is not None:
            clauses.append("EXISTS (SELECT 1 FROM mail_message mf WHERE mf.thread_id = t.id AND mf.folder = ?)")
            params.append(folder)
        having = []
        if unread_only:
            having.append("unread_count > 0")
        if with_attachments_only:
            having.append("has_attachments = 1")
        having_sql = f" HAVING {' AND '.join(having)}" if having else ""
        params.append(limit)
        return self.query(
            f"""
            SELECT
                t.id AS thread_id, t.mailbox_id, t.subject_norm,
                (SELECT m2.subject FROM mail_message m2 WHERE m2.thread_id = t.id
                 ORDER BY m2.date DESC, m2.id DESC LIMIT 1) AS subject,
                (SELECT m2.sender_name FROM mail_message m2 WHERE m2.thread_id = t.id
                 ORDER BY m2.date DESC, m2.id DESC LIMIT 1) AS sender_name,
                (SELECT m2.sender_address FROM mail_message m2 WHERE m2.thread_id = t.id
                 ORDER BY m2.date DESC, m2.id DESC LIMIT 1) AS sender_address,
                MAX(m.date) AS last_date,
                COUNT(m.id) AS message_count,
                SUM(CASE WHEN m.is_read = 0 THEN 1 ELSE 0 END) AS unread_count,
                MAX(m.has_attachments) AS has_attachments,
                MAX(m.is_flagged) AS has_flagged
            FROM mail_thread t
            JOIN mail_message m ON m.thread_id = t.id
            WHERE {' AND '.join(clauses)}
            GROUP BY t.id
            {having_sql}
            ORDER BY last_date DESC
            LIMIT ?
            """,
            params,
        )

    def list_thread_messages(self, thread_id: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM mail_message WHERE thread_id = ? ORDER BY date, id", (thread_id,))

    def mark_thread_read(self, thread_id: int) -> list[sqlite3.Row]:
        """Marks every unread message in the thread read locally and
        returns the rows that changed (folder + uid), so the caller can
        push \\Seen to the server for exactly those — see
        MailService.push_read_flags(). Returns [] (and touches nothing)
        if the thread was already fully read, so a caller doesn't need to
        check first."""
        rows = self.query(
            "SELECT * FROM mail_message WHERE thread_id = ? AND is_read = 0", (thread_id,))
        if rows:
            self.execute("UPDATE mail_message SET is_read = 1 WHERE thread_id = ?", (thread_id,))
        return rows

    # ---- search (П2) — local FTS5 only; server-side IMAP SEARCH is
    # integrations/mail/imap_client.py's job, orchestrated by MailService,
    # since it needs a live connection this layer never holds ----------
    def search_mail(self, mailbox_id: int, query: str, folder: str | None = None,
                     limit: int = 100) -> list[sqlite3.Row]:
        if not query.strip():
            return self.list_mail_messages(mailbox_id, folder=folder, limit=limit)
        sql = (
            "SELECT m.* FROM mail_fts f JOIN mail_message m ON m.id = f.rowid "
            "WHERE mail_fts MATCH ? AND m.mailbox_id = ?"
        )
        params: list[Any] = [_fts_query(query), mailbox_id]
        if folder is not None:
            sql += " AND m.folder = ?"
            params.append(folder)
        sql += " ORDER BY m.date DESC LIMIT ?"
        params.append(limit)
        return self.query(sql, params)

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

    def get_mail_message_by_message_id(self, mailbox_id: int, message_id: str) -> sqlite3.Row | None:
        """Looks up a stored message by its RFC822 Message-ID header —
        the exact-match signal core/mail_thread.py's reference-based
        threading resolves a References/In-Reply-To entry against."""
        return self.query_one(
            "SELECT * FROM mail_message WHERE mailbox_id = ? AND message_id = ? "
            "ORDER BY id LIMIT 1",
            (mailbox_id, message_id),
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

    def get_mail_attachment(self, attachment_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM mail_attachment WHERE id = ?", (attachment_id,))

    def set_attachment_extracted_text(self, attachment_id: int, text: str) -> None:
        """The UPDATE alone is enough to make the attachment's content
        searchable — mail_attachment_text_au (migration 015) recomputes
        mail_message.attachments_text from it, and mail_message_au
        carries that into mail_fts, the same cascade
        thread_participants()'s caller relies on for updated_at."""
        self.execute(
            "UPDATE mail_attachment SET extracted_text = ? WHERE id = ?", (text, attachment_id))

    # ---- flags (П4) ----------------------------------------------------
    _FLAG_COLUMNS = {"is_read", "is_flagged", "is_answered", "is_forwarded"}

    def set_message_flags(self, message_id: int, **flags: bool) -> None:
        cols = {k: v for k, v in flags.items() if k in self._FLAG_COLUMNS}
        if not cols:
            return
        set_clause = ", ".join(f"{k} = ?" for k in cols)
        self.execute(
            f"UPDATE mail_message SET {set_clause} WHERE id = ?",
            (*(1 if v else 0 for v in cols.values()), message_id))

    def sync_message_flags(self, mailbox_id: int, folder: str, uid: int, flags: dict) -> None:
        """Reconciles server-reported flags into an already-upserted
        message — a normal header sync's other half, alongside
        upsert_mail_message() for subject/sender/date. Deliberately
        separate from that call (not folded into _MESSAGE_HEADER_COLUMNS):
        a message with a still-pending local action (say, "mark read"
        that hasn't reached the server yet — see mail_action_queue) is
        left alone here, so a resync mid-flight can't silently revert an
        optimistic local change back to what the server said *before*
        that action landed. Once the queued action is confirmed applied,
        the next sync's server-reported flags become authoritative again,
        same as for a message with no pending action at all."""
        message = self.get_mail_message_by_uid(mailbox_id, folder, uid)
        if message is None or self.has_pending_mail_action(message["id"]):
            return
        self.set_message_flags(message["id"], **flags)

    # ---- move / delete / restore (П4) -----------------------------------
    def move_message_local(self, message_id: int, new_folder: str) -> None:
        """Same-mailbox move only — thread_id stays valid, since threads
        are scoped to a mailbox, not a folder (see П2). A cross-mailbox
        move is a different message row entirely under the destination
        mailbox, handled by MailService as fetch + upsert-there +
        delete-here, not by this method."""
        self.execute(
            "UPDATE mail_message SET folder = ?, restore_folder = NULL WHERE id = ?",
            (new_folder, message_id))

    def move_message_to_trash_local(self, message_id: int, trash_folder: str) -> None:
        """Like move_message_local(), but remembers where the message
        came from so restore_message_from_trash() has something to
        restore to — the "reversible while it's in Trash" half of the
        П4 checklist's undo requirement."""
        message = self.get_mail_message(message_id)
        if message is None:
            return
        self.execute(
            "UPDATE mail_message SET folder = ?, restore_folder = ? WHERE id = ?",
            (trash_folder, message["folder"], message_id))

    def set_message_restore_folder(self, message_id: int, restore_folder: str | None) -> None:
        """Used by MailService after a confirmed trash-move's placeholder
        delete-and-resync (see _apply_move_action) — the fresh row that
        resync creates starts with restore_folder NULL like any newly
        upserted message, so this is what re-attaches "where it came
        from" onto the row that's actually going to stick around,
        looked up by Message-ID once the real UID is known."""
        self.execute(
            "UPDATE mail_message SET restore_folder = ? WHERE id = ?", (restore_folder, message_id))

    def restore_message_from_trash(self, message_id: int) -> str | None:
        """Moves a message back to restore_folder and clears it. Returns
        the folder it was restored to (for the caller to push the same
        move server-side), or None if there was nothing to restore —
        either the message isn't in Trash, or it never went through
        move_message_to_trash_local() (arrived in Trash some other way,
        so there's no "back" recorded for it)."""
        message = self.get_mail_message(message_id)
        if message is None or not message["restore_folder"]:
            return None
        target = message["restore_folder"]
        self.execute(
            "UPDATE mail_message SET folder = ?, restore_folder = NULL WHERE id = ?",
            (target, message_id))
        return target

    def list_trash_messages(self, mailbox_id: int) -> list[sqlite3.Row]:
        folder = self.get_mail_folder_by_special_use(mailbox_id, "Trash")
        if folder is None:
            return []
        return self.list_mail_messages(mailbox_id, folder=folder["name"])

    def delete_mail_message_local(self, message_id: int) -> None:
        """Permanent delete's local half — no undo past this point,
        matching permanently_delete()'s server-side counterpart."""
        with self._lock:
            self._conn.execute("DELETE FROM mail_attachment WHERE message_id = ?", (message_id,))
            self._conn.execute("DELETE FROM mail_message WHERE id = ?", (message_id,))
            self._conn.commit()

    # ---- offline action queue (П4) --------------------------------------
    # Scoped to exactly what PLAN.md's checklist names — "действия
    # (пометки, перемещения)" — per-message tag/move/delete actions.
    # Folder administration (create/rename/delete/subscribe) always
    # needs a live connection to mean anything, so it isn't queued.
    def enqueue_mail_action(self, mailbox_id: int, message_id: int | None,
                             kind: str, payload: dict) -> int:
        cur = self.execute(
            "INSERT INTO mail_action_queue(mailbox_id, message_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (mailbox_id, message_id, kind, json.dumps(payload, ensure_ascii=False), now_iso()),
        )
        return cur.lastrowid

    def list_pending_mail_actions(self, mailbox_id: int | None = None) -> list[sqlite3.Row]:
        if mailbox_id is None:
            return self.query(
                "SELECT * FROM mail_action_queue WHERE applied_at IS NULL ORDER BY id")
        return self.query(
            "SELECT * FROM mail_action_queue WHERE applied_at IS NULL AND mailbox_id = ? ORDER BY id",
            (mailbox_id,))

    def mark_mail_action_applied(self, action_id: int) -> None:
        self.execute(
            "UPDATE mail_action_queue SET applied_at = ? WHERE id = ?", (now_iso(), action_id))

    def has_pending_mail_action(self, message_id: int) -> bool:
        row = self.query_one(
            "SELECT 1 FROM mail_action_queue WHERE message_id = ? AND applied_at IS NULL LIMIT 1",
            (message_id,))
        return row is not None
