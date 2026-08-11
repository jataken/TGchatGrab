"""Thin wrapper around a single Telethon client: one session for the whole
app, covering the login wizard and basic entity/dialog lookups. The
collector (collector.py) builds on top of this for history + realtime."""
from __future__ import annotations

from dataclasses import dataclass

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import Channel, Chat, User

from .. import __version__
from ..config import AppConfig


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
        return await self.client.is_user_authorized()

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

    # ---- chat lookups -------------------------------------------------
    async def list_dialogs(self, limit: int = 300) -> list[DialogInfo]:
        await self.connect()
        out: list[DialogInfo] = []
        async for d in self.client.iter_dialogs(limit=limit):
            entity = d.entity
            if not isinstance(entity, (Channel, Chat)):
                continue
            members = getattr(entity, "participants_count", None)
            out.append(DialogInfo(
                chat_id=d.id, title=d.name or str(d.id),
                username=getattr(entity, "username", None),
                members=members, is_group_or_channel=True,
            ))
        return out

    async def resolve_chat(self, link_or_username: str):
        await self.connect()
        text = link_or_username.strip()
        text = text.replace("https://t.me/", "").replace("http://t.me/", "")
        text = text.replace("t.me/", "").lstrip("@")
        text = text.split("?")[0].strip("/")
        entity = await self.client.get_entity(text)
        if isinstance(entity, User):
            raise ValueError("Это личный чат, а не групповой — приложение собирает только групповые чаты и каналы.")
        return entity
