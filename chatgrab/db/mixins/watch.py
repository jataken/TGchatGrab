"""Watch rules (words worth being told about the moment they appear) and
the hits they've produced — see services/watch_service.py."""
from __future__ import annotations

import sqlite3
from typing import Any

from ..timeutil import now_iso


class WatchMixin:
    def add_watch_rule(self, phrase: str, chat_id: int | None = None, notify: bool = True) -> int:
        cur = self.execute(
            "INSERT INTO watch_rule(phrase, chat_id, enabled, notify, created_at) "
            "VALUES (?, ?, 1, ?, ?)",
            (phrase.strip(), chat_id, 1 if notify else 0, now_iso()),
        )
        return cur.lastrowid

    def list_watch_rules(self, enabled_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM watch_rule"
        if enabled_only:
            sql += " WHERE enabled = 1"
        return self.query(sql + " ORDER BY created_at")

    def set_watch_rule(self, rule_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE watch_rule SET {cols} WHERE id = ?", (*fields.values(), rule_id))

    def delete_watch_rule(self, rule_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM watch_hit WHERE rule_id = ?", (rule_id,))
            self._conn.execute("DELETE FROM watch_rule WHERE id = ?", (rule_id,))
            self._conn.commit()

    def add_watch_hit(self, rule_id: int, chat_id: int, message_id: int) -> bool:
        """Records a match. Returns False when this message already matched
        this rule — re-scanning history must not resurrect old alerts."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO watch_hit(rule_id, chat_id, message_id, matched_at) "
                "VALUES (?, ?, ?, ?)",
                (rule_id, chat_id, message_id, now_iso()),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def list_watch_hits(self, unseen_only: bool = False, limit: int = 200) -> list[sqlite3.Row]:
        sql = ("SELECT h.*, r.phrase, m.text, m.date, m.sender_id, m.sender_display_name, "
               "       m.sender_username, m.link, m.media_path, c.title AS chat_title "
               "FROM watch_hit h "
               "JOIN watch_rule r ON r.id = h.rule_id "
               "LEFT JOIN messages m ON m.chat_id = h.chat_id AND m.message_id = h.message_id "
               "LEFT JOIN chats c ON c.chat_id = h.chat_id")
        if unseen_only:
            sql += " WHERE h.seen = 0"
        return self.query(sql + " ORDER BY h.matched_at DESC LIMIT ?", (limit,))

    def unseen_watch_count(self) -> int:
        row = self.query_one("SELECT count(*) AS c FROM watch_hit WHERE seen = 0")
        return row["c"] if row else 0

    def mark_watch_hits_seen(self, hit_ids: list[int] | None = None) -> None:
        if hit_ids:
            placeholders = ",".join("?" for _ in hit_ids)
            self.execute(f"UPDATE watch_hit SET seen = 1 WHERE id IN ({placeholders})", hit_ids)
        else:
            self.execute("UPDATE watch_hit SET seen = 1 WHERE seen = 0")
