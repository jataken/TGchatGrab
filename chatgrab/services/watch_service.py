"""Watch list: be told when a phrase appears, without standing up a bot.

A bot is the right tool when something should *happen* — a reply sent, a
lead filed. It is the wrong tool when the user only wants to know. Setting
one up means a bot account or a userbot rule, a manager, sending limits;
all of that is overhead for "tell me if someone writes «куплю глицерин»".

This is the light path: phrases matched against messages as they arrive,
matches recorded so the list survives a restart, and a desktop
notification when the user asked for one. Nothing is ever sent anywhere.

Matching is substring, case-insensitive, over the same normalized form the
repeat detector uses, so «Куплю  ГЛИЦЕРИН» matches «куплю глицерин». A
phrase scoped to a chat only fires there.
"""
from __future__ import annotations

import logging

from ..db.database import Database
from ..db.dedup import normalize

_logger = logging.getLogger("chatgrab")


class WatchService:
    def __init__(self, db: Database, on_hit=None):
        self.db = db
        # (rule_row, message_record) -> None; the UI supplies this to raise
        # a tray notification. Kept as a callback so this module has no
        # dependency on Qt and stays testable without a display.
        self.on_hit = on_hit
        self._cache: list | None = None

    def invalidate(self) -> None:
        """Call after rules change — the matcher caches them, since it runs
        on every incoming message."""
        self._cache = None

    def _rules(self) -> list:
        if self._cache is None:
            self._cache = [
                (r, normalize(r["phrase"]))
                for r in self.db.list_watch_rules(enabled_only=True)
                if r["phrase"].strip()
            ]
        return self._cache

    def check(self, record: dict, notify: bool = True) -> list:
        """Match one stored message against the watch list. Returns the
        rules that fired, and records each hit.

        Safe to call on history backfill as well as live messages: a hit is
        unique per (rule, chat, message), so re-reading old history cannot
        produce a second alert for something already seen.

        `notify=False` records without alerting — used when scanning
        history in bulk, where one popup per match would bury the desktop.
        """
        rules = self._rules()
        if not rules:
            return []
        haystack = normalize(f"{record.get('text') or ''} {record.get('media_caption') or ''}")
        if not haystack:
            return []

        fired = []
        for rule, phrase in rules:
            if rule["chat_id"] is not None and rule["chat_id"] != record.get("chat_id"):
                continue
            if phrase and phrase in haystack:
                is_new = self.db.add_watch_hit(
                    rule["id"], record["chat_id"], record["message_id"],
                )
                if is_new:
                    fired.append(rule)
        if fired and notify and self.on_hit:
            for rule in fired:
                if rule["notify"]:
                    try:
                        self.on_hit(rule, record)
                    except Exception:
                        # A failing notifier must not stop collection.
                        _logger.warning("не удалось показать уведомление о совпадении",
                                        exc_info=True)
        return fired

    def rescan(self, limit: int = 20000) -> int:
        """Apply the current rules to already-collected messages. Used when
        a phrase is added after the fact — otherwise a new rule would only
        ever see the future, which is rarely what the user means."""
        rules = self._rules()
        if not rules:
            return 0
        found = 0
        rows = self.db.query(
            "SELECT chat_id, message_id, text, media_caption FROM messages "
            "ORDER BY date DESC LIMIT ?",
            (limit,),
        )
        for row in rows:
            hits = self.check({
                "chat_id": row["chat_id"], "message_id": row["message_id"],
                "text": row["text"], "media_caption": row["media_caption"],
            }, notify=False)
            found += len(hits)
        return found
