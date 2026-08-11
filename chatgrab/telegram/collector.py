"""The core of the app: a sequential history-backfill queue (one chat at a
time, so Telegram's account-wide rate limit isn't hit any faster than
necessary) plus a single realtime listener covering every enabled chat at
once (push-based, so it doesn't touch the same limit)."""
from __future__ import annotations

import asyncio
import datetime as dt
import shutil
import time
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from telethon import events
from telethon.errors import FloodWaitError
from telethon.tl.types import Channel, Chat, User
from telethon.utils import get_peer_id

from ..config import AppConfig
from ..db.database import Database, now_iso
from ..paths import Paths
from .errors import humanize_error
from .ratelimit import AccountHealth, AdaptiveDelay
from .service import DialogInfo, TelegramService
from ..services.ignore_service import IgnoreService

DEFAULT_DELAY_BOUNDS = {"min": 0.2, "max": 4.0, "current": 1.0}
DEFAULT_SCHEDULE = {"enabled": False, "start": "23:00", "end": "08:00",
                     "days": [0, 1, 2, 3, 4, 5, 6]}


def _display_name(entity) -> str:
    if entity is None:
        return ""
    if isinstance(entity, User):
        parts = [entity.first_name or "", entity.last_name or ""]
        name = " ".join(p for p in parts if p).strip()
        if name:
            return name
        return f"@{entity.username}" if entity.username else str(entity.id)
    title = getattr(entity, "title", None)
    return title or str(getattr(entity, "id", ""))


def build_link(username: str | None, chat_id: int, message_id: int) -> str:
    if username:
        return f"https://t.me/{username}/{message_id}"
    cid = str(chat_id)
    if cid.startswith("-100"):
        internal = cid[4:]
    elif cid.startswith("-"):
        internal = cid[1:]
    else:
        internal = cid
    return f"https://t.me/c/{internal}/{message_id}"


