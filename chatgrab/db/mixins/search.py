"""Full-text search over collected messages (FTS5), and the saved search
presets the search screen lets a user name and reload later.

_fts_query() also backs export_service's export_select() — see
mixins/export.py, which imports it from here rather than duplicating it.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..timeutil import now_iso


def _fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 MATCH query: quote each token as a
    phrase so punctuation in the source text can't break the query syntax,
    AND them together."""
    tokens = [t for t in text.strip().split() if t]
    escaped = [f'"{t.replace(chr(34), chr(34) * 2)}"' for t in tokens]
    return " AND ".join(escaped) if escaped else '""'


class SearchMixin:
    # ---- saved searches ----------------------------------------------
    def save_search_preset(self, name: str, params: dict[str, Any]) -> None:
        self.execute(
            "INSERT INTO search_preset(name, params, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET params = excluded.params",
            (name, json.dumps(params, ensure_ascii=False), now_iso()),
        )

    def list_search_presets(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM search_preset ORDER BY name")

    def delete_search_preset(self, name: str) -> None:
        self.execute("DELETE FROM search_preset WHERE name = ?", (name,))

    # ---- search (FTS5) -------------------------------------------------
    def search_messages(self, query: str = "", chat_id: int | None = None,
                         author: str = "", date_from: str | None = None,
                         date_to: str | None = None, photos_only: bool = False,
                         forwards_only: bool = False, replies_only: bool = False,
                         include_hidden: bool = False, sort_desc: bool = True,
                         page: int = 0, page_size: int = 100) -> tuple[list[sqlite3.Row], int]:
        clauses: list[str] = []
        params: list[Any] = []
        from_sql = "FROM messages m"
        if query.strip():
            from_sql = "FROM messages_fts f JOIN messages m ON m.id = f.rowid"
            clauses.append("messages_fts MATCH ?")
            params.append(_fts_query(query))
        if chat_id:
            clauses.append("m.chat_id = ?")
            params.append(chat_id)
        if author.strip():
            clauses.append("(m.sender_display_name LIKE ? OR m.sender_username LIKE ?)")
            like = f"%{author.strip()}%"
            params += [like, like]
        if date_from:
            clauses.append("m.date >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("m.date <= ?")
            params.append(date_to)
        if photos_only:
            clauses.append("m.media_type = 'photo' AND m.media_path IS NOT NULL AND m.media_path != ''")
        if forwards_only:
            clauses.append("m.is_forward = 1")
        if replies_only:
            clauses.append("m.is_reply = 1")
        if not include_hidden:
            clauses.append("m.is_hidden = 0")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        total = self.query_one(f"SELECT count(*) AS c {from_sql}{where}", params)["c"]
        order = "DESC" if sort_desc else "ASC"
        rows = self.query(
            f"SELECT m.* {from_sql}{where} ORDER BY m.date {order} LIMIT ? OFFSET ?",
            [*params, page_size, page * page_size],
        )
        return rows, total
