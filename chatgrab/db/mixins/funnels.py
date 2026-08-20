"""С10: the funnel catalogue — `funnel` and `funnel_stage`, a configurable
replacement for the flat status vocabulary core/lead.py used to hardcode.
Same flat-list-plus-order_index shape as directions.py's catalogue,
including its ↑/↓ reorder() pattern (a full order_index rewrite from the
screen's current order, not a swap — can't drift out of sync with what's
on screen)."""
from __future__ import annotations

import sqlite3
from typing import Any

from ..timeutil import now_iso


class FunnelsMixin:
    # ---- funnels -------------------------------------------------------
    def create_funnel(self, name: str, channel: str) -> int:
        order_index = (self.query_one(
            "SELECT COALESCE(MAX(order_index), -1) + 1 AS n FROM funnel")["n"])
        cur = self.execute(
            "INSERT INTO funnel(name, channel, order_index, created_at) VALUES (?, ?, ?, ?)",
            (name.strip(), channel, order_index, now_iso()),
        )
        return cur.lastrowid

    def list_funnels(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM funnel ORDER BY order_index, id")

    def get_funnel(self, funnel_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM funnel WHERE id = ?", (funnel_id,))

    def default_funnel_id(self) -> int | None:
        """The funnel a new lead lands in when nothing more specific is
        given — the first one by order_index, i.e. the seeded "Телеграм ·
        биржа" funnel on every install until a second one exists. None
        only on a database with zero funnels, which shouldn't happen
        past migration 013, but add_lead() treats it as "leave funnel_id
        NULL" rather than raising, since a lead is still worth keeping
        even in that state."""
        row = self.query_one("SELECT id FROM funnel ORDER BY order_index, id LIMIT 1")
        return row["id"] if row else None

    def update_funnel(self, funnel_id: int, **fields: Any) -> None:
        cols = {k: v for k, v in fields.items() if k in ("name", "channel", "order_index")}
        if not cols:
            return
        if "name" in cols:
            cols["name"] = cols["name"].strip()
        set_clause = ", ".join(f"{k} = ?" for k in cols)
        self.execute(f"UPDATE funnel SET {set_clause} WHERE id = ?", (*cols.values(), funnel_id))

    # ---- stages ----------------------------------------------------------
    def create_funnel_stage(self, funnel_id: int, code: str, label: str, kind: str = "open",
                             requires_reason: bool = False, color_bg: str = "rgba(145,132,217,46)",
                             color_fg: str = "#d2cefd", color_dot: str = "#b5abfc") -> int:
        order_index = (self.query_one(
            "SELECT COALESCE(MAX(order_index), -1) + 1 AS n FROM funnel_stage WHERE funnel_id = ?",
            (funnel_id,))["n"])
        cur = self.execute(
            "INSERT INTO funnel_stage"
            "(funnel_id, code, label, kind, order_index, requires_reason, color_bg, color_fg, color_dot) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (funnel_id, code.strip(), label.strip(), kind, order_index,
             1 if requires_reason else 0, color_bg, color_fg, color_dot),
        )
        return cur.lastrowid

    def list_funnel_stages(self, funnel_id: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM funnel_stage WHERE funnel_id = ? ORDER BY order_index, id", (funnel_id,))

    def get_funnel_stage(self, stage_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM funnel_stage WHERE id = ?", (stage_id,))

    def get_funnel_stage_by_code(self, funnel_id: int, code: str) -> sqlite3.Row | None:
        return self.query_one(
            "SELECT * FROM funnel_stage WHERE funnel_id = ? AND code = ?", (funnel_id, code))

    def update_funnel_stage(self, stage_id: int, **fields: Any) -> None:
        cols = {k: v for k, v in fields.items()
                if k in ("code", "label", "kind", "requires_reason", "color_bg", "color_fg", "color_dot")}
        if not cols:
            return
        if "code" in cols:
            cols["code"] = cols["code"].strip()
        if "label" in cols:
            cols["label"] = cols["label"].strip()
        if "requires_reason" in cols:
            cols["requires_reason"] = 1 if cols["requires_reason"] else 0
        set_clause = ", ".join(f"{k} = ?" for k in cols)
        self.execute(f"UPDATE funnel_stage SET {set_clause} WHERE id = ?", (*cols.values(), stage_id))

    def delete_funnel_stage(self, stage_id: int) -> None:
        self.execute("DELETE FROM funnel_stage WHERE id = ?", (stage_id,))

    def reorder_funnel_stages(self, funnel_id: int, ordered_ids: list[int]) -> None:
        """Rewrite order_index to match the given sequence — the ↑/↓
        buttons' full rewrite, same as directions.py's reorder_directions
        (see that method's docstring for why not a swap)."""
        with self._lock:
            for index, stage_id in enumerate(ordered_ids):
                self._conn.execute(
                    "UPDATE funnel_stage SET order_index = ? WHERE id = ? AND funnel_id = ?",
                    (index, stage_id, funnel_id))
            self._conn.commit()
