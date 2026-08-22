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

import re

import aiohttp

from ..db.database import Database
from ..security import SecurityService

SETTING_KEY = "bitrix_webhook_url"

# С7/С10: which Bitrix24 lead-stage STATUS_ID each ChatGrab funnel stage
# maps onto — {str(funnel_stage.id): bitrix_status_id}. One dict for the
# whole app (not per-direction), so it lives as plain JSON in app_settings
# rather than a schema column, the same way any other single-value app
# setting does — see db.get_setting/set_setting.
#
# Keyed by the stage's own numeric id, not its code string: a status
# *code* is only unique within one funnel (funnel_stage.UNIQUE is
# (funnel_id, code)), so two different funnels are free to both define a
# stage coded "won" with different meanings — a code-keyed map would
# conflate them the moment a second funnel (П9) exists. A stage's id is
# a real AUTOINCREMENT primary key, globally unique by construction, so
# it's what status_id_for_lead() below resolves through instead.
STATUS_MAP_KEY = "bitrix_status_map"

# What decides whether a lead's Bitrix24 sync gets queued automatically:
# never (button on the card only), once it reaches a real sales stage, or
# every lead regardless of stage. Manual is the default — a fresh install
# with a webhook just pasted in shouldn't start pushing leads anywhere
# until the mapping above has actually been set up.
AUTO_SEND_MANUAL = "manual"
AUTO_SEND_QUALIFIED = "qualified"
AUTO_SEND_ALL = "all"
AUTO_SEND_POLICIES = [AUTO_SEND_MANUAL, AUTO_SEND_QUALIFIED, AUTO_SEND_ALL]
AUTO_SEND_POLICY_KEY = "bitrix_auto_send_policy"
AUTO_SEND_POLICY_LABELS = {
    AUTO_SEND_MANUAL: "только по кнопке на карточке",
    AUTO_SEND_QUALIFIED: "автоматически, начиная с «квалифицирован»",
    AUTO_SEND_ALL: "автоматически, все заявки",
}

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


# ---- status/policy settings (С7, plain JSON, not secrets) -----------------
def get_status_map(db: Database) -> dict:
    return db.get_setting(STATUS_MAP_KEY, {}) or {}


def set_status_map(db: Database, mapping: dict) -> None:
    # Empty values are noise, not a mapping — drop them so an unmapped
    # status reads the same whether it was never touched or cleared.
    cleaned = {k: v for k, v in (mapping or {}).items() if v}
    db.set_setting(STATUS_MAP_KEY, cleaned)


def status_id_for_lead(db: Database, lead, status_map: dict | None = None) -> str | None:
    """Resolves one lead's own funnel_stage (via its funnel_id + status
    code) to the Bitrix STATUS_ID mapped for that stage's id — С10's
    stage-id-keyed indirection, see STATUS_MAP_KEY's docstring. None if
    the lead's stage can't be resolved (a stale/foreign status) or
    simply isn't mapped — lead_fields() already treats that as "omit
    STATUS_ID", not an error."""
    if not lead["funnel_id"]:
        return None
    stage = db.get_funnel_stage_by_code(lead["funnel_id"], lead["status"])
    if stage is None:
        return None
    if status_map is None:
        status_map = get_status_map(db)
    return status_map.get(str(stage["id"]))


def get_auto_send_policy(db: Database) -> str:
    policy = db.get_setting(AUTO_SEND_POLICY_KEY, AUTO_SEND_MANUAL)
    return policy if policy in AUTO_SEND_POLICIES else AUTO_SEND_MANUAL


def set_auto_send_policy(db: Database, policy: str) -> None:
    if policy not in AUTO_SEND_POLICIES:
        raise ValueError(f"неизвестная политика отправки: {policy!r}")
    db.set_setting(AUTO_SEND_POLICY_KEY, policy)


def register_bitrix_rotation(db: Database, security: SecurityService) -> None:
    """Same reasoning as bots/crypto.py's register_bot_token_rotation —
    without this, the stored webhook URL would silently become
    undecryptable the next time the master password's key changed. Р3:
    delegates to SecurityService.register_setting_rotation() instead of
    its own copy — see that method's docstring."""
    security.register_setting_rotation(
        db, SETTING_KEY, "Bitrix webhook URL unrecoverable during key rotation")


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

    async def list_statuses(self, entity_id: str) -> list[dict]:
        """crm.status.list, filtered to one ENTITY_ID — "STATUS" for a
        lead's stage, "SOURCE" for where it came from. Both mapping
        pickers on the Bitrix24 screen call this to fill their dropdowns
        with the portal's real values instead of asking the user to type
        a STATUS_ID/SOURCE_ID by hand."""
        result = await self.call("crm.status.list", {"filter": {"ENTITY_ID": entity_id}})
        return result or []


# ---- field mapping ----------------------------------------------------
def lead_fields(lead, direction, status_id: str | None = None) -> dict:
    """core.lead row (+ its direction row, or None) -> crm.lead.add/update
    FIELDS. status_id is the already-resolved Bitrix STATUS_ID for this
    lead's own funnel stage (see status_id_for_lead() — resolving it
    needs db access this function deliberately doesn't have, so the
    caller does that lookup and hands in the result) — None simply omits
    STATUS_ID, which leaves the lead on whatever stage Bitrix put it on
    when it was created, better than sending a guessed value. SOURCE_ID
    comes from the lead's direction when that direction has been mapped
    (direction.crm_source_id); an unmapped or missing direction falls
    back to "OTHER" rather than failing the send — a lead without a
    CRM-source mapping still must not be lost."""
    handle = lead["display_name"] or lead["username"] or \
        (f"тг {lead['tg_user_id']}" if lead["tg_user_id"] else "без имени")
    title_bits = [b for b in (lead["product"], direction["name"] if direction else None) if b]
    title = " — ".join(title_bits) if title_bits else f"Заявка от {handle}"

    comment_bits = []
    for rus, key in (("Объём", "volume"), ("Срок", "deadline"), ("Город", "city"), ("Доставка", "delivery")):
        if lead[key]:
            comment_bits.append(f"{rus}: {lead[key]}")
    comments = "; ".join(comment_bits)

    source_id = None
    if direction is not None:
        try:
            source_id = direction["crm_source_id"]
        except (IndexError, KeyError):
            source_id = None

    fields = {
        "TITLE": title,
        "NAME": lead["display_name"] or "",
        "COMMENTS": comments,
        "SOURCE_ID": source_id or "OTHER",
    }
    if status_id:
        fields["STATUS_ID"] = status_id
    if lead["phone"]:
        fields["PHONE"] = [{"VALUE": lead["phone"], "VALUE_TYPE": "WORK"}]
    if lead["email"]:
        fields["EMAIL"] = [{"VALUE": lead["email"], "VALUE_TYPE": "WORK"}]
    im_value = lead["username"] or (str(lead["tg_user_id"]) if lead["tg_user_id"] else None)
    if im_value:
        fields["IM"] = [{"VALUE": im_value, "VALUE_TYPE": "TELEGRAM"}]
    return fields
