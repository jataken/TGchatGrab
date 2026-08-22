"""A bot's own configuration: the bot row itself, its triggers, and the
actions each trigger fires — everything ui/screens/bots/rules_tab.py and
friends edit. Leads, contacts and activity history are a separate mixin
(see mixins/leads.py) — delete_bot() below removes this bot's config, but
deliberately leaves those alone, since they're records of real
conversations, not bot configuration."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..timeutil import now_iso


class BotsMixin:
    # ---- bots -----------------------------------------------------------
    def add_bot(self, name: str, type_: str, token_encrypted: str | None,
                preset: str = "custom", manager_chat_id: str | None = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO bots(name, type, token_encrypted, preset, manager_chat_id,
                       status, created_at)
                   VALUES (?, ?, ?, ?, ?, 'stopped', ?)""",
                (name, type_, token_encrypted, preset, manager_chat_id, now_iso()),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_bot(self, bot_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM bots WHERE id = ?", (bot_id,))

    def list_bots(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM bots ORDER BY created_at")

    def set_bot_field(self, bot_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE bots SET {cols} WHERE id = ?", (*fields.values(), bot_id))

    def delete_bot(self, bot_id: int) -> None:
        """Removes the bot's own config (triggers/actions/scenarios/
        templates/scenario sessions). Leads, contacts and activity history
        are kept — they're shared records of real conversations, not bot
        configuration, so they outlive the bot that created them."""
        with self._lock:
            trigger_ids = [r["id"] for r in self._conn.execute(
                "SELECT id FROM bot_triggers WHERE bot_id = ?", (bot_id,)).fetchall()]
            for tid in trigger_ids:
                self._conn.execute("DELETE FROM bot_actions WHERE trigger_id = ?", (tid,))
            self._conn.execute("DELETE FROM bot_triggers WHERE bot_id = ?", (bot_id,))
            self._conn.execute("DELETE FROM bot_scenarios WHERE bot_id = ?", (bot_id,))
            self._conn.execute("DELETE FROM bot_scenario_sessions WHERE bot_id = ?", (bot_id,))
            self._conn.execute("DELETE FROM bot_templates WHERE bot_id = ?", (bot_id,))
            self._conn.execute("DELETE FROM bots WHERE id = ?", (bot_id,))
            self._conn.commit()

    # ---- bot triggers / actions ------------------------------------------
    def add_trigger(self, bot_id: int, type_: str, config: dict, enabled: bool = True) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO bot_triggers(bot_id, type, config, enabled, created_at) VALUES (?, ?, ?, ?, ?)",
                (bot_id, type_, json.dumps(config, ensure_ascii=False), 1 if enabled else 0, now_iso()),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_trigger(self, trigger_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM bot_triggers WHERE id = ?", (trigger_id,))

    def list_triggers(self, bot_id: int, type_: str | None = None) -> list[sqlite3.Row]:
        if type_ is not None:
            return self.query(
                "SELECT * FROM bot_triggers WHERE bot_id = ? AND type = ? AND enabled = 1 ORDER BY id",
                (bot_id, type_),
            )
        return self.query("SELECT * FROM bot_triggers WHERE bot_id = ? ORDER BY id", (bot_id,))

    def set_trigger_field(self, trigger_id: int, **fields: Any) -> None:
        if not fields:
            return
        if "config" in fields and isinstance(fields["config"], dict):
            fields["config"] = json.dumps(fields["config"], ensure_ascii=False)
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE bot_triggers SET {cols} WHERE id = ?", (*fields.values(), trigger_id))

    def delete_trigger(self, trigger_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM bot_actions WHERE trigger_id = ?", (trigger_id,))
            self._conn.execute("DELETE FROM bot_triggers WHERE id = ?", (trigger_id,))
            self._conn.commit()

    def add_action(self, trigger_id: int, type_: str, config: dict, order_index: int = 0) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO bot_actions(trigger_id, type, config, order_index) VALUES (?, ?, ?, ?)",
                (trigger_id, type_, json.dumps(config, ensure_ascii=False), order_index),
            )
            self._conn.commit()
            return cur.lastrowid

    def list_actions(self, trigger_id: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM bot_actions WHERE trigger_id = ? ORDER BY order_index, id", (trigger_id,)
        )

    def set_action_field(self, action_id: int, **fields: Any) -> None:
        if not fields:
            return
        if "config" in fields and isinstance(fields["config"], dict):
            fields["config"] = json.dumps(fields["config"], ensure_ascii=False)
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE bot_actions SET {cols} WHERE id = ?", (*fields.values(), action_id))

    def delete_action(self, action_id: int) -> None:
        self.execute("DELETE FROM bot_actions WHERE id = ?", (action_id,))
