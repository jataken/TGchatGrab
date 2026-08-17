"""Everything export_service.py needs from the database: which messages
match a set of filters (export_select — the same filter shape
search_messages in mixins/search.py builds, just without pagination or a
total count, since an export wants every matching row), the log of past
runs (for "только новое с прошлой выгрузки"), saved parameter presets, and
scheduled-export rows."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .search import _fts_query
from ..timeutil import now_iso


class ExportMixin:
    # ---- export selection ---------------------------------------------
    def export_select(self, chat_ids: list[int], date_from: str | None = None,
                       date_to: str | None = None, include_hidden: bool = False,
                       query: str = "", author: str = "", photos_only: bool = False,
                       forwards_only: bool = False, replies_only: bool = False,
                       min_id_by_chat: dict[int, int] | None = None,
                       unique_only: bool = False,
                       _columns: str = "m.*") -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[Any] = []
        from_sql = "FROM messages m"
        if query.strip():
            from_sql = "FROM messages_fts f JOIN messages m ON m.id = f.rowid"
            clauses.append("messages_fts MATCH ?")
            params.append(_fts_query(query))
        if chat_ids:
            if min_id_by_chat is not None:
                or_parts = []
                for cid in chat_ids:
                    if cid in min_id_by_chat:
                        or_parts.append("(m.chat_id = ? AND m.message_id > ?)")
                        params += [cid, min_id_by_chat[cid]]
                    else:
                        or_parts.append("(m.chat_id = ?)")
                        params.append(cid)
                clauses.append("(" + " OR ".join(or_parts) + ")")
            else:
                placeholders = ",".join("?" for _ in chat_ids)
                clauses.append(f"m.chat_id IN ({placeholders})")
                params += list(chat_ids)
        else:
            return []
        if date_from:
            clauses.append("m.date >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("m.date <= ?")
            params.append(date_to)
        if author.strip():
            clauses.append("(m.sender_display_name LIKE ? OR m.sender_username LIKE ?)")
            like = f"%{author.strip()}%"
            params += [like, like]
        if photos_only:
            clauses.append("m.media_type = 'photo' AND m.media_path IS NOT NULL AND m.media_path != ''")
        if forwards_only:
            clauses.append("m.is_forward = 1")
        if replies_only:
            clauses.append("m.is_reply = 1")
        if not include_hidden:
            clauses.append("m.is_hidden = 0")
        if unique_only:
            # Keep the first posting of each repeated text within its chat
            # and drop the later reposts. Messages without a fingerprint
            # (short ones, media-only) always pass — see db/dedup.py for
            # why those are not treated as repeats.
            clauses.append(
                "(m.text_hash IS NULL OR m.message_id = ("
                "  SELECT min(e.message_id) FROM messages e"
                "  WHERE e.chat_id = m.chat_id AND e.text_hash = m.text_hash))"
            )
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        # Pure chronological order — not grouped by chat first — so a
        # merged multi-chat export reads as one continuous timeline
        # (old to new) instead of one chat's whole history, then the
        # next chat's from its own beginning. Per-chat file splitting
        # groups these afterward without disturbing the relative order,
        # so single-chat output stays date-sorted too.
        return self.query(
            f"SELECT {_columns} {from_sql}{where} ORDER BY m.date ASC", params
        )

    def export_select_meta(self, **kwargs) -> list[sqlite3.Row]:
        """The same selection as export_select, but only the columns needed
        to plan an export: which chat, when, how long, and whether media is
        attached.

        The estimate on the export screen re-runs on every toggle, and
        pulling full message text each time made that cost grow with the
        size of the archive. `char_len` is stored per row precisely so the
        token estimate never has to read the text itself."""
        return self.export_select(_columns="m.chat_id, m.date, m.char_len, m.media_path", **kwargs)

    # ---- export log / presets ----------------------------------------
    def add_export_log(self, **fields: Any) -> int:
        cols = list(fields.keys())
        placeholders = ", ".join("?" for _ in cols)
        with self._lock:
            cur = self._conn.execute(
                f"INSERT INTO export_log({', '.join(cols)}) VALUES ({placeholders})",
                list(fields.values()),
            )
            self._conn.commit()
            return cur.lastrowid

    def list_export_log(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM export_log ORDER BY id DESC LIMIT ?", (limit,))

    def last_export_max_id(self, chat_id: int) -> int | None:
        rows = self.query(
            "SELECT max_message_id_by_chat FROM export_log ORDER BY id DESC LIMIT 50"
        )
        for row in rows:
            data = json.loads(row["max_message_id_by_chat"])
            if str(chat_id) in data:
                return data[str(chat_id)]
        return None

    def incremental_baseline(self, chat_ids: list[int]) -> dict[int, int]:
        """Highest message_id already exported per chat, across all past
        exports — the cutoff for "только новое с прошлой выгрузки"."""
        wanted = set(chat_ids)
        best: dict[int, int] = {}
        for row in self.query("SELECT max_message_id_by_chat FROM export_log"):
            data = json.loads(row["max_message_id_by_chat"])
            for k, v in data.items():
                cid = int(k)
                if cid in wanted:
                    best[cid] = max(best.get(cid, 0), v)
        return best

    def save_preset(self, name: str, params: dict[str, Any]) -> None:
        self.execute(
            "INSERT INTO export_preset(name, params, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET params = excluded.params",
            (name, json.dumps(params, ensure_ascii=False), now_iso()),
        )

    def list_presets(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM export_preset ORDER BY created_at DESC")

    def delete_preset(self, name: str) -> None:
        self.execute("DELETE FROM export_preset WHERE name = ?", (name,))

    # ---- scheduled exports ---------------------------------------------
    def add_export_schedule(self, preset_name: str, every_hours: int, at_hour: int) -> int:
        cur = self.execute(
            "INSERT INTO export_schedule(preset_name, every_hours, at_hour, enabled, created_at) "
            "VALUES (?, ?, ?, 1, ?)",
            (preset_name, every_hours, at_hour, now_iso()),
        )
        return cur.lastrowid

    def list_export_schedules(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM export_schedule ORDER BY created_at")

    def set_export_schedule(self, schedule_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE export_schedule SET {cols} WHERE id = ?",
                     (*fields.values(), schedule_id))

    def delete_export_schedule(self, schedule_id: int) -> None:
        self.execute("DELETE FROM export_schedule WHERE id = ?", (schedule_id,))
