"""Chats (the tracked-source list) and messages (what's actually been
collected from them) — kept in one mixin because almost every messages
method takes a chat_id and most chat methods exist to scope a messages
query, not because either is small enough to not deserve its own file."""
from __future__ import annotations

import sqlite3
from typing import Any

from ..dedup import fingerprint
from ..timeutil import now_iso


class ChatsMixin:
    # ---- chats --------------------------------------------------------
    def add_chat(self, chat_id: int, title: str, username: str | None,
                 depth_mode: str = "all", depth_from_date: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO chats(chat_id, title, username, is_tracked, enabled,
                       depth_mode, depth_from_date, status, created_at, queue_order)
                   VALUES (?, ?, ?, 1, 1, ?, ?, 'queued', ?,
                       (SELECT COALESCE(MAX(queue_order), 0) + 1 FROM chats))
                   ON CONFLICT(chat_id) DO UPDATE SET
                       is_tracked = 1, title = excluded.title, username = excluded.username""",
                (chat_id, title, username, depth_mode, depth_from_date, now_iso()),
            )
            self._conn.commit()

    def list_chats(self, tracked_only: bool = True) -> list[sqlite3.Row]:
        sql = "SELECT * FROM chats"
        if tracked_only:
            sql += " WHERE is_tracked = 1"
        sql += " ORDER BY queue_order"
        return self.query(sql)

    def get_chat(self, chat_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM chats WHERE chat_id = ?", (chat_id,))

    def set_chat_field(self, chat_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(
            f"UPDATE chats SET {cols} WHERE chat_id = ?",
            (*fields.values(), chat_id),
        )

    def untrack_chat(self, chat_id: int) -> None:
        """Remove from the list but keep collected messages/photos."""
        self.set_chat_field(chat_id, is_tracked=0, enabled=0, status="off")

    def delete_chat_and_data(self, chat_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
            self._conn.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
            self._conn.execute("DELETE FROM chat_stat_cache WHERE chat_id = ?", (chat_id,))
            self._conn.execute("DELETE FROM ignore_rule WHERE chat_id = ?", (chat_id,))
            self._conn.commit()

    # ---- messages -------------------------------------------------
    def upsert_message(self, m: dict[str, Any]) -> bool:
        """Insert or update-on-edit a message, keyed on (chat_id, message_id).
        Returns True if a new row was inserted, False if an existing one was
        updated (edit)."""
        existing = self.query_one(
            "SELECT id FROM messages WHERE chat_id = ? AND message_id = ?",
            (m["chat_id"], m["message_id"]),
        )
        m = dict(m)
        m["char_len"] = len(m.get("text") or "")
        m["is_reply"] = 1 if m.get("reply_to_message_id") else 0
        m["is_forward"] = 1 if m.get("forwarded_from") else 0
        m["is_hidden"] = 1 if m.get("is_hidden") else 0
        # Recomputed on edit too: an edited message is a different text and
        # so a different repeat-group than the one it was posted as.
        m["text_hash"] = fingerprint(m.get("text") or "")
        cols = ["chat_id", "message_id", "chat_title", "date", "edited_date",
                "sender_id", "sender_username", "sender_display_name", "text",
                "reply_to_message_id", "forwarded_from", "media_type",
                "media_caption", "media_path", "views", "link", "char_len",
                "is_reply", "is_forward", "is_hidden", "text_hash"]
        values = [m.get(c) for c in cols]
        with self._lock:
            if existing:
                set_clause = ", ".join(f"{c} = ?" for c in cols)
                self._conn.execute(
                    f"UPDATE messages SET {set_clause} WHERE id = ?",
                    (*values, existing["id"]),
                )
                self._conn.commit()
                return False
            placeholders = ", ".join("?" for _ in cols)
            self._conn.execute(
                f"INSERT INTO messages({', '.join(cols)}) VALUES ({placeholders})",
                values,
            )
            self._conn.commit()
            return True

    def message_count(self, chat_id: int | None = None, include_hidden: bool = True) -> int:
        sql = "SELECT count(*) AS c FROM messages"
        clauses, params = [], []
        if chat_id is not None:
            clauses.append("chat_id = ?")
            params.append(chat_id)
        if not include_hidden:
            clauses.append("is_hidden = 0")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return self.query_one(sql, params)["c"]

    def media_count(self, chat_id: int | None = None, media_type: str | None = None) -> int:
        sql = "SELECT count(*) AS c FROM messages WHERE media_path IS NOT NULL AND media_path != ''"
        params: list[Any] = []
        if chat_id is not None:
            sql += " AND chat_id = ?"
            params.append(chat_id)
        if media_type is not None:
            sql += " AND media_type = ?"
            params.append(media_type)
        return self.query_one(sql, params)["c"]

    def last_message_date(self, chat_id: int) -> str | None:
        row = self.query_one(
            "SELECT max(date) AS d FROM messages WHERE chat_id = ?", (chat_id,)
        )
        return row["d"] if row else None

    def existing_message_ids(self, chat_id: int) -> set[int]:
        rows = self.query("SELECT message_id FROM messages WHERE chat_id = ?", (chat_id,))
        return {r["message_id"] for r in rows}

    def min_max_message_id(self, chat_id: int) -> tuple[int | None, int | None]:
        row = self.query_one(
            "SELECT min(message_id) AS mn, max(message_id) AS mx FROM messages WHERE chat_id = ?",
            (chat_id,),
        )
        return (row["mn"], row["mx"]) if row else (None, None)

    def find_gaps(self, chat_id: int) -> list[tuple[int, int]]:
        """Missing message_id ranges between the min and max ids we hold."""
        rows = self.query(
            "SELECT message_id FROM messages WHERE chat_id = ? ORDER BY message_id",
            (chat_id,),
        )
        ids = [r["message_id"] for r in rows]
        gaps: list[tuple[int, int]] = []
        for a, b in zip(ids, ids[1:]):
            if b - a > 1:
                gaps.append((a + 1, b - 1))
        return gaps

    def repeat_summary(self, chat_ids: list[int] | None = None) -> dict[str, int]:
        """How many stored messages are reposts of text already collected —
        i.e. how much «только уникальные» would leave out."""
        scope, params = "", []
        if chat_ids:
            scope = " AND chat_id IN (" + ",".join("?" for _ in chat_ids) + ")"
            params = list(chat_ids)
        row = self.query_one(
            f"""
            SELECT count(*) AS groups, coalesce(sum(n - 1), 0) AS repeats FROM (
                SELECT count(*) AS n FROM messages
                WHERE text_hash IS NOT NULL{scope}
                GROUP BY chat_id, text_hash HAVING n > 1)
            """,
            params,
        )
        return {
            "repeats": row["repeats"] if row else 0,
            "groups": row["groups"] if row else 0,
        }

    def gap_summary(self, chat_id: int) -> dict[str, int]:
        """How many holes there are in the collected id sequence, and how
        many messages they add up to.

        Computed in SQL rather than by pulling every id into Python the way
        find_gaps() does, so this can run on every screen refresh for a chat
        with tens of thousands of messages.

        A gap is not proof of loss: Telegram's per-chat ids also cover
        service messages and anything since deleted, and those holes are
        permanent. It marks a chat worth re-checking, not a defect."""
        row = self.query_one(
            """
            SELECT count(*) AS gaps, coalesce(sum(diff - 1), 0) AS missing FROM (
                SELECT message_id - lag(message_id) OVER (ORDER BY message_id) AS diff
                FROM messages WHERE chat_id = ?
            ) WHERE diff > 1
            """,
            (chat_id,),
        )
        return {"gaps": row["gaps"] if row else 0, "missing": row["missing"] if row else 0}

    # ---- authors ----------------------------------------------------
    def authors_for_chat(self, chat_id: int) -> list[sqlite3.Row]:
        return self.query(
            """SELECT sender_id, sender_username, sender_display_name,
                      count(*) AS n, min(date) AS first, max(date) AS last
               FROM messages WHERE chat_id = ? AND sender_id IS NOT NULL
               GROUP BY sender_id ORDER BY n DESC""",
            (chat_id,),
        )
