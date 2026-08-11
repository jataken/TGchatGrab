"""Thin wrapper around a single Telethon client: one session for the whole
app, covering the login wizard and basic entity/dialog lookups. The
collector (collector.py) builds on top of this for history + realtime."""
from __future__ import annotations

import re
from dataclasses import dataclass

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.tl.types import ChatInviteAlready, User

from .. import __version__
from ..config import AppConfig

_DOMAIN_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/", re.IGNORECASE)


@dataclass
class DialogInfo:
    chat_id: int
    title: str
    username: str | None
    members: int | None
    is_group_or_channel: bool


class TelegramService:
    def __init__(self, config: AppConfig):
        self.config = config
        self.client: TelegramClient | None = None
        self._phone: str | None = None
        self._phone_code_hash: str | None = None
        # Cached synchronously-readable flag — the sidebar and other
        # lightweight UI polling can check this without awaiting a round
        # trip to Telegram on every refresh tick.
        self.authorized: bool = False

    def _build_client(self) -> TelegramClient:
        if not self.config.api_id.strip() or not self.config.api_hash.strip():
            raise ValueError(
                "Сначала укажите ключ приложения (api_id) и секрет (api_hash) на "
                "экране «Настройки» — их выдаёт my.telegram.org."
            )
        try:
            api_id = int(self.config.api_id.strip())
        except ValueError:
            raise ValueError(
                "Ключ приложения (api_id) должен быть числом — проверьте значение "
                "на экране «Настройки»."
            ) from None
        api_hash = self.config.api_hash.strip()
        return TelegramClient(
            self.config.session_path, api_id, api_hash,
            device_model="ChatGrab", app_version=__version__,
            system_version="Windows 10",
        )

    async def connect(self) -> None:
        if self.client is None:
            self.client = self._build_client()
        if not self.client.is_connected():
            await self.client.connect()

    async def disconnect(self) -> None:
        if self.client and self.client.is_connected():
            await self.client.disconnect()

    async def is_authorized(self) -> bool:
        await self.connect()
        self.authorized = await self.client.is_user_authorized()
        return self.authorized

    async def me(self):
        await self.connect()
        return await self.client.get_me()

    # ---- auth wizard --------------------------------------------------
    async def send_code(self, phone: str) -> None:
        await self.connect()
        result = await self.client.send_code_request(phone)
        self._phone = phone
        self._phone_code_hash = result.phone_code_hash

    async def submit_code(self, code: str) -> bool:
        """Returns True if signed in, False if a 2FA password is required."""
        try:
            await self.client.sign_in(
                phone=self._phone, code=code, phone_code_hash=self._phone_code_hash
            )
            return True
        except SessionPasswordNeededError:
            return False

    async def submit_password(self, password: str) -> None:
        await self.client.sign_in(password=password)

    async def sign_out(self) -> None:
        if self.client:
            try:
                await self.client.log_out()
            except Exception:
                pass
            self.client = None
        self._phone = None
        self._phone_code_hash = None
        self.authorized = False

    # ---- chat lookups -------------------------------------------------
    async def list_dialogs(self, limit: int | None = 1000) -> list[DialogInfo]:
        await self.connect()
        out: list[DialogInfo] = []
        async for d in self.client.iter_dialogs(limit=limit):
            # Telethon's own classification — covers megagroups, broadcast
            # channels and basic groups, and correctly excludes chats the
            # account was kicked from (ChatForbidden) and private DMs.
            if not (d.is_group or d.is_channel):
                continue
            entity = d.entity
            members = getattr(entity, "participants_count", None)
            out.append(DialogInfo(
                chat_id=d.id, title=d.name or str(d.id),
                username=getattr(entity, "username", None),
                members=members, is_group_or_channel=True,
            ))
        return out

    async def resolve_chat(self, link_or_username: str):
        await self.connect()
        text = _DOMAIN_RE.sub("", link_or_username.strip())
        text = text.split("?")[0].split("#")[0].strip("/")
        if not text:
            raise ValueError("Не удалось распознать ссылку или имя чата.")

        invite_hash = _extract_invite_hash(text)
        if invite_hash:
            return await self._resolve_invite(invite_hash)

        username = text.lstrip("@")
        try:
            entity = await self.client.get_entity(username)
        except ValueError as e:
            raise ValueError(
                f"Такого чата не существует, либо он недоступен: «{link_or_username.strip()}». "
                "Проверьте ссылку/имя или выберите чат из списка своих диалогов."
            ) from e
        if isinstance(entity, User):
            raise ValueError("Это личный чат, а не групповой — приложение собирает только групповые чаты и каналы.")
        return entity

    async def _resolve_invite(self, invite_hash: str):
        try:
            invite = await self.client(CheckChatInviteRequest(invite_hash))
        except Exception as e:
            raise ValueError(
                "Не удалось проверить ссылку-приглашение — возможно, она устарела или отозвана."
            ) from e
        if isinstance(invite, ChatInviteAlready):
            return invite.chat
        # Not a participant yet (ChatInvite/ChatInvitePeek) — joining is the
        # only way a user account can read full history of a private chat.
        updates = await self.client(ImportChatInviteRequest(invite_hash))
        if not updates.chats:
            raise ValueError("Не удалось присоединиться к чату по этой ссылке.")
        return updates.chats[0]


def _extract_invite_hash(text: str) -> str | None:
    """`+HASH` or `joinchat/HASH` — private invite links, resolved via
    CheckChatInviteRequest/ImportChatInviteRequest rather than get_entity
    (which would otherwise misread a leading '+' as a phone number)."""
    if text.startswith("+"):
        return text[1:] or None
    m = re.match(r"^joinchat/([\w-]+)$", text, re.IGNORECASE)
    return m.group(1) if m else None
