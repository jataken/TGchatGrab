"""Trigger matching + action execution, shared by both runner types
(Bot API and userbot). A rule is a trigger row plus its ordered actions;
matching is pure and synchronous, execution is async because actions can
send messages. The runner supplies a `send_dm` callable — the only piece
that differs between aiogram and Telethon — everything else (save lead,
tag, log, run scenario, notify manager) is identical.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from ..core import lead as lead_domain
from ..db.database import Database
from .scenario_engine import ScenarioEngine
from .templating import context_for, render, resolve_action_text

_logger = logging.getLogger("chatgrab")

# target is a numeric telegram_id or an "@username" string (userbot replies
# need a resolvable entity — a username works even for contacts the
# account has never seen before; a bare id only works once it has).
SendFn = Callable[[int | str, str], Awaitable[None]]


@dataclass
class IncomingEvent:
    contact_telegram_id: int
    username: str | None
    text: str
    is_command: bool = False
    command: str | None = None
    chat_id: int | None = None       # set for chat_message triggers (userbot)
    chat_type: str | None = None     # 'dm' | 'group' | 'channel'


def _cfg(trigger_row) -> dict:
    try:
        return json.loads(trigger_row["config"])
    except (json.JSONDecodeError, TypeError):
        return {}


class RulesEngine:
    def __init__(self, db: Database):
        self.db = db
        self.scenarios = ScenarioEngine(db)

    # ---- matching --------------------------------------------------------
    def matches(self, trigger_row, event: IncomingEvent) -> bool:
        if not trigger_row["enabled"]:
            return False
        ttype = trigger_row["type"]
        cfg = _cfg(trigger_row)
        if ttype == "incoming_dm":
            return event.chat_type in (None, "dm")
        if ttype == "command":
            return event.is_command and event.command == cfg.get("command")
        if ttype == "keyword":
            words = [w.strip().lower() for w in cfg.get("keywords", []) if w.strip()]
            if not words:
                return False
            text_l = event.text.lower()
            return any(w in text_l for w in words)
        if ttype == "chat_message":
            if event.chat_type not in ("group", "channel"):
                return False
            wanted_chat = cfg.get("chat_id")
            if wanted_chat is not None and event.chat_id != wanted_chat:
                return False
            words = [w.strip().lower() for w in cfg.get("keywords", []) if w.strip()]
            if not words:
                return True
            text_l = event.text.lower()
            return any(w in text_l for w in words)
        # schedule / inactivity triggers aren't message-driven — they're
        # evaluated by their own background tick, not this per-message path.
        return False

    def triggers_for(self, bot_id: int, event: IncomingEvent) -> list:
        return [t for t in self.db.list_triggers(bot_id) if self.matches(t, event)]

    # ---- execution ---------------------------------------------------
    async def fire(self, bot_id: int, trigger_row, event: IncomingEvent, send_dm: SendFn,
                    log=lambda text, tone="": None) -> None:
        contact_id = self.db.upsert_contact(event.contact_telegram_id, event.username)
        self.db.log_activity(contact_id, bot_id, event.chat_id, None, event.chat_type or "dm",
                              kind="trigger_fired")
        for action in self.db.list_actions(trigger_row["id"]):
            try:
                await self._run_action(bot_id, action, event, contact_id, send_dm, log)
            except Exception as e:
                log(f"действие «{action['type']}» завершилось ошибкой: {e}", "warn")

    async def _run_action(self, bot_id: int, action_row, event: IncomingEvent, contact_id: int,
                           send_dm: SendFn, log) -> None:
        cfg = _cfg(action_row)
        atype = action_row["type"]

        if atype == "send_dm":
            text = resolve_action_text(self.db, cfg, bot_id, self._values(bot_id, contact_id, event))
            if not text:
                log("действие «отправить сообщение» пропущено — пустой текст и не выбран шаблон", "warn")
                return
            await send_dm(event.contact_telegram_id, text)
            log(f"отправлено личное сообщение контакту {event.contact_telegram_id}", "ok")

        elif atype == "run_scenario":
            scenario_id = cfg.get("scenario_id")
            if scenario_id is None:
                return
            result = self.scenarios.start(bot_id, scenario_id, event.contact_telegram_id)
            if result.question:
                await send_dm(event.contact_telegram_id, result.question)
            elif result.done and result.answers is not None:
                self._save_lead_from_answers(bot_id, contact_id, result.answers, event)
            log("сценарий запущен", "ok")

        elif atype == "save_lead":
            content = {"text": event.text} if event.text else {}
            self.db.add_lead(
                contact_id, bot_id, content, status="new",
                source_chat_id=event.chat_id,
                source_type=lead_domain.source_type_from_chat_type(event.chat_type),
                event_source=lead_domain.EVENT_SOURCE_RULE,
            )
            log("заявка сохранена", "ok")

        elif atype == "forward_lead" or atype == "notify_manager":
            bot = self.db.get_bot(bot_id)
            manager = bot["manager_chat_id"] if bot else None
            if manager:
                contact = self.db.get_contact(contact_id)
                handle = f"@{contact['username']}" if contact and contact["username"] else str(event.contact_telegram_id)
                await send_dm(manager, f"Новое обращение от {handle}:\n{event.text}")
            log("менеджер уведомлён" if manager else "у бота не задан менеджер — уведомление пропущено",
                "ok" if manager else "warn")

        elif atype == "tag":
            tag = cfg.get("tag")
            if tag:
                contact = self.db.get_contact(contact_id)
                tags = json.loads(contact["tags"]) if contact else []
                if tag not in tags:
                    tags.append(tag)
                    self.db.set_contact_tags(contact_id, tags)
                log(f"контакту проставлен тег «{tag}»", "ok")

        elif atype == "notify":
            # Generic manager notification with the user's own wording,
            # distinct from notify_manager's auto-built "new inquiry" text.
            text = resolve_action_text(self.db, cfg, bot_id, self._values(bot_id, contact_id, event))
            bot = self.db.get_bot(bot_id)
            manager = bot["manager_chat_id"] if bot else None
            if manager and text:
                await send_dm(manager, text)
                log("менеджер уведомлён", "ok")
            elif not manager:
                log("у бота не задан менеджер — уведомление пропущено", "warn")

    def _values(self, bot_id: int, contact_id: int, event: IncomingEvent,
                 answers: dict | None = None) -> dict:
        """What `{variables}` in this bot's templates can refer to right now."""
        return context_for(self.db, bot_id, self.db.get_contact(contact_id),
                           answers=answers, event_text=event.text)

    def _save_lead_from_answers(self, bot_id: int, contact_id: int, answers: dict,
                                event: IncomingEvent) -> None:
        self.db.add_lead(
            contact_id, bot_id, answers, status="new",
            source_chat_id=event.chat_id,
            source_type=lead_domain.source_type_from_chat_type(event.chat_type),
            event_source=lead_domain.EVENT_SOURCE_SCENARIO,
        )

    # ---- scenario continuation (a contact already mid-dialog) -----------
    def has_active_scenario(self, bot_id: int, contact_telegram_id: int) -> bool:
        return self.db.get_active_scenario_session(bot_id, contact_telegram_id) is not None

    async def continue_scenario(self, bot_id: int, event: IncomingEvent, send_dm: SendFn, log) -> None:
        contact_id = self.db.upsert_contact(event.contact_telegram_id, event.username)
        result = self.scenarios.submit_answer(bot_id, event.contact_telegram_id, event.text)
        if result.error:
            await send_dm(event.contact_telegram_id, result.error)
            return
        if result.question:
            await send_dm(event.contact_telegram_id, result.question)
            return
        if result.done and result.answers is not None:
            self._save_lead_from_answers(bot_id, contact_id, result.answers, event)

            # Confirm to the contact first — they're the one waiting on a
            # reply — then hand the summary to the manager.
            await self._send_scenario_confirmation(
                bot_id, contact_id, event, result, send_dm, log,
            )

            bot = self.db.get_bot(bot_id)
            manager = bot["manager_chat_id"] if bot else None
            if manager:
                contact = self.db.get_contact(contact_id)
                handle = f"@{contact['username']}" if contact and contact["username"] else str(event.contact_telegram_id)
                summary = "; ".join(f"{k}: {v}" for k, v in result.answers.items())
                await send_dm(manager, f"Новая заявка от {handle}\n{summary}")
            log("сценарий завершён, заявка сохранена", "ok")

    async def _send_scenario_confirmation(self, bot_id: int, contact_id: int, event: IncomingEvent,
                                           result, send_dm: SendFn, log) -> None:
        session = self.db.last_finished_session(bot_id, event.contact_telegram_id)
        scenario = self.db.get_scenario(session["scenario_id"]) if session else None
        template_id = scenario["done_template_id"] if scenario else None
        if template_id is None:
            return
        template = self.db.get_template(template_id)
        if template is None:
            log("сценарий завершён, но выбранный шаблон подтверждения удалён", "warn")
            return
        values = self._values(bot_id, contact_id, event, answers=result.answers)
        text = render(template["text"], values)
        if text:
            await send_dm(event.contact_telegram_id, text)