class Collector(QObject):
    log_event = Signal(dict)
    chats_changed = Signal()
    stats_changed = Signal()

    def __init__(self, db: Database, tg: TelegramService, config: AppConfig, paths: Paths):
        super().__init__()
        self.db = db
        self.tg = tg
        self.config = config
        self.paths = paths
        self.ignore_service = IgnoreService(db)
        self.log_entries: list[dict] = []
        self.delay = AdaptiveDelay(DEFAULT_DELAY_BOUNDS["min"], DEFAULT_DELAY_BOUNDS["max"],
                                    DEFAULT_DELAY_BOUNDS["current"])
        self.health = AccountHealth()
        self.schedule = dict(DEFAULT_SCHEDULE)
        self.enabled_chat_ids: set[int] = set()
        self._paused_chats: set[int] = set()
        self._current_chat_id: int | None = None
        self._tasks: list[asyncio.Task] = []
        self._entity_cache: dict[int, object] = {}
        self._running = False

    # ---- lifecycle -------------------------------------------------
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._load_settings()
        self._register_realtime_handler()
        self._requeue_pending()
        self._tasks.append(asyncio.ensure_future(self._history_worker()))
        self._tasks.append(asyncio.ensure_future(self._stats_worker()))
        self._tasks.append(asyncio.ensure_future(self._watchdog_worker()))
        self._log("все чаты", f"прослушивание новых сообщений включено для {len(self.enabled_chat_ids)} чатов", "ok")

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

    def _load_settings(self) -> None:
        bounds = self.db.get_setting("delay_bounds", DEFAULT_DELAY_BOUNDS)
        self.delay = AdaptiveDelay(bounds["min"], bounds["max"], bounds.get("current", bounds["min"]))
        self.schedule = self.db.get_setting("schedule", DEFAULT_SCHEDULE)

    def save_delay_bounds(self, min_delay: float, max_delay: float) -> None:
        self.delay.set_bounds(min_delay, max_delay)
        self.db.set_setting("delay_bounds", {"min": min_delay, "max": max_delay, "current": self.delay.current})

    def save_schedule(self, enabled: bool, start: str, end: str, days: list[int]) -> None:
        self.schedule = {"enabled": enabled, "start": start, "end": end, "days": days}
        self.db.set_setting("schedule", self.schedule)

    # ---- logging -----------------------------------------------------
    def _log(self, chat_title: str, text: str, tone: str = "") -> None:
        entry = {"time": time.strftime("%H:%M:%S"), "chat": chat_title, "text": text, "tone": tone}
        self.log_entries.insert(0, entry)
        self.log_entries = self.log_entries[:300]
        self.log_event.emit(entry)

    # ---- chat management ----------------------------------------------
    def refresh_listen_filter(self) -> None:
        self.enabled_chat_ids = {c["chat_id"] for c in self.db.list_chats() if c["enabled"]}

    def _requeue_pending(self) -> None:
        for chat in self.db.list_chats():
            if not chat["enabled"]:
                self.db.set_chat_field(chat["chat_id"], status="off")
            elif chat["history_done"]:
                self.db.set_chat_field(chat["chat_id"], status="listening")
            elif chat["chat_id"] not in self._paused_chats:
                self.db.set_chat_field(chat["chat_id"], status="queued")
        self.refresh_listen_filter()
        self.chats_changed.emit()

    async def _get_entity(self, chat_id: int):
        if chat_id in self._entity_cache:
            return self._entity_cache[chat_id]
        entity = await self.tg.client.get_entity(chat_id)
        self._entity_cache[chat_id] = entity
        return entity

    async def add_chat_by_link(self, link_or_username: str, depth_mode: str = "all",
                                depth_from_date: str | None = None) -> int:
        entity = await self.tg.resolve_chat(link_or_username)
        return await self._add_resolved(entity, depth_mode, depth_from_date)

    async def add_chat_from_dialog(self, dialog: DialogInfo, depth_mode: str = "all",
                                    depth_from_date: str | None = None) -> int:
        entity = await self.tg.client.get_entity(dialog.chat_id)
        return await self._add_resolved(entity, depth_mode, depth_from_date)

    async def _add_resolved(self, entity, depth_mode: str, depth_from_date: str | None) -> int:
        chat_id = get_peer_id(entity)
        title = getattr(entity, "title", None) or getattr(entity, "username", None) or str(chat_id)
        username = getattr(entity, "username", None)
        self._entity_cache[chat_id] = entity
        self.db.add_chat(chat_id, title, username, depth_mode, depth_from_date)
        self.refresh_listen_filter()
        self._log(title, "чат добавлен · поставлен в очередь на загрузку истории", "ok")
        self.chats_changed.emit()
        return chat_id

    def set_chat_enabled(self, chat_id: int, enabled: bool) -> None:
        chat = self.db.get_chat(chat_id)
        if not chat:
            return
        status = "off"
        if enabled:
            status = "listening" if chat["history_done"] else "queued"
        self.db.set_chat_field(chat_id, enabled=1 if enabled else 0, status=status)
        self.refresh_listen_filter()
        self._log(chat["title"], "сбор включён" if enabled else "сбор выключен — данные сохранены")
        self.chats_changed.emit()

    def toggle_history_loading(self, chat_id: int) -> None:
        chat = self.db.get_chat(chat_id)
        if not chat:
            return
        if chat["status"] == "loading" or chat_id in self._paused_chats and chat["status"] != "listening":
            self._paused_chats.add(chat_id)
            if chat["status"] != "listening":
                self.db.set_chat_field(chat_id, status="idle")
            self._log(chat["title"], "загрузка истории приостановлена — продолжу с последнего собранного сообщения")
        else:
            self._paused_chats.discard(chat_id)
            if chat["history_done"]:
                self._log(chat["title"], "история уже собрана полностью")
            else:
                self.db.set_chat_field(chat_id, status="queued", enabled=1)
                self._log(chat["title"], "начинаю загрузку истории с последнего собранного сообщения")
        self.refresh_listen_filter()
        self.chats_changed.emit()

    def toggle_listen(self, chat_id: int) -> None:
        chat = self.db.get_chat(chat_id)
        if not chat:
            return
        self.set_chat_enabled(chat_id, not bool(chat["enabled"]))

    def remove_chat(self, chat_id: int, purge: bool) -> None:
        chat = self.db.get_chat(chat_id)
        if not chat:
            return
        title = chat["title"]
        self._paused_chats.discard(chat_id)
        self._entity_cache.pop(chat_id, None)
        if purge:
            photo_dir = self.paths.photos_dir / str(chat_id)
            if photo_dir.exists():
                shutil.rmtree(photo_dir, ignore_errors=True)
            self.db.delete_chat_and_data(chat_id)
            self._log(title, "чат убран из списка вместе с собранными данными", "warn")
        else:
            self.db.untrack_chat(chat_id)
            self._log(title, "чат убран из списка, собранные сообщения оставлены в базе")
        self.refresh_listen_filter()
        self.chats_changed.emit()

    # ---- realtime listener ------------------------------------------
    def _register_realtime_handler(self) -> None:
        self.refresh_listen_filter()
        self.tg.client.add_event_handler(self._on_new_message, events.NewMessage())
        self.tg.client.add_event_handler(self._on_edit_message, events.MessageEdited())

    async def _on_new_message(self, event) -> None:
        chat_id = event.chat_id
        if chat_id not in self.enabled_chat_ids:
            return
        chat = self.db.get_chat(chat_id)
        if not chat:
            return
        await self._store_message(event.message, chat)
        chat = self.db.get_chat(chat_id)
        self.db.set_chat_field(chat_id, newest_loaded_id=max(chat["newest_loaded_id"] or 0, event.message.id))
        self._log(chat["title"], f"новое сообщение записано (id {event.message.id})", "ok")
        self.chats_changed.emit()

    async def _on_edit_message(self, event) -> None:
        chat_id = event.chat_id
        if chat_id not in self.enabled_chat_ids:
            return
        chat = self.db.get_chat(chat_id)
        if not chat:
            return
        await self._store_message(event.message, chat)
        self._log(chat["title"], "сообщение отредактировано автором — запись обновлена")
        self.chats_changed.emit()

    # ---- message -> DB record -------------------------------------------
    async def _message_to_record(self, message, chat) -> dict:
        chat_id = chat["chat_id"]
        try:
            sender = await message.get_sender()
        except Exception:
            sender = None
        sender_username = getattr(sender, "username", None)
        text = message.raw_text or ""
        media_type = None
        if message.photo:
            media_type = "photo"
        elif message.video:
            media_type = "video"
        elif message.voice:
            media_type = "voice"
        elif message.document:
            media_type = "document"
        elif message.media:
            media_type = "media"
        photo_path = None
        if media_type == "photo" and self.config.photos_enabled:
            photo_path = await self._download_photo(message, chat)
        forwarded_from = await self._forward_label(message)
        is_hidden = self.ignore_service.matches(chat_id, sender_username, _display_name(sender), text)
        return {
            "chat_id": chat_id,
            "is_hidden": 1 if is_hidden else 0,
            "message_id": message.id,
            "chat_title": chat["title"],
            "date": message.date.astimezone().isoformat(timespec="seconds") if message.date else now_iso(),
            "edited_date": message.edit_date.astimezone().isoformat(timespec="seconds") if message.edit_date else None,
            "sender_id": message.sender_id,
            "sender_username": sender_username,
            "sender_display_name": _display_name(sender),
            "text": text,
            "reply_to_message_id": message.reply_to_msg_id if message.is_reply else None,
            "forwarded_from": forwarded_from,
            "media_type": media_type,
            "media_caption": text if media_type else None,
            "photo_path": photo_path,
            "views": getattr(message, "views", None),
            "link": build_link(chat["username"], chat_id, message.id),
        }

    async def _forward_label(self, message) -> str | None:
        fwd = message.forward
        if not fwd:
            return None
        if fwd.from_name:
            return fwd.from_name
        if fwd.sender_id:
            try:
                entity = await self._get_entity(fwd.sender_id)
                return f"@{entity.username}" if getattr(entity, "username", None) else _display_name(entity)
            except Exception:
                return f"id{fwd.sender_id}"
        if fwd.channel_id:
            return f"канал id{fwd.channel_id}"
        return "переслано"

    async def _download_photo(self, message, chat) -> str | None:
        path = self.paths.photo_path(chat["chat_id"], message.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(3):
            try:
                result = await message.download_media(file=str(path))
                if not result:
                    return None
                self._log(chat["title"], f"фото сохранено → photos\\{chat['chat_id']}\\{message.id}.jpg")
                return str(Path(result).relative_to(self.paths.data_dir))
            except FloodWaitError as e:
                self.health.note_flood_wait(e.seconds)
                self._log(chat["title"], f"Telegram попросил подождать {e.seconds} с (скачивание фото), продолжу сам", "warn")
                await asyncio.sleep(e.seconds + 1)
            except Exception:
                return None
        return None

    async def _store_message(self, message, chat) -> bool:
        record = await self._message_to_record(message, chat)
        return self.db.upsert_message(record)

    # ---- history backfill --------------------------------------------
    async def _history_worker(self) -> None:
        while True:
            await asyncio.sleep(1.5)
            chat = self._next_queued_chat()
            if chat is None:
                continue
            if self.schedule.get("enabled") and not self._within_schedule_window():
                continue
            self._current_chat_id = chat["chat_id"]
            self.db.set_chat_field(chat["chat_id"], status="loading")
            self.chats_changed.emit()
            try:
                await self._backfill_chat(chat)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.db.set_chat_field(chat["chat_id"], status="idle", last_error=humanize_error(e))
                self._log(chat["title"], f"ошибка загрузки истории: {humanize_error(e)}", "warn")
            finally:
                self._current_chat_id = None
                self.chats_changed.emit()

    def _next_queued_chat(self):
        for chat in self.db.list_chats():
            if chat["status"] == "queued" and chat["chat_id"] not in self._paused_chats:
                return chat
        return None

    def _within_schedule_window(self) -> bool:
        now = dt.datetime.now()
        if now.weekday() not in self.schedule.get("days", list(range(7))):
            return False
        start = dt.datetime.strptime(self.schedule["start"], "%H:%M").time()
        end = dt.datetime.strptime(self.schedule["end"], "%H:%M").time()
        cur = now.time()
        if start <= end:
            return start <= cur <= end
        return cur >= start or cur <= end

    async def _backfill_chat(self, chat: dict) -> None:
        chat_id = chat["chat_id"]
        entity = await self._get_entity(chat_id)
        try:
            total = await self.tg.client.get_messages(entity, limit=0)
            self.db.set_chat_field(chat_id, approx_total=total.total)
        except Exception:
            pass

        if chat["newest_loaded_id"]:
            await self._collect_direction(entity, chat, reverse=True, min_id=chat["newest_loaded_id"])

        chat = self.db.get_chat(chat_id)
        if not chat["enabled"] or chat_id in self._paused_chats:
            return

        if not chat["history_done"]:
            cutoff_date = None
            if chat["depth_mode"] == "from_date" and chat["depth_from_date"]:
                cutoff_date = dt.datetime.fromisoformat(chat["depth_from_date"])
                if cutoff_date.tzinfo is None:
                    cutoff_date = cutoff_date.astimezone()
            offset_id = chat["oldest_loaded_id"] or 0
            reached_end = await self._collect_direction(
                entity, chat, reverse=False, offset_id=offset_id, cutoff_date=cutoff_date
            )
            if reached_end:
                self.db.set_chat_field(chat_id, history_done=1)
                self._log(chat["title"], "история загружена полностью — перешёл на новые сообщения", "ok")

        fresh = self.db.get_chat(chat_id)
        if fresh["enabled"]:
            self.db.set_chat_field(chat_id, status="listening")
        self.db.rebuild_stat_cache(chat_id)

    async def _collect_direction(self, entity, chat: dict, reverse: bool, offset_id: int = 0,
                                  min_id: int = 0, cutoff_date=None) -> bool:
        chat_id = chat["chat_id"]
        count = 0
        reached_end = True
        async for message in self._iter_with_flood_handling(entity, chat, reverse=reverse,
                                                              offset_id=offset_id, min_id=min_id):
            fresh = self.db.get_chat(chat_id)
            if not fresh or not fresh["enabled"] or chat_id in self._paused_chats:
                reached_end = False
                break
            if cutoff_date and message.date and message.date < cutoff_date:
                reached_end = True
                break
            await self._store_message(message, fresh)
            fresh = self.db.get_chat(chat_id)
            if reverse:
                self.db.set_chat_field(chat_id, newest_loaded_id=max(fresh["newest_loaded_id"] or 0, message.id))
            else:
                mn = fresh["oldest_loaded_id"]
                self.db.set_chat_field(chat_id, oldest_loaded_id=message.id if mn is None else min(mn, message.id))
            count += 1
            if count % 25 == 0:
                self.chats_changed.emit()
                total_hint = f" из ≈{fresh['approx_total']}" if fresh["approx_total"] else ""
                self._log(chat["title"], f"история: получено {self.db.message_count(chat_id)}{total_hint} сообщений")
            await asyncio.sleep(self.delay.current)
        self.chats_changed.emit()
        return reached_end

    async def _iter_with_flood_handling(self, entity, chat: dict, reverse: bool,
                                         offset_id: int = 0, min_id: int = 0):
        kwargs: dict = {"reverse": reverse}
        if offset_id:
            kwargs["offset_id"] = offset_id
        if min_id:
            kwargs["min_id"] = min_id
        it = self.tg.client.iter_messages(entity, **kwargs)
        while True:
            try:
                message = await it.__anext__()
                self.health.note_request()
                self.delay.on_success()
                yield message
            except StopAsyncIteration:
                return
            except FloodWaitError as e:
                self.health.note_flood_wait(e.seconds)
                self.delay.on_flood_wait()
                self._log(chat["title"], f"Telegram попросил подождать {e.seconds} с, продолжу сам", "warn")
                await asyncio.sleep(e.seconds + 1)
                self._log(chat["title"], "пауза закончилась — сбор продолжается")
                continue

    # ---- integrity: gap patching --------------------------------------
    async def patch_gaps(self, chat_id: int) -> int:
        chat = self.db.get_chat(chat_id)
        if not chat:
            return 0
        entity = await self._get_entity(chat_id)
        gaps = self.db.find_gaps(chat_id)
        patched = 0
        for start, end in gaps:
            async for message in self._iter_with_flood_handling(entity, chat, reverse=True, offset_id=start - 1):
                if message.id > end:
                    break
                await self._store_message(message, chat)
                patched += 1
                await asyncio.sleep(self.delay.current)
        self._log(chat["title"], f"проверка целостности: залатано {len(gaps)} пропусков, {patched} сообщений", "ok")
        self.chats_changed.emit()
        return patched

    # ---- background workers -------------------------------------------
    async def _stats_worker(self) -> None:
        while True:
            for chat in self.db.list_chats():
                self.db.rebuild_stat_cache(chat["chat_id"])
            self.stats_changed.emit()
            await asyncio.sleep(30)

    async def _watchdog_worker(self) -> None:
        backoff = 2
        while True:
            await asyncio.sleep(15)
            try:
                if self.tg.client and not self.tg.client.is_connected():
                    self._log("все чаты", "соединение потеряно — пробую переподключиться", "warn")
                    await self.tg.client.connect()
                    if self.tg.client.is_connected():
                        self._log("все чаты", "соединение восстановлено, докачиваю пропущенное", "ok")
                        backoff = 2
                        for chat in self.db.list_chats():
                            if chat["enabled"] and chat["newest_loaded_id"]:
                                try:
                                    entity = await self._get_entity(chat["chat_id"])
                                    await self._collect_direction(entity, chat, reverse=True,
                                                                   min_id=chat["newest_loaded_id"])
                                except Exception:
                                    pass
                    else:
                        backoff = min(60, backoff * 2)
                        await asyncio.sleep(backoff)
            except Exception:
                backoff = min(60, backoff * 2)
