"""Which bot the «Боты» block is currently about.

Правила, Сценарий and Шаблоны each used to carry their own bot dropdown,
none of them aware of the others. You could be looking at one bot's rules
and another bot's scenario with nothing on screen saying so — and since
rules reference scenarios and templates, that is exactly the situation
where a wrong edit is easy and hard to notice.

The choice is made once, in the sidebar above the block's screens, and
every screen in the block reads it from here.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class BotSelection(QObject):
    changed = Signal(object)   # bot_id, or None when there are no bots

    def __init__(self) -> None:
        super().__init__()
        self._current: int | None = None

    @property
    def current(self) -> int | None:
        return self._current

    def set_current(self, bot_id: int | None) -> None:
        if bot_id == self._current:
            return
        self._current = bot_id
        self.changed.emit(bot_id)

    def ensure_valid(self, bots: list) -> int | None:
        """Keep the selection pointing at a bot that still exists — after a
        deletion, fall back to the first one rather than leaving screens
        bound to a missing id."""
        ids = [b["id"] for b in bots]
        if self._current not in ids:
            self.set_current(ids[0] if ids else None)
        return self._current
