"""Bot-assistant runner: an aiogram Bot+Dispatcher polling loop that lives
as an asyncio task in the same qasync event loop as the rest of the app —
no separate thread or process. Handles the incoming-DM flow: match a
trigger, or continue an in-flight scenario if the contact is mid-dialog."""
from __future__ import annotations

import asyncio
import logging
import re

from ..db.database import Database
from .crypto import decrypt_token
from .rules_engine import IncomingEvent, RulesEngine

_logger = logging.getLogger("chatgrab")

# Bot API tokens look like "123456789:AAExampleTokenTextHere-abc123" and
# aiogram/aiohttp error messages sometimes echo the request URL (which
# embeds the token) verbatim. Redact before anything reaches chatgrab.log,
# the bot's last_error field, or the bots list UI — all three persist or
# display str(exception) as-is otherwise.
_TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")


def _redact(text: str) -> str:
    return _TOKEN_RE.sub("<токен скрыт>", text)


class BotApiRunner:
    def __init__(self, db: Database, security, rules: RulesEngine, bot_row, on_log, on_status, outbox):
        self.db = db
        self.security = security
        self.rules = rules
        self.bot_id = bot_row["id"]
        self._on_log = on_log        # (bot_id, text, tone) -> None
        self._on_status = on_status  # (bot_id, status, error) -> None
        self.outbox = outbox
        # Every message this runner hands to RulesEngine arrives because
        # the contact just wrote something — reactive, always. Built once
        # here rather than per-message since the wrap itself is stateless.
        self._reactive_send = outbox.wrap(self.bot_id, self.send_dm, reactive=True)
        self.bot = None
        self.dp = None
        self._poll_task: asyncio.Task | None = None

    def _log(self, text: str, tone: str = "") -> None:
        self._on_log(self.bot_id, text, tone)

    async def start(self) -> None:
        from aiogram import Bot, Dispatcher
        from aiogram.client.default import DefaultBotProperties
        from aiogram.exceptions import TelegramAPIError

        bot_row = self.db.get_bot(self.bot_id)
        token = decrypt_token(self.security, bot_row["token_encrypted"] or "")
        if not token:
            self._on_status(self.bot_id, "error", "Не указан токен Bot API.")
            return

        self.bot = Bot(token=token, default=DefaultBotProperties())
        try:
            me = await self.bot.get_me()
        except TelegramAPIError as e:
            self._on_status(self.bot_id, "error", _redact(f"Telegram отклонил токен: {e}"))
            await self.bot.session.close()
            self.bot = None
            return

        self.dp = Dispatcher()
        self.dp.message.register(self._on_message)

        self._poll_task = asyncio.ensure_future(self._run_polling())
        self._on_status(self.bot_id, "running", None)
        self._log(f"бот @{me.username} запущен и слушает личные сообщения", "ok")

    async def _run_polling(self) -> None:
        from aiogram.exceptions import TelegramAPIError
        try:
            await self.dp.start_polling(self.bot, handle_signals=False)
        except asyncio.CancelledError:
            raise
        except TelegramAPIError as e:
            self._on_status(self.bot_id, "error", _redact(f"Ошибка Bot API: {e}"))
            self._log(_redact(f"остановлен из-за ошибки Telegram: {e}"), "warn")
        except Exception as e:
            self._on_status(self.bot_id, "error", _redact(str(e)))
            self._log(_redact(f"остановлен из-за непредвиденной ошибки: {e}"), "warn")

    async def stop(self) -> None:
        if self.dp:
            self.dp.stop_polling()
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
            self._poll_task = None
        if self.bot:
            await self.bot.session.close()
            self.bot = None
        self._on_status(self.bot_id, "stopped", None)
        self._log("бот остановлен", "")

    async def send_dm(self, target: int | str, text: str) -> None:
        if not text:
            return
        if isinstance(target, str) and not target.startswith("@"):
            try:
                target = int(target)
            except ValueError:
                pass
        from aiogram.exceptions import TelegramRetryAfter
        for _ in range(3):
            try:
                await self.bot.send_message(target, text)
                return
            except TelegramRetryAfter as e:
                self._log(f"Telegram попросил подождать {e.retry_after} с перед отправкой, продолжу сам", "warn")
                await asyncio.sleep(e.retry_after + 1)
            except Exception as e:
                self._log(_redact(f"не удалось отправить сообщение {target}: {e}"), "warn")
                return

    async def _on_message(self, message) -> None:
        # A single malformed/unexpected message (DB error, odd payload)
        # must not vanish into aiogram's own exception handling unlogged —
        # same per-item isolation Collector uses for history backfill.
        try:
            await self._handle_message(message)
        except Exception as e:
            self._log(_redact(f"ошибка обработки входящего сообщения: {e}"), "warn")

    async def _handle_message(self, message) -> None:
        if message.from_user is None:
            return  # channel post or service message — no author to answer

        text = message.text or message.caption or ""
        contact_telegram_id = message.from_user.id
        username = message.from_user.username
        self.db.upsert_contact(contact_telegram_id, username)

        # Where this arrived actually matters. Until this was read, every
        # message was labelled a DM — so a bot added to a group treated
        # group chatter as private conversation: «написали в личку» rules
        # fired on it and scenarios started with whoever happened to post.
        chat_type, chat_id = self._classify(message)

        is_command = text.startswith("/")
        command = text.split()[0][1:].split("@")[0] if is_command else None
        event = IncomingEvent(
            contact_telegram_id=contact_telegram_id, username=username, text=text,
            is_command=is_command, command=command, chat_type=chat_type, chat_id=chat_id,
        )

        # A scripted dialog is a private, one-to-one thing: continuing one
        # in a group would answer a member's unrelated message with the
        # next question and put the group's words into their lead.
        if chat_type == "dm" and not is_command \
                and self.rules.has_active_scenario(self.bot_id, contact_telegram_id):
            await self.rules.continue_scenario(self.bot_id, event, self._reactive_send, self._log)
            return

        triggers = self.rules.triggers_for(self.bot_id, event)
        for trigger in triggers:
            await self.rules.fire(self.bot_id, trigger, event, self._reactive_send, self._log)
        if not triggers and chat_type == "dm":
            # Only worth logging for DMs — in a busy group this would be
            # one log line per unrelated message.
            self._log(f"сообщение от {contact_telegram_id} не совпало ни с одним правилом")

    @staticmethod
    def _classify(message) -> tuple[str, int | None]:
        """Telegram's chat type mapped onto the engine's vocabulary."""
        raw = getattr(message.chat, "type", None)
        raw = getattr(raw, "value", raw)  # aiogram may hand back an enum
        if raw in ("group", "supergroup"):
            return "group", message.chat.id
        if raw == "channel":
            return "channel", message.chat.id
        return "dm", None
