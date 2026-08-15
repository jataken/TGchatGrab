"""Bitrix24 CRM: webhook credential storage/rotation, the low-level REST
client, and the lead -> CRM field mapping. The send queue and its drain
loop live in services/bitrix_sync_service.py — this module only knows how
to talk to one portal and how to shape one lead's fields for it.

An incoming webhook URL (`https://<portal>/rest/<user_id>/<token>/`)
carries both identity and permission in one opaque string, unlike a bot
token that's paired with other config — so it's stored as one encrypted
value, not split into portal/user_id/token fields that would just be
concatenated back together at call time.
"""
from __future__ import annotations

import logging
import re

import aiohttp

from ..db.database import Database
from ..security import SecurityService

_logger = logging.getLogger("chatgrab")

SETTING_KEY = "bitrix_webhook_url"

# The token is embedded in the URL path the same way a Bot API token is
# embedded in its own request URL (see bot_api_runner.py's _TOKEN_RE) —
# aiohttp exceptions and Bitrix's own error text can echo the request URL
# verbatim, and this app persists/displays str(exception) as-is in
# chatgrab.log and the UI, so it's redacted before either ever sees it.
_WEBHOOK_TOKEN_RE = re.compile(r"(/rest/\d+/)[A-Za-z0-9]+")


def _redact(text: str) -> str:
    return _WEBHOOK_TOKEN_RE.sub(r"\1<токен скрыт>", text or "")


class BitrixError(Exception):
    pass


# ---- credential storage --------------------------------------------------
def get_webhook_url(db: Database, security: SecurityService) -> str | None:
    stored = db.get_setting(SETTING_KEY)
    if not stored:
        return None
    return security.decrypt_secret(stored)


def set_webhook_url(db: Database, security: SecurityService, url: str | None) -> None:
    if not url or not url.strip():
        db.set_setting(SETTING_KEY, None)
        return
    db.set_setting(SETTING_KEY, security.encrypt_secret(url.strip()))


def register_bitrix_rotation(db: Database, security: SecurityService) -> None:
    """Same reasoning as bots/crypto.py's register_bot_token_rotation —
    without this, the stored webhook URL would silently become
    undecryptable the next time the master password's key changed."""

    def _on_rotate(old_password, old_salt_b64, old_iterations,
                    new_password, new_salt_b64, new_iterations) -> None:
        stored = db.get_setting(SETTING_KEY)
        if not stored:
            return
        try:
            plain = (SecurityService.decrypt_with(stored, old_password, old_salt_b64, old_iterations)
                     if old_password and old_salt_b64 else stored)
        except Exception:
            _logger.warning("Bitrix webhook URL unrecoverable during key rotation")
            return
        new_stored = (SecurityService.encrypt_with(plain, new_password, new_salt_b64, new_iterations)
                      if new_password and new_salt_b64 else plain)
        db.set_setting(SETTING_KEY, new_stored)

    security.add_rotation_listener(_on_rotate)


# ---- REST client ----------------------------------------------------------
class BitrixClient:
    """One aiohttp call per Bitrix REST method — JSON in, `result` out."""

    def __init__(self, webhook_url: str, timeout: float = 20.0):
        self.base_url = webhook_url.rstrip("/") + "/"
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def call(self, method: str, params: dict | None = None):
        url = self.base_url + method + ".json"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.post(url, json=params or {}) as resp:
                    try:
                        data = await resp.json(content_type=None)
                    except Exception as e:
                        raise BitrixError(_redact(f"Bitrix вернул не JSON (код {resp.status}): {e}")) from e
        except aiohttp.ClientError as e:
            raise BitrixError(_redact(f"не удалось связаться с Bitrix24: {e}")) from e
        if not isinstance(data, dict):
            raise BitrixError("Bitrix вернул неожиданный ответ")
        if "error" in data:
            desc = data.get("error_description") or data.get("error")
            raise BitrixError(_redact(f"Bitrix: {desc}"))
        return data.get("result")

    async def ping(self) -> str:
        """For "Проверить подключение" — `profile` works on every webhook
        regardless of which scopes it was granted, so a bad URL/token
        fails as "can't connect" rather than a missing-scope error that
        reads like something else is wrong."""
        result = await self.call("profile")
        name = " ".join(filter(None, [result.get("NAME"), result.get("LAST_NAME")])) \
            if isinstance(result, dict) else ""
        return f"Подключено: {name}" if name else "Подключено."

    async def find_duplicate_lead(self, phone: str | None, email: str | None) -> int | None:
        for kind, value in (("PHONE", phone), ("EMAIL", email)):
            if not value:
                continue
            result = await self.call("crm.duplicate.findbycomm", {"type": kind, "values": [value]})
            leads = (result or {}).get("LEAD") or []
            if leads:
                return int(leads[0])
        return None

    async def add_lead(self, fields: dict) -> int:
        return int(await self.call("crm.lead.add", {"fields": fields}))

    async def update_lead(self, crm_id: str, fields: dict) -> None:
        await self.call("crm.lead.update", {"id": crm_id, "fields": fields})


# ---- field mapping ----------------------------------------------------
def lead_fields(lead, direction) -> dict:
    """core.lead row (+ its direction row, or None) -> crm.lead.add/update
    FIELDS. SOURCE_ID is fixed to "OTHER" for now — a portal-specific
    mapping is С7's own checklist item ("маппинг... настраивается в UI"),
    not this session's."""
    handle = lead["display_name"] or lead["username"] or \
        (f"тг {lead['tg_user_id']}" if lead["tg_user_id"] else "без имени")
    title_bits = [b for b in (lead["product"], direction["name"] if direction else None) if b]
    title = " — ".join(title_bits) if title_bits else f"Заявка от {handle}"

    comment_bits = []
    for rus, key in (("Объём", "volume"), ("Срок", "deadline"), ("Город", "city"), ("Доставка", "delivery")):
        if lead[key]:
            comment_bits.append(f"{rus}: {lead[key]}")
    comments = "; ".join(comment_bits)

    fields = {
        "TITLE": title,
        "NAME": lead["display_name"] or "",
        "COMMENTS": comments,
        "SOURCE_ID": "OTHER",
    }
    if lead["phone"]:
        fields["PHONE"] = [{"VALUE": lead["phone"], "VALUE_TYPE": "WORK"}]
    if lead["email"]:
        fields["EMAIL"] = [{"VALUE": lead["email"], "VALUE_TYPE": "WORK"}]
    im_value = lead["username"] or (str(lead["tg_user_id"]) if lead["tg_user_id"] else None)
    if im_value:
        fields["IM"] = [{"VALUE": im_value, "VALUE_TYPE": "TELEGRAM"}]
    return fields
