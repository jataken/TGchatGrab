"""Ignore rules: authors or stop-words, global or per-chat. Rules apply to
newly collected messages automatically (checked in Collector before
insert) and, on request, can be applied retroactively — which only marks
existing rows as hidden, never deletes them, so it's reversible."""
from __future__ import annotations

from ..db.database import Database


class IgnoreService:
    def __init__(self, db: Database):
        self.db = db

    def add_rule(self, rule_type: str, value: str, scope: str = "global",
                 chat_id: int | None = None) -> None:
        value = value.strip()
        if not value:
            return
        self.db.add_ignore_rule(rule_type, value, scope, chat_id if scope == "chat" else None)

    def remove_rule(self, rule_id: int) -> None:
        self.db.delete_ignore_rule(rule_id)

    def list_rules(self):
        return self.db.list_ignore_rules()

    def matches(self, chat_id: int, sender_username: str | None,
                sender_display_name: str | None, text: str) -> bool:
        for r in self.db.list_ignore_rules():
            if r["scope"] == "chat" and r["chat_id"] != chat_id:
                continue
            if r["rule_type"] == "author":
                if r["value"] == sender_username or r["value"] == sender_display_name:
                    return True
            else:  # stopword
                if r["value"].lower() in (text or "").lower():
                    return True
        return False

    def apply_to_existing(self) -> int:
        """Mark already-collected messages matching current rules as
        hidden. Returns the number of rows affected."""
        return self.db.apply_ignore_rules(self.db.list_ignore_rules())
