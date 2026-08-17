"""Reusable message templates with {variables} — see bots/templating.py
for how a template's text actually gets rendered."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..timeutil import now_iso


class TemplatesMixin:
    def add_template(self, bot_id: int | None, name: str, text: str, variables: list[str]) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO bot_templates(bot_id, name, text, variables, created_at) VALUES (?, ?, ?, ?, ?)",
                (bot_id, name, text, json.dumps(variables, ensure_ascii=False), now_iso()),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_template(self, template_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM bot_templates WHERE id = ?", (template_id,))

    def list_templates(self, bot_id: int | None = None) -> list[sqlite3.Row]:
        if bot_id is not None:
            return self.query(
                "SELECT * FROM bot_templates WHERE bot_id = ? ORDER BY created_at", (bot_id,)
            )
        return self.query("SELECT * FROM bot_templates ORDER BY created_at")

    def update_template(self, template_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields = dict(fields)
        if "variables" in fields and isinstance(fields["variables"], list):
            fields["variables"] = json.dumps(fields["variables"], ensure_ascii=False)
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE bot_templates SET {cols} WHERE id = ?", (*fields.values(), template_id))

    def template_usage(self, template_id: int) -> dict[str, int]:
        """What would break if this template were deleted: actions that
        send it, and scenarios that use it as their closing message.
        Referenced from JSON config, so there is no foreign key to lean on
        — this is the only way to warn before the fact instead of showing
        a broken reference afterwards."""
        actions = self.query(
            "SELECT id FROM bot_actions WHERE json_extract(config, '$.template_id') = ?",
            (template_id,),
        )
        scenarios = self.query(
            "SELECT id FROM bot_scenarios WHERE done_template_id = ?", (template_id,)
        )
        return {"actions": len(actions), "scenarios": len(scenarios)}

    def delete_template(self, template_id: int) -> None:
        self.execute("DELETE FROM bot_templates WHERE id = ?", (template_id,))
