"""Author/stopword rules that hide already-collected messages — never
delete, only flip is_hidden, so applying a rule is always undoable."""
from __future__ import annotations

import sqlite3
from typing import Any

from ..timeutil import now_iso


class IgnoreRulesMixin:
    def add_ignore_rule(self, rule_type: str, value: str, scope: str, chat_id: int | None) -> None:
        self.execute(
            "INSERT INTO ignore_rule(rule_type, value, scope, chat_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (rule_type, value, scope, chat_id, now_iso()),
        )

    def delete_ignore_rule(self, rule_id: int) -> None:
        self.execute("DELETE FROM ignore_rule WHERE id = ?", (rule_id,))

    def list_ignore_rules(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM ignore_rule ORDER BY created_at DESC")

    def set_message_hidden(self, chat_id: int, message_id: int, hidden: bool) -> None:
        self.execute(
            "UPDATE messages SET is_hidden = ? WHERE chat_id = ? AND message_id = ?",
            (1 if hidden else 0, chat_id, message_id),
        )

    def apply_ignore_rules(self, rules: list[sqlite3.Row]) -> int:
        """Mark existing messages matching author/stopword rules as hidden.
        Never deletes — only sets is_hidden, so it can be undone."""
        total = 0
        with self._lock:
            for r in rules:
                if r["rule_type"] == "author":
                    sql = "UPDATE messages SET is_hidden = 1 WHERE is_hidden = 0 AND (sender_username = ? OR sender_display_name = ?)"
                    params: list[Any] = [r["value"], r["value"]]
                else:
                    sql = "UPDATE messages SET is_hidden = 1 WHERE is_hidden = 0 AND text LIKE ?"
                    params = [f"%{r['value']}%"]
                if r["scope"] == "chat" and r["chat_id"] is not None:
                    sql += " AND chat_id = ?"
                    params.append(r["chat_id"])
                cur = self._conn.execute(sql, params)
                total += cur.rowcount
            self._conn.commit()
        return total
