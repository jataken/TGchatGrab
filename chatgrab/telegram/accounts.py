"""Несколько аккаунтов Telegram вместо одного.

Зачем это нужно именно здесь. Ограничения Telegram считаются на аккаунт, и
самый дорогой ресурс в этом приложении — не диск и не трафик, а номер, с
которого всё читается. Пока сбор и рассылка идут с одного номера, любая
неосторожность в ботах бьёт по тому же аккаунту, который собирает
историю: получить ограничение на отправку и потерять доступ к чатам —
одно и то же событие. Разделение снимает именно это.

Устройство намеренно скучное:

- Один `TelegramService` на аккаунт, у каждого свой файл сессии.
- У чата и у бота есть `account_id`; NULL означает «основной».
- Пока аккаунт один, всё ведёт себя ровно как раньше: реестр отдаёт тот
  же самый сервис, что создаётся при старте.

Реестр не решает, кому что делать, — он только выдаёт клиента по ключу.
Решение принимают collector (по чату) и bot manager (по боту).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from ..config import AppConfig
from ..db.database import Database
from ..paths import Paths
from .service import TelegramService

_logger = logging.getLogger("chatgrab")

# Файл сессии основного аккаунта — тот, что уже лежит у пользователя.
PRIMARY_SESSION_FILE = "worker.session"


_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def session_file_for(name: str, taken: set[str]) -> str:
    """Имя файла сессии из имени аккаунта: предсказуемое, чтобы его можно
    было найти глазами в папке.

    Транслитерация — не косметика. Файл сессии открывает не только это
    приложение: путь уходит в SQLite Telethon и в резервные копии, а имя
    в кириллице на Windows зависит от кодовой страницы. Латиница просто
    работает везде.
    """
    slug = "".join(_TRANSLIT.get(ch, ch) for ch in name.strip().lower())
    slug = re.sub(r"[^a-z0-9\-]+", "-", slug).strip("-")
    slug = slug or "account"
    candidate = f"{slug}.session"
    n = 2
    while candidate in taken:
        candidate = f"{slug}-{n}.session"
        n += 1
    return candidate


class AccountRegistry:
    """Держит по одному TelegramService на аккаунт.

    Сервисы создаются лениво: подключать аккаунт, к чатам которого сейчас
    никто не обращается, незачем — это лишний коннект и лишний повод для
    Telegram посмотреть на номер.
    """

    def __init__(self, db: Database, config: AppConfig, paths: Paths,
                 primary: TelegramService):
        self.db = db
        self.config = config
        self.paths = paths
        # Тот самый сервис, который создаёт app.py. Он же обслуживает
        # аккаунт по умолчанию — иначе экран «Подключение» и сборщик
        # работали бы с разными объектами и разошлись бы в состоянии.
        self.primary = primary
        self._services: dict[int, TelegramService] = {}

    # ---- lookup -------------------------------------------------------
    def ensure_primary_row(self) -> int | None:
        """Заводит запись для уже существующей сессии, если её ещё нет.

        Без этого у пользователя, который обновился со старой версии,
        аккаунт есть, а строки о нём нет — и экран «Аккаунты» выглядел бы
        пустым при работающем сборе.
        """
        if self.db.list_accounts():
            return self.db.default_account()["id"]
        if not Path(self.config.session_path).exists():
            return None
        return self.db.add_account("Мой аккаунт", PRIMARY_SESSION_FILE, make_default=True)

    def service_for(self, account_id: int | None) -> TelegramService:
        """Клиент для аккаунта. None (или неизвестный id) — основной."""
        if account_id is None:
            return self.primary
        default = self.db.default_account()
        if default is not None and account_id == default["id"]:
            return self.primary
        if account_id in self._services:
            return self._services[account_id]
        row = self.db.get_account(account_id)
        if row is None:
            # Аккаунт удалили, а чат ещё на него ссылается: собирать
            # основным лучше, чем не собирать вовсе.
            _logger.warning("аккаунт %s не найден, беру основной", account_id)
            return self.primary
        service = TelegramService(self.config)
        service.session_path_override = str(self.paths.session_dir / row["session_file"])
        self._services[account_id] = service
        return service

    def for_chat(self, chat) -> TelegramService:
        return self.service_for(chat["account_id"] if "account_id" in chat.keys() else None)

    def known_services(self) -> list[TelegramService]:
        """Только уже созданные — то, что надо обойти при остановке."""
        return [self.primary, *self._services.values()]

    # ---- lifecycle ----------------------------------------------------
    async def disconnect_all(self) -> None:
        for service in self._services.values():
            try:
                await service.disconnect()
            except Exception:
                _logger.debug("не удалось отключить дополнительный аккаунт", exc_info=True)

    def forget(self, account_id: int) -> None:
        self._services.pop(account_id, None)

    def session_path_for(self, account_id: int) -> Path | None:
        row = self.db.get_account(account_id)
        if row is None:
            return None
        return self.paths.session_dir / row["session_file"]
