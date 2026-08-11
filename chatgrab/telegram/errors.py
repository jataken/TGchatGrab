"""Translate Telethon exceptions into plain Russian messages, with no
Telegram-API jargon, for display in the UI."""
from __future__ import annotations

from telethon.errors import (
    ApiIdInvalidError,
    ChannelPrivateError,
    ChatAdminRequiredError,
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    RPCError,
    SessionPasswordNeededError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.errors import AuthKeyUnregisteredError


def humanize_error(exc: Exception) -> str:
    """Never raises — called from every error-handling path in the UI, so
    a bug here must degrade to a generic message, not compound the
    original failure with a second, unhandled one."""
    try:
        return _humanize(exc)
    except Exception:
        try:
            return str(exc) or exc.__class__.__name__
        except Exception:
            return "Неизвестная ошибка."


def _humanize(exc: Exception) -> str:
    if isinstance(exc, PhoneCodeInvalidError):
        return "Неверный код. Проверьте цифры и попробуйте снова."
    if isinstance(exc, PhoneCodeExpiredError):
        return "Срок действия кода истёк. Нажмите «Прислать новый код»."
    if isinstance(exc, PhoneNumberInvalidError):
        return "Похоже, номер телефона указан неверно."
    if isinstance(exc, PhoneNumberBannedError):
        return "Этот номер заблокирован в Telegram."
    if isinstance(exc, PasswordHashInvalidError):
        return "Неверный пароль двухэтапной проверки."
    if isinstance(exc, SessionPasswordNeededError):
        return "Нужен пароль двухэтапной проверки."
    if isinstance(exc, FloodWaitError):
        return f"Telegram попросил подождать {exc.seconds} с."
    if isinstance(exc, ChannelPrivateError):
        return "Чат недоступен — возможно, вы не состоите в нём или доступ закрыт."
    if isinstance(exc, ChatAdminRequiredError):
        return "Для этого действия в чате нужны права администратора."
    if isinstance(exc, (UsernameNotOccupiedError, UsernameInvalidError)):
        return "Такого чата не существует — проверьте ссылку или имя."
    if isinstance(exc, ApiIdInvalidError):
        return "Ключ приложения (api_id/api_hash) неверен. Проверьте настройки."
    if isinstance(exc, AuthKeyUnregisteredError):
        return "Вход недействителен — войдите заново на экране «Подключение»."
    if isinstance(exc, RPCError):
        return f"Telegram отклонил запрос: {exc.__class__.__name__}."
    return str(exc) or exc.__class__.__name__
