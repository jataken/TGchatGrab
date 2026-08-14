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
        # Реестр аккаунтов; None — приложение на одном аккаунте.
        self.accounts = None
        # Ключ — (клиент, собеседник): access_hash выдаётся аккаунту, а не
        # приложению, поэтому entity одного аккаунта бесполезна другому.
        self._entity_cache: dict[tuple[int, int | str], object] = {}
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
        self._registered_clients: set[int] = set()

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

    # ---- accounts -----------------------------------------------------
    def service_for_bot(self, bot) -> TelegramService:
        """Аккаунт, от имени которого пишет этот бот. Отдельный аккаунт под
        рассылку — единственный способ не подставлять под ограничение тот
        номер, которым собирается история: лимиты Telegram считаются на
        аккаунт, и «не может писать» и «не может читать чаты» иначе
        оказываются одним и тем же событием."""
        if self.accounts is None or bot is None:
            return self.tg
        account_id = bot["account_id"] if "account_id" in bot.keys() else None
        return self.accounts.service_for(account_id)

    def client_for_bot(self, bot):
        return self.service_for_bot(bot).client

    def register(self) -> None:
        """Idempotent — safe to call whenever a client becomes available,
        even if a userbot-type bot doesn't exist yet. Called again after
        bots start, so an account that only just got its first bot is
        picked up without a restart."""
        for service in self._bot_services():
            if service.client is None or id(service.client) in self._registered_clients:
                continue
            service.client.add_event_handler(self._on_new_message, events.NewMessage(incoming=True))
            self._registered_clients.add(id(service.client))
            self._registered = True

    def unregister(self) -> None:
        for service in self._bot_services():
            if service.client is not None and id(service.client) in self._registered_clients:
                service.client.remove_event_handler(self._on_new_message)
                self._registered_clients.discard(id(service.client))
        self._registered = False

    def _bot_services(self) -> list[TelegramService]:
        if self.accounts is None:
            return [self.tg]
        seen: dict[int, TelegramService] = {id(self.tg): self.tg}
        for bot in self.db.list_bots():
            if bot["type"] != "userbot":
                continue
            service = self.service_for_bot(bot)
            seen[id(service)] = service
        return list(seen.values())

    def _running_bots(self, client=None) -> list:
        """Боты, которые должны увидеть это сообщение.

        При нескольких аккаунтах сообщение приходит на конкретный клиент,
        и бот с другого аккаунта его не видел — отдать ему событие значит
        ответить с номера, который в переписке не участвовал.
        """
        bots = [b for b in self.db.list_bots()
                if b["type"] == "userbot" and b["status"] == "running"]
        if client is None or self.accounts is None:
            return bots
        return [b for b in bots if self.client_for_bot(b) is client]

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
        running = self._running_bots(getattr(event, "client", None))
        if not running:
            return
        try:
            sender = await event.get_sender()
        except Exception:
            return
        if sender is None or getattr(sender, "bot", False):
            return

        contact_telegram_id = sender.id
        client = getattr(event, "client", None)
        self._entity_cache[(id(client), contact_telegram_id)] = sender
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
            bot = self.db.get_bot(bot_id)
            limits = bot_settings.load(bot)
            key = (bot_id, target)
            log = self._log_for(bot_id)
            client = self.client_for_bot(bot)
            if client is None:
                log("аккаунт бота не подключён — сообщение не отправлено", "warn")
                return

            cooldown = limits["dm_cooldown_seconds"]
            last = self._cooldowns.get(key)
            if last is not None and time.monotonic() - last < cooldown:
                log(f"пропущено сообщение {target} — действует пауза после предыдущего "
                    f"({cooldown:g} с)", "warn")
                return

            entity = await self._resolve(target, client)
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
                        await client.send_message(entity, text)
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

    async def _resolve(self, target: int | str, client=None):
        client = client or self.tg.client
        if client is None:
            return None
        if isinstance(target, str) and target.startswith("@"):
            try:
                return await client.get_entity(target)
            except Exception:
                return None
        try:
            tid = int(target)
        except (TypeError, ValueError):
            return None
        key = (id(client), tid)
        if key in self._entity_cache:
            return self._entity_cache[key]
        try:
            entity = await client.get_entity(tid)
            self._entity_cache[key] = entity
            return entity
        except Exception:
            return None
