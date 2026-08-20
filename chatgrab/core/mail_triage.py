"""П7: scores an incoming mail message for "is this a real request, not a
newsletter" — a pure function, no sqlite/Qt/network, so it's testable
without a database and the "Разбор" screen can run it live against
whatever's on the form right now, before the user even saves. Every
signal that needs data this module can't see on its own (the direction
catalogue, whether the sender already has a lead, whether "we've" already
replied in this thread) is looked up by the caller (MailService) and
handed in as an already-computed field or a plain list — see score()'s
docstring for the exact shape.
"""
from __future__ import annotations

import re

CATEGORY_REQUEST = "запрос"
CATEGORY_ORDER = "заказ"
CATEGORY_INVOICE = "счёт"
CATEGORY_BULK = "рассылка"
CATEGORY_OTHER = "прочее"

# «Запросные обороты» — the checklist's own four examples, plus the
# е/ё spelling of "объём" (real-world typing habit, not scope creep).
REQUEST_PHRASES = [
    "пришлите кп", "стоимость", "наличие", "объём", "объем",
]
ORDER_PHRASES = ["заказ", "заказать", "оформить заказ"]
INVOICE_PHRASES = ["счёт", "счет", "оплата", "оплатить", "инвойс"]

# "Признаки заявки" — checklist names «заявка», «спецификация», and
# "таблица"; a table is recognised by the attachment's own extension
# (nobody names a file "таблица.xlsx"), not by matching that word.
_LEAD_FILENAME_WORDS = ["заявка", "спецификация"]
_SPREADSHEET_EXTENSIONS = (".xlsx", ".xls", ".csv", ".ods")

_NOREPLY_RE = re.compile(r"^(no-?reply|notification)s?@", re.IGNORECASE)

DEFAULT_WEIGHTS: dict[str, int] = {
    "direction_keyword": 40,
    "request_phrase": 25,
    "lead_like_attachment": 20,
    "known_sender": 15,
    "reply_in_thread": 15,
    "direction_stop_word": -30,
    "bulk_signal": -40,
    "noreply_address": -35,
}

DEFAULTS: dict = {
    "weights": dict(DEFAULT_WEIGHTS),
    "threshold": 50,
    "max_notifications_per_tick": 5,
    "llm_borderline_enabled": False,
}

_WEIGHT_BOUNDS = (-100, 100)
_THRESHOLD_BOUNDS = (-200, 200)
_CAP_BOUNDS = (1, 100)


def normalize(raw: dict | None) -> dict:
    """Settings for the «Почта → Разбор» screen, with defaults filled in
    and every value clamped — same discipline as bots/settings.py, a hand-
    edited or stale stored value can only hurt the feature it configures,
    never anything it can't (this app_settings key is triage-only)."""
    out = {
        "weights": dict(DEFAULT_WEIGHTS),
        "threshold": DEFAULTS["threshold"],
        "max_notifications_per_tick": DEFAULTS["max_notifications_per_tick"],
        "llm_borderline_enabled": DEFAULTS["llm_borderline_enabled"],
    }
    if not isinstance(raw, dict):
        return out
    raw_weights = raw.get("weights")
    if isinstance(raw_weights, dict):
        lo, hi = _WEIGHT_BOUNDS
        for key in DEFAULT_WEIGHTS:
            if key not in raw_weights:
                continue
            try:
                out["weights"][key] = max(lo, min(hi, int(raw_weights[key])))
            except (TypeError, ValueError):
                continue
    if "threshold" in raw:
        try:
            lo, hi = _THRESHOLD_BOUNDS
            out["threshold"] = max(lo, min(hi, int(raw["threshold"])))
        except (TypeError, ValueError):
            pass
    if "max_notifications_per_tick" in raw:
        try:
            lo, hi = _CAP_BOUNDS
            out["max_notifications_per_tick"] = max(lo, min(hi, int(raw["max_notifications_per_tick"])))
        except (TypeError, ValueError):
            pass
    if "llm_borderline_enabled" in raw:
        out["llm_borderline_enabled"] = bool(raw["llm_borderline_enabled"])
    return out


def _contains_any(haystack: str, phrases: list[str]) -> str | None:
    low = haystack.lower()
    for phrase in phrases:
        if phrase in low:
            return phrase
    return None


def _lead_like_attachment(filenames: list[str]) -> str | None:
    for name in filenames:
        low = (name or "").lower()
        if any(word in low for word in _LEAD_FILENAME_WORDS):
            return name
        if low.endswith(_SPREADSHEET_EXTENSIONS):
            return name
    return None


