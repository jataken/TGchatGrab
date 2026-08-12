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
from .rules_engine import IncomingEvent, RulesEngine

_logger = logging.getLogger("chatgrab")
DEFAULT_COOLDOWN_SECONDS = 30


class UserbotRunner:
    def __init__(self, tg: TelegramService, db: Database, rules: RulesEngine, on_log, on_status,
                 cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS):
        self.tg = tg
        self.db = db
        self.rules = rules
        self._on_log = on_log        # (bot_id, text, tone) -> None
        self._on_status = on_status  # (bot_id, status, error) -> None
        self.cooldown_seconds = cooldown_seconds
        self._entity_cache: dict[int, object] = {}
        self._cooldowns: dict[tuple[int, int | str], float] = {}
        self._registered = False

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
            incoming = IncomingEvent(contact_telegram_id=contact_telegram_id, username=username,
                                      text=text, chat_id=chat_id, chat_type=chat_type)
            log = self._log_for(bot_id)
            send_dm = self._make_send(bot_id)
            if chat_type == "dm" and self.rules.has_active_scenario(bot_id, contact_telegram_id):
                await self.rules.continue_scenario(bot_id, incoming, send_dm, log)
                continue
            triggers = self.rules.triggers_for(bot_id, incoming)
            for trigger in triggers:
                await self.rules.fire(bot_id, trigger, incoming, send_dm, log)

    def _make_send(self, bot_id: int):
        async def send_dm(target: int | str, text: str) -> None:
            if not text:
                return
            key = (bot_id, target)
            now = time.monotonic()
            last = self._cooldowns.get(key)
            if last is not None and now - last < self.cooldown_seconds:
                self._log_for(bot_id)(
                    f"пропущено сообщение {target} — действует пауза после предыдущего "
                    f"({self.cooldown_seconds} с)", "warn")
                return
            entity = await self._resolve(target)
            if entity is None:
                self._log_for(bot_id)(
                    f"не удалось отправить сообщение {target} — аккаунт ещё не видел этого пользователя "
                    "(нужен @username или предыдущий контакт)", "warn")
                return
            for _ in range(3):
                try:
                    await self.tg.client.send_message(entity, text)
                    self._cooldowns[key] = time.monotonic()
                    return
                except FloodWaitError as e:
                    self._log_for(bot_id)(
                        f"Telegram попросил подождать {e.seconds} с перед отправкой, продолжу сам", "warn")
                    await asyncio.sleep(e.seconds + 1)
                except Exception as e:
                    self._log_for(bot_id)(f"не удалось отправить сообщение {target}: {e}", "warn")
                    return
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
