"""П10: mail_filter/mail_filter_log — rules that act on new mail as it
syncs, and the journal every one of their hits writes ("каждое
срабатывание пишется в журнал и отменяется одной кнопкой" — the DB side
of that; the actual apply/undo orchestration against a live IMAP
connection is MailService's job, see services/mail_service.py).
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..timeutil import now_iso


class MailFiltersMixin:
    # ---- filters ---------------------------------------------------------
    def create_mail_filter(self, name: str, conditions: list[dict], *,
                            mailbox_id: int | None = None, label_id: int | None = None,
                            move_to_folder: str | None = None, mark_read: bool = False,
                            no_notify: bool = False) -> int:
        order_index = (self.query_one(
            "SELECT COALESCE(MAX(order_index), -1) + 1 AS n FROM mail_filter "
            "WHERE mailbox_id IS ?", (mailbox_id,))["n"])
        cur = self.execute(
            "INSERT INTO mail_filter"
            "(mailbox_id, name, enabled, order_index, conditions, label_id, move_to_folder, "
            " mark_read, no_notify, created_at) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)",
            (mailbox_id, name.strip(), order_index, json.dumps(conditions, ensure_ascii=False),
             label_id, move_to_folder, 1 if mark_read else 0, 1 if no_notify else 0, now_iso()),
        )
        return cur.lastrowid

    def list_mail_filters(self, mailbox_id: int | None = None,
                           enabled_only: bool = False) -> list[sqlite3.Row]:
        """mailbox_id filters to rules that apply to that mailbox — a
        filter with mailbox_id NULL applies everywhere, so it's included
        alongside whatever's specific to the mailbox asked for. Passing
        no mailbox_id at all lists every filter, mailbox-specific or
        not (the filters-management screen's own list)."""
        sql = "SELECT * FROM mail_filter"
        clauses, params = [], []
        if mailbox_id is not None:
            clauses.append("(mailbox_id IS NULL OR mailbox_id = ?)")
            params.append(mailbox_id)
        if enabled_only:
            clauses.append("enabled = 1")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY order_index, id"
        return self.query(sql, params)

    def get_mail_filter(self, filter_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM mail_filter WHERE id = ?", (filter_id,))

    def update_mail_filter(self, filter_id: int, **fields: Any) -> None:
        cols = {k: v for k, v in fields.items()
                if k in ("mailbox_id", "name", "enabled", "conditions", "label_id",
                         "move_to_folder", "mark_read", "no_notify")}
        if not cols:
            return
        if "name" in cols:
            cols["name"] = cols["name"].strip()
        if "conditions" in cols and not isinstance(cols["conditions"], str):
            cols["conditions"] = json.dumps(cols["conditions"], ensure_ascii=False)
        for flag in ("enabled", "mark_read", "no_notify"):
            if flag in cols:
                cols[flag] = 1 if cols[flag] else 0
        set_clause = ", ".join(f"{k} = ?" for k in cols)
        self.execute(f"UPDATE mail_filter SET {set_clause} WHERE id = ?", (*cols.values(), filter_id))

    def delete_mail_filter(self, filter_id: int) -> None:
        self.execute("DELETE FROM mail_filter WHERE id = ?", (filter_id,))

    def reorder_mail_filters(self, mailbox_id: int | None, ordered_ids: list[int]) -> None:
        """Full order_index rewrite from the screen's current order —
        same reorder() shape as directions.py/funnels.py before it."""
        with self._lock:
            for index, filter_id in enumerate(ordered_ids):
                self._conn.execute(
                    "UPDATE mail_filter SET order_index = ? WHERE id = ? AND mailbox_id IS ?",
                    (index, filter_id, mailbox_id))
            self._conn.commit()

    # ---- log / undo --------------------------------------------------
    def log_filter_hit(self, filter_id: int, message_id: int, summary: str, undo_data: dict) -> int:
        cur = self.execute(
            "INSERT INTO mail_filter_log(filter_id, message_id, applied_at, summary, undo_data) "
            "VALUES (?, ?, ?, ?, ?)",
            (filter_id, message_id, now_iso(), summary, json.dumps(undo_data, ensure_ascii=False)),
        )
        return cur.lastrowid

    def filter_already_applied(self, filter_id: int, message_id: int) -> bool:
        """Idempotency guard: a filter runs both at header-sync time and
        again once the body's fetched (has_attachment/size conditions
        only become knowable then, same staged evaluation as
        mail_triage's own score()/rescan). Without this, a header-stage
        hit and a body-stage re-hit on the same filter+message would
        double-apply (a second identical label add is harmless, but a
        second "move" or a second log row isn't)."""
        row = self.query_one(
            "SELECT 1 FROM mail_filter_log WHERE filter_id = ? AND message_id = ? LIMIT 1",
            (filter_id, message_id))
        return row is not None

    def list_filter_log(self, filter_id: int | None = None, limit: int = 200) -> list[sqlite3.Row]:
        sql = "SELECT * FROM mail_filter_log"
        params: list[Any] = []
        if filter_id is not None:
            sql += " WHERE filter_id = ?"
            params.append(filter_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return self.query(sql, params)

    def get_filter_log_entry(self, log_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM mail_filter_log WHERE id = ?", (log_id,))

    def mark_filter_log_undone(self, log_id: int) -> None:
        self.execute("UPDATE mail_filter_log SET undone = 1 WHERE id = ?", (log_id,))

    def count_filter_hits_since(self, since_iso: str) -> int:
        """«фильтры спрятали N писем» — every hit counts, not just
        no_notify ones: a moved-out-of-Входящие message is just as
        "hidden" from the inbox list as a muted one, and that's the
        counter's own wording ("спрятали"), not "заглушили"."""
        row = self.query_one(
            "SELECT count(*) AS n FROM mail_filter_log WHERE applied_at >= ?", (since_iso,))
        return row["n"] if row else 0
