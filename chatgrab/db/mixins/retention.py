"""How much is stored, and cutting it down — see services/retention_service.py.
chat_storage() is also read by mixins/productivity.py's chat_productivity()
(both mixins end up on the same Database instance, so calling
self.chat_storage() from there works exactly like calling any other own
method would)."""
from __future__ import annotations

import sqlite3
from typing import Any


class RetentionMixin:
    def chat_storage(self) -> list[dict]:
        """Per-chat volume: how many messages, how far back they go, and how
        many carry a downloaded file. Feeds both the retention screen and
        the source-productivity view."""
        rows = self.query(
            """
            SELECT c.chat_id, c.title,
                   count(m.id) AS messages,
                   sum(CASE WHEN m.media_path IS NOT NULL AND m.media_path != '' THEN 1 ELSE 0 END) AS media,
                   min(m.date) AS oldest, max(m.date) AS newest,
                   coalesce(sum(m.char_len), 0) AS chars
            FROM chats c LEFT JOIN messages m ON m.chat_id = c.chat_id
            GROUP BY c.chat_id, c.title ORDER BY messages DESC
            """
        )
        return [dict(r) for r in rows]

    def messages_older_than(self, cutoff_iso: str, chat_id: int | None = None) -> int:
        sql = "SELECT count(*) AS c FROM messages WHERE date < ?"
        params: list[Any] = [cutoff_iso]
        if chat_id is not None:
            sql += " AND chat_id = ?"
            params.append(chat_id)
        return self.query_one(sql, params)["c"]

    def select_older_than(self, cutoff_iso: str) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM messages WHERE date < ? ORDER BY date", (cutoff_iso,))

    def delete_older_than(self, cutoff_iso: str) -> int:
        """Deletes and reports how many rows went. FTS and watch hits are
        cleaned alongside so nothing points at a message that is gone."""
        with self._lock:
            cur = self._conn.execute("SELECT count(*) FROM messages WHERE date < ?", (cutoff_iso,))
            n = cur.fetchone()[0]
            self._conn.execute(
                "DELETE FROM watch_hit WHERE (chat_id, message_id) IN "
                "(SELECT chat_id, message_id FROM messages WHERE date < ?)",
                (cutoff_iso,),
            )
            self._conn.execute("DELETE FROM messages WHERE date < ?", (cutoff_iso,))
            self._conn.commit()
            return n
