"""Scripted-dialog definitions (bot_scenarios) — see bots/scenario_engine.py
for how the steps this mixin stores actually get walked at runtime.

scenario_usage() lived physically next to the templates section in the old
single-file database.py (it was written right before bot_templates'
delete_template()), but it answers a scenario question ("what breaks if
this scenario is deleted"), not a template one — it moved here rather than
into mixins/templates.py."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..timeutil import now_iso


def _with_step_ids(steps: list[dict]) -> list[dict]:
    """Give every scenario step a stable `id`, assigned once and preserved
    across edits.

    The engine walks steps by position and doesn't read this yet. It exists
    so that when branching lands, a jump can name its destination by id
    instead of by index — otherwise inserting a step in the middle of a
    live scenario would silently repoint every existing branch, and fixing
    that later would mean migrating real customer data. Cheap now,
    expensive to retrofit.
    """
    used = {s["id"] for s in steps if isinstance(s, dict) and s.get("id")}
    out = []
    for step in steps:
        step = dict(step)
        if not step.get("id"):
            n = len(used) + 1
            while f"s{n}" in used:
                n += 1
            step["id"] = f"s{n}"
            used.add(step["id"])
        out.append(step)
    return out


class ScenariosMixin:
    def add_scenario(self, bot_id: int, name: str, steps: list[dict]) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO bot_scenarios(bot_id, name, steps, created_at) VALUES (?, ?, ?, ?)",
                (bot_id, name, json.dumps(_with_step_ids(steps), ensure_ascii=False), now_iso()),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_scenario(self, scenario_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM bot_scenarios WHERE id = ?", (scenario_id,))

    def list_scenarios(self, bot_id: int) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM bot_scenarios WHERE bot_id = ? ORDER BY created_at", (bot_id,))

    def update_scenario(self, scenario_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields = dict(fields)
        if "steps" in fields and isinstance(fields["steps"], list):
            fields["steps"] = json.dumps(_with_step_ids(fields["steps"]), ensure_ascii=False)
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE bot_scenarios SET {cols} WHERE id = ?", (*fields.values(), scenario_id))

    def scenario_usage(self, scenario_id: int) -> dict[str, int]:
        """Actions that launch this scenario, plus how many contacts are
        part-way through it right now — losing those mid-dialog is the
        part a user is least likely to expect."""
        actions = self.query(
            "SELECT id FROM bot_actions WHERE json_extract(config, '$.scenario_id') = ?",
            (scenario_id,),
        )
        active = self.query(
            "SELECT id FROM bot_scenario_sessions WHERE scenario_id = ? AND status = 'active'",
            (scenario_id,),
        )
        return {"actions": len(actions), "active_dialogs": len(active)}

    def delete_scenario(self, scenario_id: int) -> None:
        self.execute("DELETE FROM bot_scenarios WHERE id = ?", (scenario_id,))
