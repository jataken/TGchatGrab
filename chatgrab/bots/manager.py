"""Orchestrates every configured bot: starts/stops the right runner type,
tracks status in the database (so a bot left "running" resumes on the next
launch even after a crash, the same way collected chats resume), and is
the single object the UI talks to regardless of whether a given bot is a
Bot API assistant or a userbot rule bundle."""
from __future__ import annotations

import asyncio
import logging
import time

from PySide6.QtCore import QObject, Signal

from ..db.database import Database
from ..security import SecurityService
from ..telegram.service import TelegramService
from .bot_api_runner import BotApiRunner
from .crypto import decrypt_token, encrypt_token
from .outbox import Outbox
from .presets import apply_preset
from .rules_engine import RulesEngine
from .scheduler import TriggerScheduler
from .userbot_runner import UserbotRunner

_logger = logging.getLogger("chatgrab")


class BotManager(QObject):
    log_event = Signal(dict)
    bots_changed = Signal()

    def __init__(self, db: Database, tg: TelegramService, security: SecurityService):
        super().__init__()
        self.db = db
        self.tg = tg
        self.security = security
        self.rules = RulesEngine(db)
        # Constructed before either runner — both take it, so every send
        # either one hands to RulesEngine is already outbox-wrapped at the
        # one place each runner builds a send_dm, rather than each runner
        # needing to know about limits/drafts/blacklist itself.
        self.outbox = Outbox(db, self._on_log)
        self.userbot_runner = UserbotRunner(tg, db, self.rules, self._on_log, self._on_status, self.outbox)
        self._bot_api_runners: dict[int, BotApiRunner] = {}
        self.log_entries: list[dict] = []
        self._running = False
        # Evaluates the trigger types that no incoming message can match —
        # inactivity reminders and schedules. See bots/scheduler.py.
        self.scheduler = TriggerScheduler(db, self.rules, self._send_for_bot, self._on_log)
        # One lock per bot_id — serializes start_bot()/stop_bot() for that
        # bot so a rapid double-click (or any other concurrent caller)
        # can't create two BotApiRunner instances polling the same token
        # at once, which Telegram rejects as a getUpdates conflict.
        self._bot_locks: dict[int, asyncio.Lock] = {}

    def _lock_for(self, bot_id: int) -> asyncio.Lock:
        lock = self._bot_locks.get(bot_id)
        if lock is None:
            lock = asyncio.Lock()
            self._bot_locks[bot_id] = lock
        return lock

    # ---- logging / status callbacks (shared by both runner types) -------
    def _on_log(self, bot_id: int, text: str, tone: str = "") -> None:
        bot = self.db.get_bot(bot_id)
        name = bot["name"] if bot else f"бот {bot_id}"
        entry = {"time": time.strftime("%H:%M:%S"), "bot": name, "bot_id": bot_id, "text": text, "tone": tone}
        self.log_entries.insert(0, entry)
        self.log_entries = self.log_entries[:300]
        self.log_event.emit(entry)
        if tone == "warn":
            _logger.warning("[bot:%s] %s", name, text)

    def _on_status(self, bot_id: int, status: str, error: str | None) -> None:
        self.db.set_bot_field(bot_id, status=status, last_error=error)
        self.bots_changed.emit()

    def _raw_send_for_bot(self, bot_id: int):
        """The runner's own send callable, whichever type owns this bot —
        not yet outbox-wrapped. Only two callers should ever touch this:
        _send_for_bot below (wraps it for the scheduler) and send_draft
        (wraps it fresh per call, since a draft can outlive the runner
        instance that originally tried to send it)."""
        runner = self._bot_api_runners.get(bot_id)
        if runner is not None:
            return runner.send_dm
        return self.userbot_runner.make_send(bot_id)

    def _send_for_bot(self, bot_id: int):
        """The scheduler's send path — schedule/inactivity triggers are
        proactive by definition (nothing from the contact prompted them),
        so this is where a cold first message becomes a draft instead of
        going out."""
        return self.outbox.wrap(bot_id, self._raw_send_for_bot(bot_id), reactive=False)

    # ---- outbox: drafts / blacklist ---------------------------------------
    async def send_draft(self, draft_id: int) -> None:
        """A human clicked "send" on a draft — that's the click invariant 6
        asks for, so this goes out through the *reactive* wrap: still
        blacklist/dry-run/hour-day-checked, just not re-drafted for being
        a first message, since resolving exactly that is the point."""
        draft = self.db.get_draft(draft_id)
        if draft is None or draft["sent_at"] or draft["dismissed_at"]:
            return
        send = self.outbox.wrap(draft["bot_id"], self._raw_send_for_bot(draft["bot_id"]), reactive=True)
        await send(draft["target"], draft["text"])
        self.db.mark_draft_sent(draft_id)

    def dismiss_draft(self, draft_id: int) -> None:
        self.db.dismiss_draft(draft_id)

    # ---- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        """Called once the Telegram session is authorized — mirrors
        Collector.start()'s timing, since the userbot runner needs
        tg.client to exist. Resumes every bot whose status is still
        'running' from before (a clean exit or a crash, doesn't matter —
        the DB is the source of truth, same as chat collection).

        One bot failing to start (bad token, transient network error)
        must never block the others, and must never propagate out of
        here — this runs right alongside the "you're connected" UI
        transition, which should still succeed even if the bot
        subsystem can't."""
        if self._running:
            return
        self._running = True
        # Аккаунты юзерботов, отличные от основного, подключаются здесь —
        # иначе бот, оставшийся в статусе «работает» с прошлого запуска,
        # молча не слушал бы ничего до первого ручного «Запустить».
        for bot in self.db.list_bots():
            if bot["type"] != "userbot" or bot["status"] != "running":
                continue
            service = self.userbot_runner.service_for_bot(bot)
            if service is self.tg:
                continue
            try:
                await service.connect()
            except Exception:
                _logger.warning("не удалось подключить аккаунт бота %s", bot["id"], exc_info=True)
        try:
            self.userbot_runner.register()
        except Exception:
            _logger.warning("userbot runner failed to register", exc_info=True)
        for bot in self.db.list_bots():
            if bot["type"] == "bot_api" and bot["status"] == "running":
                try:
                    await self._start_bot_api(bot)
                except Exception as e:
                    _logger.warning("bot %s failed to start", bot["id"], exc_info=True)
                    self._on_status(bot["id"], "error", str(e))
        self.scheduler.start()
        self.bots_changed.emit()

    async def stop(self) -> None:
        self.scheduler.stop()
        self.userbot_runner.unregister()
        for runner in list(self._bot_api_runners.values()):
            await runner.stop()
        self._bot_api_runners.clear()
        self._running = False

    # ---- creation / deletion ---------------------------------------------
    def create_bot(self, name: str, type_: str, token_plain: str | None, preset: str,
                    manager_chat_id: str | None) -> int:
        token_enc = encrypt_token(self.security, token_plain) if token_plain else None
        bot_id = self.db.add_bot(name, type_, token_enc, preset=preset, manager_chat_id=manager_chat_id)
        apply_preset(self.db, bot_id, preset)
        self.bots_changed.emit()
        return bot_id

    async def delete_bot(self, bot_id: int) -> None:
        await self.stop_bot(bot_id)
        self.db.delete_bot(bot_id)
        self.bots_changed.emit()

    # ---- start/stop a single bot ------------------------------------------
    async def start_bot(self, bot_id: int) -> None:
        async with self._lock_for(bot_id):
            bot = self.db.get_bot(bot_id)
            if not bot:
                return
            if bot["type"] == "userbot":
                service = self.userbot_runner.service_for_bot(bot)
                try:
                    # Свой аккаунт у бота может быть ещё не подключён:
                    # реестр создаёт клиентов лениво.
                    await service.connect()
                    if not await service.is_authorized():
                        self.db.set_bot_field(
                            bot_id, status="error",
                            last_error="Аккаунт бота не авторизован — войдите на экране «Аккаунты».")
                        self._on_log(bot_id, "аккаунт бота не авторизован", "warn")
                        self.bots_changed.emit()
                        return
                except Exception as e:
                    self.db.set_bot_field(bot_id, status="error", last_error=str(e))
                    self._on_log(bot_id, f"не удалось подключить аккаунт бота: {e}", "warn")
                    self.bots_changed.emit()
                    return
                self.userbot_runner.register()
                self.db.set_bot_field(bot_id, status="running", last_error=None)
                self._on_log(bot_id, "правила юзербота включены", "ok")
            else:
                if bot_id in self._bot_api_runners:
                    return  # already running (or another start_bot() call is mid-flight)
                await self._start_bot_api(bot)
            self.bots_changed.emit()

    async def stop_bot(self, bot_id: int) -> None:
        async with self._lock_for(bot_id):
            bot = self.db.get_bot(bot_id)
            if not bot:
                return
            if bot["type"] == "userbot":
                self.db.set_bot_field(bot_id, status="stopped")
                self._on_log(bot_id, "правила юзербота выключены", "")
            else:
                runner = self._bot_api_runners.pop(bot_id, None)
                if runner:
                    await runner.stop()
                else:
                    self.db.set_bot_field(bot_id, status="stopped")
            self.bots_changed.emit()

    async def _start_bot_api(self, bot_row) -> None:
        runner = BotApiRunner(self.db, self.security, self.rules, bot_row, self._on_log,
                               self._on_status, self.outbox)
        self._bot_api_runners[bot_row["id"]] = runner
        await runner.start()

    # ---- token management (settings-style read/update) -------------------
    def get_plain_token(self, bot_id: int) -> str:
        bot = self.db.get_bot(bot_id)
        if not bot or not bot["token_encrypted"]:
            return ""
        return decrypt_token(self.security, bot["token_encrypted"])

    def set_token(self, bot_id: int, token_plain: str) -> None:
        self.db.set_bot_field(bot_id, token_encrypted=encrypt_token(self.security, token_plain))

    def set_manager(self, bot_id: int, manager_chat_id: str) -> None:
        self.db.set_bot_field(bot_id, manager_chat_id=manager_chat_id or None)