def score(fields: dict, attachments_text: str, directions: list, settings: dict) -> tuple[int, str, list[str]]:
    """fields — parsed message data the caller already has or computed:
        subject (str), body_text (str), sender_address (str | None),
        has_list_unsubscribe (bool), is_bulk_precedence (bool),
        attachment_filenames (list[str]),
        known_sender (bool) — sender/domain already has a lead,
        reply_in_thread (bool) — this thread already has a message *from*
        the mailbox's own address, i.e. "we" already answered here once.
    attachments_text — extracted text of every attachment, concatenated
        (П3's mail_attachment.extracted_text), "" if none/not extracted yet.
    directions — the direction catalogue, each item exposing "name",
        "keywords" and "stop_words" — plain dicts or sqlite3.Row both
        work (only [] indexing is used), but keywords/stop_words must
        already be decoded lists, not the JSON text direction.keywords
        is stored as in the database — the caller's job, same as every
        other already-parsed field this function takes.
    settings — a normalize()'d settings dict (weights/threshold/etc);
        pass normalize(raw) yourself, this function trusts it as-is.

    Returns (score, category, reasons) — reasons is human-readable, in
    the order signals were evaluated, each one naming its actual applied
    weight from `settings` (not the hard-coded default), since a screen
    that lets the user retune those weights has to explain itself with
    the numbers actually in effect, not the ones that used to be there.
    """
    weights = settings.get("weights", DEFAULT_WEIGHTS)
    subject = fields.get("subject") or ""
    body = fields.get("body_text") or ""
    haystack = f"{subject}\n{body}\n{attachments_text or ''}"

    total = 0
    reasons: list[str] = []

    keyword_hit = None
    stop_word_hit = None
    for direction in directions:
        # direction["x"] works the same way for a plain dict and a
        # sqlite3.Row (both support column/key indexing) — deliberately
        # not "x in direction" to check for a key: on a Row that tests
        # *value* membership instead (Row iterates like a tuple, not a
        # mapping), which silently always came back False here.
        keywords = direction["keywords"] or []
        stop_words = direction["stop_words"] or []
        name = direction["name"]
        if keyword_hit is None:
            found = _contains_any(haystack, keywords or [])
            if found:
                keyword_hit = (name, found)
        if stop_word_hit is None:
            found = _contains_any(haystack, stop_words or [])
            if found:
                stop_word_hit = (name, found)

    if keyword_hit is not None:
        w = weights["direction_keyword"]
        total += w
        reasons.append(f"ключевое слово направления «{keyword_hit[1]}» ({keyword_hit[0]}) ({w:+d})")

    request_hit = _contains_any(haystack, REQUEST_PHRASES)
    if request_hit:
        w = weights["request_phrase"]
        total += w
        reasons.append(f"запросный оборот «{request_hit}» ({w:+d})")

    attachment_hit = _lead_like_attachment(fields.get("attachment_filenames") or [])
    if attachment_hit:
        w = weights["lead_like_attachment"]
        total += w
        reasons.append(f"вложение похоже на заявку: «{attachment_hit}» ({w:+d})")

    if fields.get("known_sender"):
        w = weights["known_sender"]
        total += w
        reasons.append(f"отправитель уже встречался в заявках ({w:+d})")

    if fields.get("reply_in_thread"):
        w = weights["reply_in_thread"]
        total += w
        reasons.append(f"ответ в цепочке, где мы уже писали ({w:+d})")

    if stop_word_hit is not None:
        w = weights["direction_stop_word"]
        total += w
        reasons.append(f"стоп-слово направления «{stop_word_hit[1]}» ({stop_word_hit[0]}) ({w:+d})")

    bulk_hit = bool(fields.get("has_list_unsubscribe") or fields.get("is_bulk_precedence"))
    if bulk_hit:
        w = weights["bulk_signal"]
        total += w
        reasons.append(f"признаки массовой рассылки: List-Unsubscribe/Precedence ({w:+d})")

    sender = (fields.get("sender_address") or "").strip()
    noreply_hit = bool(_NOREPLY_RE.match(sender))
    if noreply_hit:
        w = weights["noreply_address"]
        total += w
        reasons.append(f"адрес отправителя похож на no-reply ({w:+d})")

    order_hit = _contains_any(haystack, ORDER_PHRASES)
    invoice_hit = _contains_any(haystack, INVOICE_PHRASES)
    if bulk_hit or noreply_hit:
        category = CATEGORY_BULK
    elif invoice_hit:
        category = CATEGORY_INVOICE
    elif order_hit:
        category = CATEGORY_ORDER
    elif request_hit or keyword_hit is not None:
        category = CATEGORY_REQUEST
    else:
        category = CATEGORY_OTHER

    return total, category, reasons
