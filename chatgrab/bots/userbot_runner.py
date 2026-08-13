"""Userbot-trigger runner: one Telethon event handler, registered once on
the same TelegramClient the parser already authorized — no second session.
Covers both roles from the spec: monitoring a group/channel for keyword
matches, and personal messages sent directly to the account. Throttling,
per-contact cooldown and FloodWaitError handling are part of the send
path from the start, since unsolicited outgoing DMs from a normal account
are the main way Telegram rate-limits or restricts it."""
from __future__ import annotations

import asyncio
import logging
import time

from telethon import events
from telethon.errors import FloodWaitError

from ..db.database import Database
from ..telegram.service import TelegramService
from . import settings as bot_settings
from .rules_engine import IncomingEvent, RulesEngine

_logger = logging.getLogger("chatgrab")


class UserbotRunner:
    def __init__(self, tg: TelegramService, db: Database, rules: RulesEngine, on_log, on_status):
        self.tg = tg
        self.db = db
        self.rules = rules
        self._on_log = on_log        # (bot_id, text, tone) -> None
        self._on_status = on_status  # (bot_id, status, error) -> None
        self._entity_cache: dict[int, object] = {}
        # (bot_id, target) -> monotonic time of the last message to that
        # contact; enforces the per-contact cooldown.
        self._cooldowns: dict[tuple[int, int | str], float] = {}
        # bot_id -> monotonic time of that bot's last send of any kind.
        # This is the burst guard: the per-contact cooldown above never
        # spaced out a sweep across *different* contacts.
        self._last_send: dict[int, float] = {}
        # One lock per bot so concurrent senders (a reminder sweep and an
        # incoming message arriving together) queue on the gap instead of
        # both reading a stale timestamp and firing at once.
        self._send_locks: dict[int, asyncio.Lock] = {}
        self._registered = False

    def _send_lock(self, bot_id: int) -> asyncio.Lock:
        lock = self._send_locks.get(bot_id)
        if lock is None:
            lock = asyncio.Lock()
            self._send_locks[bot_id] = lock
        return lock

    def _log_for(self, bot_id: int):
        def log(text: str, tone: str = "") -> None:
            self._on_log(bot_id, text, tone)
        return log

    def register(self) -> None:
        """Idempotent — safe to call whenever the client becomes available,
        even if a userbot-type bot doesn't exist yet."""
        if self._registered or not self.tg.client:
            return
        self.tg.client.add_event_handler(self._on_new_message, events.NewMessage(incoming=True))
        self._registered = True

    def unregister(self) -> None:
        if self._registered and self.tg.client:
            self.tg.client.remove_event_handler(self._on_new_message)
        self._registered = False

    def _running_bots(self) -> list:
        return [b for b in self.db.list_bots() if b["type"] == "userbot" and b["status"] == "running"]

    async def _on_new_message(self, event) -> None:
        # This handler stays registered on the shared Telethon client for
        # the app's whole lifetime — one bad message (odd sender state, a
        # DB hiccup) must never kill it or silently stop later messages
        # from being processed, the same isolation Collector relies on
        # for history backfill.
        try:
            await self._handle_new_message(event)
        except Exception:
            _logger.warning("userbot runner failed to handle an incoming message", exc_info=True)

    async def _handle_new_message(self, event) -> None:
        running = self._running_bots()
        if not running:
            return
        try:
            sender = await event.get_sender()
        except Exception:
            return
        if sender is None or getattr(sender, "bot", False):
            return

        contact_telegram_id = sender.id
        self._entity_cache[contact_telegram_id] = sender
        username = getattr(sender, "username", None)
        text = event.raw_text or ""
        if event.is_private:
            chat_type, chat_id = "dm", None
        else:
            chat_type = "channel" if event.is_channel and not event.is_group else "group"
            chat_id = event.chat_id

        for bot in running:
            bot_id = bot["id"]
            log = self._log_for(bot_id)
            try:
                incoming = IncomingEvent(contact_telegram_id=contact_telegram_id, username=username,
                                          text=text, chat_id=chat_id, chat_type=chat_type)
                send_dm = self.make_send(bot_id)
                if chat_type == "dm" and self.rules.has_active_scenario(bot_id, contact_telegram_id):
                    await self.rules.continue_scenario(bot_id, incoming, send_dm, log)
                    continue
                triggers = self.rules.triggers_for(bot_id, incoming)
                for trigger in triggers:
                    await self.rules.fire(bot_id, trigger, incoming, send_dm, log)
            except Exception as e:
                # One misbehaving bot (bad trigger config, DB error) must
                # not stop the rest of `running` from seeing this message.
                log(f"ошибка обработки сообщения: {e}", "warn")

    def make_send(self, bot_id: int):
        async def send_dm(target: int | str, text: str) -> None:
            if not text:
                return
            limits = bot_settings.load(self.db.get_bot(bot_id))
            key = (bot_id, target)
            log = self._log_for(bot_id)

            cooldown = limits["dm_cooldown_seconds"]
            last = self._cooldowns.get(key)
            if last is not None and time.monotonic() - last < cooldown:
                log(f"пропущено сообщение {target} — действует пауза после предыдущего "
                    f"({cooldown:g} с)", "warn")
                return

            entity = await self._resolve(target)
            if entity is None:
                log(f"не удалось отправить сообщение {target} — аккаунт ещё не видел этого пользователя "
                    "(нужен @username или предыдущий контакт)", "warn")
                return

            # Serialize this bot's sends and space them out. Holding the
            # lock across the sleep is the point: it turns concurrent
            # senders into a queue rather than letting them all wake at
            # once and fire together.
            async with self._send_lock(bot_id):
                gap = limits["send_gap_seconds"]
                previous = self._last_send.get(bot_id)
                if previous is not None:
                    wait = gap - (time.monotonic() - previous)
                    if wait > 0:
                        await asyncio.sleep(wait)
                for _ in range(3):
                    try:
                        await self.tg.client.send_message(entity, text)
                        sent_at = time.monotonic()
                        self._cooldowns[key] = sent_at
                        self._last_send[bot_id] = sent_at
                        return
                    except FloodWaitError as e:
                        log(f"Telegram попросил подождать {e.seconds} с перед отправкой, продолжу сам", "warn")
                        await asyncio.sleep(e.seconds + 1)
                    except Exception as e:
                        log(f"не удалось отправить сообщение {target}: {e}", "warn")
                        return
                # Every attempt hit a flood wait — record the time anyway so
                # the next send still respects the gap from this attempt.
                self._last_send[bot_id] = time.monotonic()
                log(f"не удалось отправить сообщение {target} — Telegram продолжает просить подождать", "warn")
        return send_dm

    async def _resolve(self, target: int | str):
        if isinstance(target, str) and target.startswith("@"):
            try:
                return await self.tg.client.get_entity(target)
            except Exception:
                return None
        try:
            tid = int(target)
        except (TypeError, ValueError):
            return None
        if tid in self._entity_cache:
            return self._entity_cache[tid]
        try:
            entity = await self.tg.client.get_entity(tid)
            self._entity_cache[tid] = entity
            return entity
        except Exception:
            return None
