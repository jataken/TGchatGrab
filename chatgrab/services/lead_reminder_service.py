"""Firing a lead's next_action_at reminder into the tray, once, at the
assigned time.

Same shape as ExportScheduleService: a background asyncio tick loop, no
cron matching — the app isn't always running, so "is now past the
assigned time" (survives a sleeping machine) is what matters, not "is it
exactly that minute". due_lead_reminders()/fire_lead_reminder() do the
actual work; this is just the loop that calls them and tells someone.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from ..db.database import Database

_logger = logging.getLogger("chatgrab")

TICK_SECONDS = 60  # reminders are assigned to a specific minute


class LeadReminderService:
    def __init__(self, db: Database, on_fire=None):
        self.db = db
        # on_fire(lead_row) — the caller's job to turn that into a tray
        # notification (see ui/main_window.py); a no-op default keeps
        # this importable and testable without any UI at all.
        self.on_fire = on_fire or (lambda lead: None)
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._loop())

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(TICK_SECONDS)
                self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.warning("напоминания по заявкам: сбой тика", exc_info=True)

    def tick(self, now: dt.datetime | None = None) -> int:
        now = now or dt.datetime.now()
        # Same format as db.now_iso() (aware, seconds precision) — comparing
        # against a naive or microsecond-precision string here would sort
        # wrong against what next_action_at actually has stored in it.
        now_iso_str = now.astimezone().isoformat(timespec="seconds")
        due = self.db.due_lead_reminders(now_iso_str)
        for lead in due:
            self.db.fire_lead_reminder(lead["id"])
            self.on_fire(lead)
        return len(due)
