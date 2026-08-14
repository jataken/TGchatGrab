"""Running a saved export on its own, so a fresh file is simply there.

Every piece this needs already existed — presets hold the parameters, the
export log records what went out, and incremental mode knows what is new
since last time. All that was missing was something to press the button.

A schedule is "run this preset every N hours, at around this hour of the
day". Not cron: the app is a desktop program that is not always running,
so the question that actually matters is «прошло ли достаточно времени с
прошлого раза», which survives the machine being asleep at the appointed
minute. Cron semantics would silently skip.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging

from ..db.database import Database
from .export_service import ExportParams, ExportService

_logger = logging.getLogger("chatgrab")

TICK_SECONDS = 600  # 10 минут — расписания измеряются часами


class ExportScheduleService:
    def __init__(self, db: Database, export_service: ExportService, on_log=None):
        self.db = db
        self.export_service = export_service
        self.on_log = on_log or (lambda text, tone="": None)
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
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.warning("плановая выгрузка: сбой тика", exc_info=True)

    def due(self, schedule, now: dt.datetime | None = None) -> bool:
        now = now or dt.datetime.now()
        if not schedule["enabled"]:
            return False
        last = schedule["last_run_at"]
        if not last:
            # Never run: wait for the configured hour rather than firing
            # the moment the schedule is created.
            return now.hour >= schedule["at_hour"]
        try:
            last_dt = dt.datetime.fromisoformat(last)
        except (ValueError, TypeError):
            return True
        return (now - last_dt).total_seconds() >= schedule["every_hours"] * 3600

    async def tick(self, now: dt.datetime | None = None) -> int:
        now = now or dt.datetime.now()
        ran = 0
        for schedule in self.db.list_export_schedules():
            if not self.due(schedule, now):
                continue
            try:
                await self._run_one(schedule, now)
                ran += 1
            except Exception as e:
                self.db.set_export_schedule(
                    schedule["id"], last_run_at=now.isoformat(),
                    last_result=f"ошибка: {e}",
                )
                self.on_log(f"плановая выгрузка «{schedule['preset_name']}» не удалась: {e}", "warn")
        return ran

    async def _run_one(self, schedule, now: dt.datetime) -> None:
        preset = next(
            (p for p in self.db.list_presets() if p["name"] == schedule["preset_name"]), None
        )
        if preset is None:
            raise ValueError("пресет удалён")
        params = ExportParams(**json.loads(preset["params"]))

        # Off the event loop: a large export blocks Qt, Telethon and every
        # running bot for as long as it takes to write.
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: self.export_service.run(params))

        files = len(result.output_paths)
        self.db.set_export_schedule(
            schedule["id"], last_run_at=now.isoformat(),
            last_result=f"готово: {files} файл(ов), {result.row_count} сообщений",
        )
        self.on_log(
            f"плановая выгрузка «{schedule['preset_name']}» готова: "
            f"{files} файл(ов), {result.row_count} сообщений", "ok"
        )
