"""Background evaluation of the triggers that aren't driven by an incoming
message: «неактивность контакта N дней» and «расписание».

Both trigger types were configurable from day one and stored fine, but
nothing ever evaluated them — RulesEngine.matches() returns False for both
by design, because they don't belong on the per-message path. This is the
tick that was missing: a bot set to nudge silent contacts simply never
did, with no error anywhere to explain it.

Design notes:

- One shared task for every bot, not one per bot: these fire on the order
  of days, and each wake-up is a couple of indexed queries.
- Firing is recorded in bot_activity_log so a reminder goes out once per
  contact per trigger, not on every tick — without that, a 15-minute tick
  would message a silent contact 96 times a day.
- Sends still go through the runner's own send path, so the userbot's
  per-contact cooldown and FloodWait handling apply unchanged.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging

from ..db.database import Database
from . import settings as bot_settings
from .rules_engine import IncomingEvent, RulesEngine

_logger = logging.getLogger("chatgrab")

TICK_SECONDS = 900  # 15 minutes — these triggers work in days and hours
REMINDER_KIND = "reminder_sent"
SCHEDULE_KIND = "schedule_fired"


def _cfg(row) -> dict:
    try:
        return json.loads(row["config"])
    except (json.JSONDecodeError, TypeError):
        return {}


def _parse_hhmm(value: str, default: dt.time) -> dt.time:
    try:
        hh, mm = value.split(":")
        return dt.time(int(hh), int(mm))
    except (ValueError, AttributeError):
        return default


class TriggerScheduler:
    """Owns the periodic tick. `send_for_bot` is supplied by BotManager so
    this doesn't need to know which runner a bot uses."""

    def __init__(self, db: Database, rules: RulesEngine, send_for_bot, on_log,
                 tick_seconds: int = TICK_SECONDS):
        self.db = db
        self.rules = rules
        self._send_for_bot = send_for_bot   # (bot_id) -> async send(target, text)
        self._on_log = on_log               # (bot_id, text, tone) -> None
        self.tick_seconds = tick_seconds
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
                await asyncio.sleep(self.tick_seconds)
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A bad trigger config or a transient DB error must not kill
                # the tick — otherwise every later reminder silently stops,
                # which is the exact failure this module exists to fix.
                _logger.warning("trigger scheduler tick failed", exc_info=True)

    async def tick(self, now: dt.datetime | None = None) -> int:
        """One evaluation pass. Returns how many triggers fired — handy for
        tests and for the log line the user sees."""
        now = now or dt.datetime.now()
        fired = 0
        for bot in self.db.list_bots():
            if bot["status"] != "running":
                continue
            bot_id = bot["id"]
            for trigger in self.db.list_triggers(bot_id):
                if not trigger["enabled"]:
                    continue
                try:
                    if trigger["type"] == "inactivity":
                        fired += await self._run_inactivity(bot_id, trigger, now)
                    elif trigger["type"] == "schedule":
                        fired += await self._run_schedule(bot_id, trigger, now)
                except Exception as e:
                    self._on_log(bot_id, f"фоновый триггер завершился ошибкой: {e}", "warn")
        return fired

    # ---- inactivity -------------------------------------------------------
    async def _run_inactivity(self, bot_id: int, trigger, now: dt.datetime) -> int:
        cfg = _cfg(trigger)
        days = cfg.get("days")
        if not isinstance(days, int) or days <= 0:
            return 0
        cutoff = (now - dt.timedelta(days=days)).isoformat()
        contacts = self.db.contacts_silent_since(bot_id, cutoff)
        if not contacts:
            return 0

        send = self._send_for_bot(bot_id)
        limits = bot_settings.load(self.db.get_bot(bot_id))
        cap = int(limits["max_reminders_per_tick"])
        fired = 0
        skipped_by_cap = 0

        for contact in contacts:
            # Already nudged since they last spoke? Then this trigger has
            # done its job for this silence; don't repeat every tick.
            if self.db.has_activity_since(contact["id"], REMINDER_KIND, contact["last_active"]):
                continue
            if fired >= cap:
                # Leave the rest for the next tick rather than emptying a
                # backlog of hundreds in one go — the account sending them
                # is an ordinary user account, not a bot.
                skipped_by_cap += 1
                continue
            event = IncomingEvent(
                contact_telegram_id=contact["telegram_id"],
                username=contact["username"],
                text="",
                chat_type="dm",
            )
            await self.rules.fire(bot_id, trigger, event, send,
                                  log=lambda t, tone="": self._on_log(bot_id, t, tone))
            self.db.log_activity(contact["id"], bot_id, None, None, "dm", kind=REMINDER_KIND)
            fired += 1

        if fired:
            note = (f"напоминание отправлено {fired} молчащим контактам "
                    f"(тишина дольше {days} сут.)")
            if skipped_by_cap:
                note += f"; ещё {skipped_by_cap} — в следующий заход, чтобы не слать пачкой"
            self._on_log(bot_id, note, "ok")
        return fired

    # ---- schedule ---------------------------------------------------------
    async def _run_schedule(self, bot_id: int, trigger, now: dt.datetime) -> int:
        """Fires once per day, on the configured weekdays, at or after the
        configured time. `days` is a list of weekday numbers (0 = Monday),
        matching how the collector's own schedule window is stored."""
        cfg = _cfg(trigger)
        at = _parse_hhmm(cfg.get("at", "10:00"), dt.time(10, 0))
        weekdays = cfg.get("days")
        if weekdays and now.weekday() not in weekdays:
            return 0
        if now.time() < at:
            return 0

        # Already fired today?
        today_start = dt.datetime.combine(now.date(), dt.time.min).isoformat()
        if self.db.has_trigger_activity_since(bot_id, SCHEDULE_KIND, today_start):
            return 0

        send = self._send_for_bot(bot_id)
        event = IncomingEvent(contact_telegram_id=0, username=None, text="", chat_type="dm")
        await self.rules.fire(bot_id, trigger, event, send,
                              log=lambda t, tone="": self._on_log(bot_id, t, tone))
        self.db.log_activity(None, bot_id, None, None, "dm", kind=SCHEDULE_KIND)
        self._on_log(bot_id, "сработал триггер по расписанию", "ok")
        return 1
