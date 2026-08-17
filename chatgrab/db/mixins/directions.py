"""The direction catalogue — a flat list (see db/schema.py's _DDL_DIRECTION
comment for why this deliberately isn't a line-item hierarchy), plus
export/import so a catalogue can move between machines or survive a
rebuild."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..timeutil import now_iso


class DirectionsMixin:
    def add_direction(self, name: str, keywords: list[str] | None = None,
                      stop_words: list[str] | None = None, price_file: str | None = None,
                      note: str | None = None) -> int:
        order_index = (self.query_one(
            "SELECT COALESCE(MAX(order_index), -1) + 1 AS n FROM direction")["n"])
        cur = self.execute(
            "INSERT INTO direction(name, keywords, stop_words, price_file, note, "
            "enabled, order_index, created_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
            (name, json.dumps(keywords or [], ensure_ascii=False),
             json.dumps(stop_words or [], ensure_ascii=False), price_file, note,
             order_index, now_iso()),
        )
        return cur.lastrowid

    def list_directions(self, enabled_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM direction"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY order_index, id"
        return self.query(sql)

    def get_direction(self, direction_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM direction WHERE id = ?", (direction_id,))

    def update_direction(self, direction_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields = dict(fields)
        for list_field in ("keywords", "stop_words"):
            if list_field in fields and isinstance(fields[list_field], list):
                fields[list_field] = json.dumps(fields[list_field], ensure_ascii=False)
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE direction SET {cols} WHERE id = ?",
                     (*fields.values(), direction_id))

    def delete_direction(self, direction_id: int) -> None:
        self.execute("DELETE FROM direction WHERE id = ?", (direction_id,))

    def reorder_directions(self, ordered_ids: list[int]) -> None:
        """Rewrite order_index to match the given sequence — used by the
        ↑/↓ buttons on the screen, one full rewrite rather than a swap so
        it can't drift out of sync with what's actually on screen."""
        with self._lock:
            for index, direction_id in enumerate(ordered_ids):
                self._conn.execute(
                    "UPDATE direction SET order_index = ? WHERE id = ?", (index, direction_id))
            self._conn.commit()

    def export_directions(self) -> dict:
        """Plain JSON-able dict — id is left out on purpose: importing this
        elsewhere (or back into a rebuilt database) should create fresh
        rows, not collide with whatever ids happen to already exist."""
        directions = []
        for row in self.list_directions():
            directions.append({
                "name": row["name"],
                "keywords": json.loads(row["keywords"]),
                "stop_words": json.loads(row["stop_words"]),
                "price_file": row["price_file"],
                "note": row["note"],
                "enabled": bool(row["enabled"]),
            })
        return {"directions": directions}

    def import_directions(self, data: dict, replace: bool = False) -> int:
        """Load a previously exported catalogue. Malformed entries are
        skipped rather than aborting the whole import — one bad row in a
        hand-edited file shouldn't cost the rest. Returns how many were
        actually added.

        replace=True clears the existing catalogue first — the "start
        over from this file" path; the default just appends, for merging
        two machines' lists or restoring after an accidental delete.
        """
        directions = data.get("directions") if isinstance(data, dict) else None
        if not isinstance(directions, list):
            return 0
        if replace:
            self.execute("DELETE FROM direction")
        added = 0
        for entry in directions:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            keywords = entry.get("keywords")
            stop_words = entry.get("stop_words")
            self.add_direction(
                name.strip(),
                keywords=keywords if isinstance(keywords, list) else [],
                stop_words=stop_words if isinstance(stop_words, list) else [],
                price_file=entry.get("price_file") if isinstance(entry.get("price_file"), str) else None,
                note=entry.get("note") if isinstance(entry.get("note"), str) else None,
            )
            added += 1
        return added
