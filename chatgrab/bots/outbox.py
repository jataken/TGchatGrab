"""The one gate every outbound message passes through, regardless of what
triggered it — a matched rule, a scenario question, a scheduled/inactivity
reminder, or a human clicking "send" on a draft.

`Outbox.wrap(bot_id, raw_send, reactive=...)` takes a runner's own send
callable (Telethon's or aiogram's — see userbot_runner.make_send and
BotApiRunner.send_dm) and returns another send_dm-shaped callable that
applies every limit below before ever calling the real one. This is
"insert a layer in front of it," per PLAN.md С4 — the two runners and
BotManager change by one call each (see their own diffs), nothing about
how they actually talk to Telegram changes.

`reactive` is the one piece of context callers must supply, since it
can't be inferred from a bare (target, text) pair: it means "this send is
a direct reply to something the contact just did" (an inbound-triggered
rule or scenario step) as opposed to "nothing from them prompted this"
(a schedule/inactivity trigger, or resending a draft by hand). Only
reactive sends may open a conversation with a stranger outright — a
proactive first message to someone who's never heard from this account
defaults to a draft (invariant 6), and only proactive sends respect quiet
hours and the persistent per-contact cooldown, since neither makes sense
for "they just wrote to us."

FloodWait handling stays exactly where it already was: userbot_runner and
BotApiRunner each retry in place and extend their own pause, which this
layer doesn't need to duplicate — it only decides whether raw_send gets
called at all.
"""
from __future__ import annotations

import datetime as dt
import logging

from ..db.database import Database
from . import settings as bot_settings
from .scheduler import _parse_hhmm

_logger = logging.getLogger("chatgrab")


def _within_send_window(limits: dict, now: dt.datetime) -> bool:
    if limits["quiet_weekends"] and now.weekday() >= 5:
        return False
    start = _parse_hhmm(str(limits["quiet_start"]), dt.time(0, 0))
    end = _parse_hhmm(str(limits["quiet_end"]), dt.time(23, 59))
    t = now.time()
    if start <= end:
        return start <= t <= end
    return t >= start or t <= end  # window crosses midnight


class Outbox:
    def __init__(self, db: Database, on_log=None):
        self.db = db
        self.on_log = on_log or (lambda bot_id, text, tone="": None)

    def wrap(self, bot_id: int, raw_send, *, reactive: bool):
        async def send_dm(target: int | str, text: str) -> None:
            if not text:
                return
            key = str(target)
            # Aware, matching db.now_iso() — last_outbox_send() returns
            # that same format, and subtracting a naive "now" from it
            # raises TypeError rather than comparing wrong, so this isn't
            # optional the way it might look.
            now = dt.datetime.now().astimezone()

            if self.db.is_blacklisted(bot_id, key):
                self.on_log(bot_id, f"пропущено сообщение {target} — контакт в чёрном списке outbox", "warn")
                self.db.log_outbox(bot_id, key, "blocked", text)
                return

            limits = bot_settings.load(self.db.get_bot(bot_id))

            if limits["dry_run"]:
                self.on_log(bot_id, f"пробный режим — сообщение {target} не отправлено", "")
                self.db.log_outbox(bot_id, key, "dry_run", text)
                return

            is_first = self.db.last_outbox_send(bot_id, key) is None

            if not reactive:
                if not _within_send_window(limits, now):
                    self.on_log(bot_id, f"пропущено сообщение {target} — тихие часы", "warn")
                    self.db.log_outbox(bot_id, key, "blocked", text)
                    return
                if not is_first:
                    days = limits["contact_cooldown_days"]
                    last = self.db.last_outbox_send(bot_id, key)
                    if days and last and (now - dt.datetime.fromisoformat(last)).days < days:
                        self.on_log(bot_id, f"пропущено сообщение {target} — контакту уже писали недавно", "warn")
                        self.db.log_outbox(bot_id, key, "blocked", text)
                        return
                if is_first and not limits["auto_send_cold"]:
                    self.db.add_draft(bot_id, key, text, reason="первое сообщение контакту")
                    self.on_log(bot_id, f"первое сообщение {target} отложено в черновики — нужен клик человека", "")
                    return

            if is_first:
                since = (now - dt.timedelta(days=1)).isoformat()
                first_today = self.db.outbox_count_since(bot_id, since, first_only=True)
                if first_today >= limits["max_first_messages_per_day"]:
                    self.on_log(bot_id, f"пропущено сообщение {target} — превышен дневной лимит "
                                        "первых сообщений новым контактам", "warn")
                    self.db.log_outbox(bot_id, key, "blocked", text)
                    return

            hour_count = self.db.outbox_count_since(bot_id, (now - dt.timedelta(hours=1)).isoformat())
            if hour_count >= limits["max_per_hour"]:
                self.on_log(bot_id, f"пропущено сообщение {target} — превышен лимит {limits['max_per_hour']} "
                                    "в час", "warn")
                self.db.log_outbox(bot_id, key, "blocked", text)
                return
            day_count = self.db.outbox_count_since(bot_id, (now - dt.timedelta(days=1)).isoformat())
            if day_count >= limits["max_per_day"]:
                self.on_log(bot_id, f"пропущено сообщение {target} — превышен лимит {limits['max_per_day']} "
                                    "в сутки", "warn")
                self.db.log_outbox(bot_id, key, "blocked", text)
                return

            await raw_send(target, text)
            self.db.log_outbox(bot_id, key, "sent", text, is_first=is_first)

        return send_dm
