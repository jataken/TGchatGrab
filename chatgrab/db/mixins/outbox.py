"""The send log, blacklist, and drafts queue — see bots/outbox.py's
Outbox.wrap(), the one gate every outbound message passes through, which
this mixin backs with a place to record and check against."""
from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any

from ..timeutil import now_iso


class OutboxMixin:
    def log_outbox(self, bot_id: int, target: str, status: str, text: str,
                    is_first: bool = False) -> int:
        cur = self.execute(
            "INSERT INTO outbox_sends(bot_id, target, status, is_first, text, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (bot_id, target, status, 1 if is_first else 0, text, now_iso()),
        )
        return cur.lastrowid

    def last_outbox_send(self, bot_id: int, target: str) -> str | None:
        row = self.query_one(
            "SELECT created_at FROM outbox_sends WHERE bot_id = ? AND target = ? AND status = 'sent' "
            "ORDER BY created_at DESC LIMIT 1",
            (bot_id, target),
        )
        return row["created_at"] if row else None

    def outbox_count_since(self, bot_id: int, since_iso: str, *, first_only: bool = False) -> int:
        sql = "SELECT count(*) AS c FROM outbox_sends WHERE bot_id = ? AND status = 'sent' AND created_at >= ?"
        params: list[Any] = [bot_id, since_iso]
        if first_only:
            sql += " AND is_first = 1"
        row = self.query_one(sql, params)
        return row["c"] if row else 0

    def outbox_counts(self, bot_id: int) -> dict:
        """Sent so far in the current hour/day windows — just the observed
        counts, not the limits themselves: db/ doesn't import bots/settings
        (the reverse of the app's normal dependency direction), so pairing
        these with a limit is SendLimitsDialog's job, which already loads
        bot_settings for its own inputs anyway."""
        now_dt = dt.datetime.now().astimezone()
        hour = self.outbox_count_since(bot_id, (now_dt - dt.timedelta(hours=1)).isoformat())
        day = self.outbox_count_since(bot_id, (now_dt - dt.timedelta(days=1)).isoformat())
        first_today = self.outbox_count_since(
            bot_id, (now_dt - dt.timedelta(days=1)).isoformat(), first_only=True)
        return {"hour": hour, "day": day, "first_today": first_today}

    def is_blacklisted(self, bot_id: int, target: str) -> bool:
        return self.query_one(
            "SELECT 1 FROM outbox_blacklist WHERE bot_id = ? AND target = ?", (bot_id, target)
        ) is not None

    def add_to_blacklist(self, bot_id: int, target: str, reason: str | None = None) -> None:
        self.execute(
            "INSERT OR IGNORE INTO outbox_blacklist(bot_id, target, reason, created_at) VALUES (?, ?, ?, ?)",
            (bot_id, target, reason, now_iso()),
        )

    def remove_from_blacklist(self, bot_id: int, target: str) -> None:
        self.execute("DELETE FROM outbox_blacklist WHERE bot_id = ? AND target = ?", (bot_id, target))

    def list_blacklist(self, bot_id: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM outbox_blacklist WHERE bot_id = ? ORDER BY created_at DESC", (bot_id,))

    def add_draft(self, bot_id: int, target: str, text: str, reason: str | None = None) -> int:
        cur = self.execute(
            "INSERT INTO outbox_drafts(bot_id, target, text, reason, created_at) VALUES (?, ?, ?, ?, ?)",
            (bot_id, target, text, reason, now_iso()),
        )
        return cur.lastrowid

    def get_draft(self, draft_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM outbox_drafts WHERE id = ?", (draft_id,))

    def list_drafts(self, bot_id: int | None = None, pending_only: bool = True) -> list[sqlite3.Row]:
        sql = "SELECT * FROM outbox_drafts"
        clauses, params = [], []
        if bot_id is not None:
            clauses.append("bot_id = ?")
            params.append(bot_id)
        if pending_only:
            clauses.append("sent_at IS NULL AND dismissed_at IS NULL")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at"
        return self.query(sql, params)

    def mark_draft_sent(self, draft_id: int) -> None:
        self.execute("UPDATE outbox_drafts SET sent_at = ? WHERE id = ?", (now_iso(), draft_id))

    def dismiss_draft(self, draft_id: int) -> None:
        self.execute("UPDATE outbox_drafts SET dismissed_at = ? WHERE id = ?", (now_iso(), draft_id))
