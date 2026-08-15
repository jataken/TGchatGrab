"""С9: an optional assistant — off by default — that turns a prompt plus
some already-collected text (a lead's correspondence) into a suggestion:
extracted lead fields, a summary, or a reply draft. Never writes to the
database itself; every call site that uses this module is a button a
human clicks, and every result lands in an editable field the human still
has to confirm (см. lead_card.py's "Помощник" tab) — that's what "работает
как подсказка" means in practice, not just in the docstring.

Talks to the Anthropic Messages API directly over aiohttp rather than
adding the `anthropic` SDK — the same call invariant 8 made for
integrations/bitrix.py in С6: aiohttp is already the app's one HTTP
dependency, and three JSON-in/JSON-out prompts don't earn a second
library. No background service, no tick loop: unlike Bitrix24's send
queue, nothing here ever needs to retry or run unattended — a request
either goes out because a human clicked a button just now, or it never
goes out at all, which is exactly what invariant 6 ("без ключа и без
сети приложение ведёт себя ровно как раньше") requires.
"""
from __future__ import annotations

import json
import logging

import aiohttp

from ..core import lead as lead_domain
from ..db.database import Database
from ..security import SecurityService

_logger = logging.getLogger("chatgrab")

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

SETTING_KEY_ENABLED = "llm_enabled"
SETTING_KEY_API_KEY = "llm_api_key"
SETTING_KEY_MODEL = "llm_model"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"


class LLMError(Exception):
    pass


# ---- settings (enabled flag, key, model) -----------------------------------
def is_enabled(db: Database, security: SecurityService) -> bool:
    """The one gate every call site in this app checks before doing
    anything else — see build_client() below, which every button handler
    goes through instead of repeating this check inline."""
    return bool(db.get_setting(SETTING_KEY_ENABLED, False)) and get_api_key(db, security) is not None


def set_enabled(db: Database, value: bool) -> None:
    db.set_setting(SETTING_KEY_ENABLED, bool(value))


def get_api_key(db: Database, security: SecurityService) -> str | None:
    stored = db.get_setting(SETTING_KEY_API_KEY)
    if not stored:
        return None
    return security.decrypt_secret(stored)


def set_api_key(db: Database, security: SecurityService, key: str | None) -> None:
    if not key or not key.strip():
        db.set_setting(SETTING_KEY_API_KEY, None)
        return
    db.set_setting(SETTING_KEY_API_KEY, security.encrypt_secret(key.strip()))


def get_model(db: Database) -> str:
    return db.get_setting(SETTING_KEY_MODEL, DEFAULT_MODEL) or DEFAULT_MODEL


def set_model(db: Database, model: str) -> None:
    db.set_setting(SETTING_KEY_MODEL, (model or "").strip() or DEFAULT_MODEL)


def register_llm_rotation(db: Database, security: SecurityService) -> None:
    """Same reasoning as integrations/bitrix.py's register_bitrix_rotation
    — without this, the stored API key would silently become
    undecryptable the next time the master password's key changed."""

    def _on_rotate(old_password, old_salt_b64, old_iterations,
                    new_password, new_salt_b64, new_iterations) -> None:
        stored = db.get_setting(SETTING_KEY_API_KEY)
        if not stored:
            return
        try:
            plain = (SecurityService.decrypt_with(stored, old_password, old_salt_b64, old_iterations)
                     if old_password and old_salt_b64 else stored)
        except Exception:
            _logger.warning("Ключ LLM-помощника не восстановлен при смене мастер-пароля")
            return
        new_stored = (SecurityService.encrypt_with(plain, new_password, new_salt_b64, new_iterations)
                      if new_password and new_salt_b64 else plain)
        db.set_setting(SETTING_KEY_API_KEY, new_stored)

    security.add_rotation_listener(_on_rotate)


def build_client(db: Database, security: SecurityService) -> "LLMClient | None":
    """None whenever is_enabled() would be False — the shared "do I have a
    working assistant right now" check every button handler in
    lead_card.py and settings.py's test-connection button uses instead of
    duplicating the enabled+key logic inline."""
    if not is_enabled(db, security):
        return None
    api_key = get_api_key(db, security)
    if not api_key:
        return None
    return LLMClient(api_key, get_model(db))


