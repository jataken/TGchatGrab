"""FSM state for an in-flight (or finished) scripted dialog — see
db/schema.py's _DDL_BOT_SCENARIO_SESSIONS comment for why uniqueness here
is a partial index (one *active* dialog per contact) rather than a plain
column constraint.

scenario_funnel() below calls self.get_scenario() — a ScenariosMixin
method, from mixins/scenarios.py — which works because both mixins end up
on the same Database instance (same pattern as mixins/productivity.py
calling self.chat_storage() from mixins/retention.py, see its docstring)."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..timeutil import now_iso


class ScenarioSessionsMixin:
    def last_finished_session(self, bot_id: int, contact_telegram_id: int) -> sqlite3.Row | None:
        """The most recently completed run for this contact — used to find
        which scenario they just finished, since the active session row is
        already marked done by the time the confirmation is sent."""
        return self.query_one(
            "SELECT * FROM bot_scenario_sessions WHERE bot_id = ? AND contact_telegram_id = ? "
            "AND status = 'done' ORDER BY updated_at DESC, id DESC LIMIT 1",
            (bot_id, contact_telegram_id),
        )

    def scenario_funnel(self, scenario_id: int) -> list[dict]:
        """How far contacts got through a scenario: for each step, how many
        runs reached it and how many stopped there.

        Reads the accumulated session history — which only became possible
        once schema v3 stopped collapsing every contact to one row per
        status. `step_index` is where a run stopped, so a run that reached
        step N passed through every step before it."""
        scenario = self.get_scenario(scenario_id)
        if not scenario:
            return []
        steps = json.loads(scenario["steps"])
        if not steps:
            return []
        rows = self.query(
            "SELECT step_index, status FROM bot_scenario_sessions WHERE scenario_id = ?",
            (scenario_id,),
        )
        total = len(rows)
        funnel = []
        for i, step in enumerate(steps):
            reached = sum(1 for r in rows if r["step_index"] >= i or r["status"] == "done")
            dropped = sum(
                1 for r in rows
                if r["status"] in ("abandoned", "active") and r["step_index"] == i
            )
            funnel.append({
                "index": i,
                "question": step.get("question", ""),
                "field": step.get("field", ""),
                "reached": reached,
                "dropped": dropped,
                "share": (reached / total) if total else 0.0,
            })
        return funnel

    def get_active_scenario_session(self, bot_id: int, contact_telegram_id: int) -> sqlite3.Row | None:
        return self.query_one(
            "SELECT * FROM bot_scenario_sessions WHERE bot_id = ? AND contact_telegram_id = ? AND status = 'active'",
            (bot_id, contact_telegram_id),
        )

    def start_scenario_session(self, bot_id: int, scenario_id: int, contact_telegram_id: int,
                                step_id: str | None = None) -> int:
        with self._lock:
            now = now_iso()
            self._conn.execute(
                "UPDATE bot_scenario_sessions SET status = 'abandoned', updated_at = ? "
                "WHERE bot_id = ? AND contact_telegram_id = ? AND status = 'active'",
                (now, bot_id, contact_telegram_id),
            )
            cur = self._conn.execute(
                """INSERT INTO bot_scenario_sessions(bot_id, scenario_id, contact_telegram_id,
                       step_index, step_id, answers, status, started_at, updated_at)
                   VALUES (?, ?, ?, 0, ?, '{}', 'active', ?, ?)""",
                (bot_id, scenario_id, contact_telegram_id, step_id, now, now),
            )
            self._conn.commit()
            return cur.lastrowid

    def update_scenario_session(self, session_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields = dict(fields)
        if "answers" in fields and isinstance(fields["answers"], dict):
            fields["answers"] = json.dumps(fields["answers"], ensure_ascii=False)
        fields.setdefault("updated_at", now_iso())
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE bot_scenario_sessions SET {cols} WHERE id = ?", (*fields.values(), session_id))