# ---- prompts ----------------------------------------------------------
_EXTRACT_SYSTEM = (
    "Ты помощник менеджера по продажам сырья, упаковки и промышленной химии "
    "(B2B). Из текста ниже извлеки только то, что там прямо написано — ничего "
    "не придумывай и не оценивай на глаз. Ответь ЧИСТЫМ JSON-объектом, без "
    "пояснений и без markdown-разметки, с любыми из ключей: product, volume, "
    "unit, deadline, city, delivery, phone, email — включай только те ключи, "
    "для которых в тексте действительно есть значение. Если ничего не "
    "нашлось, ответь {}."
)

_SUMMARY_SYSTEM = (
    "Ты помощник менеджера по продажам B2B. Кратко перескажи переписку с "
    "клиентом по-русски: что клиент просит, какие объёмы и сроки называл, "
    "какие возражения были и на чём остановились. 3-6 пунктов списком, без "
    "вступления и заключения."
)

_DRAFT_SYSTEM = (
    "Ты менеджер по продажам сырья, упаковки или промышленной химии, "
    "отвечаешь клиенту в Telegram. По переписке ниже составь короткий, "
    "деловой и вежливый черновик ответа по-русски. Это черновик — его "
    "прочитает и при необходимости поправит человек перед отправкой, "
    "поэтому не придумывай цены, сроки или факты, которых нет в переписке."
)


def parse_field_json(raw: str) -> dict:
    """The model's extraction answer is untrusted the same way any other
    external input is — strip a possible ```json fence, parse, and keep
    only the keys/values this app actually understands (the same
    allow-list core/lead.py's SCENARIO_LEAD_FIELDS already gives the
    scenario engine's own field mapping, so a scenario-collected answer
    and an LLM-extracted one land on identical ground). Anything else —
    bad JSON, a non-object, an invented key, an empty value — is dropped
    rather than surfacing a confusing partial result; the caller is the
    "Извлечь поля" button, and an empty dict there just means "nothing
    found," not an error.
    """
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    allowed = set(lead_domain.SCENARIO_LEAD_FIELDS)
    return {k: str(v).strip() for k, v in data.items()
            if k in allowed and v not in (None, "") and str(v).strip()}


# ---- REST client -----------------------------------------------------
class LLMClient:
    """One aiohttp call per prompt — JSON in, plain text (or, for
    extraction, parsed JSON) out. Mirrors integrations/bitrix.py's
    BitrixClient in shape: a thin wrapper the caller constructs fresh per
    use via build_client(), not a long-lived object anything keeps
    around."""

    def __init__(self, api_key: str, model: str, timeout: float = 30.0):
        self.api_key = api_key
        self.model = model
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def _call(self, system: str, user_text: str, max_tokens: int = 1024) -> str:
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user_text}],
        }
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.post(API_URL, json=body, headers=headers) as resp:
                    try:
                        data = await resp.json(content_type=None)
                    except Exception as e:
                        raise LLMError(f"LLM API вернул не JSON (код {resp.status}): {e}") from e
        except aiohttp.ClientError as e:
            raise LLMError(f"не удалось связаться с LLM API: {e}") from e
        if not isinstance(data, dict):
            raise LLMError("LLM API вернул неожиданный ответ")
        if "error" in data:
            desc = (data.get("error") or {}).get("message") or "неизвестная ошибка"
            raise LLMError(f"LLM API: {desc}")
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
        if not text:
            raise LLMError("LLM API вернул пустой ответ")
        return text

    async def ping(self) -> str:
        """For "Проверить подключение" — same reasoning as
        BitrixClient.ping(): a cheap, small call so a bad key or unknown
        model fails here as "can't connect" instead of during a real
        extraction on a lead card."""
        reply = await self._call(
            "Отвечай ровно одним словом, по-русски.", "Скажи одно слово: готов.", max_tokens=16)
        return reply.strip()

    async def extract_lead_fields(self, text: str) -> dict:
        raw = await self._call(_EXTRACT_SYSTEM, text, max_tokens=512)
        return parse_field_json(raw)

    async def summarize_correspondence(self, text: str) -> str:
        return (await self._call(_SUMMARY_SYSTEM, text, max_tokens=512)).strip()

    async def draft_reply(self, text: str) -> str:
        return (await self._call(_DRAFT_SYSTEM, text, max_tokens=512)).strip()
