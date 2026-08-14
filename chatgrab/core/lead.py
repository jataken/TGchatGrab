"""Lead domain: status codes, the funnel order, and the one rule the app
actually enforces around a status change.

Codes stay English/snake_case, matching every other enum-shaped column in
this schema (chats.status, bots.status, bot_scenarios.kind, and so on) —
Russian only ever appears in a label lookup, never as a stored value. That
keeps comparisons and migrations boring.

"допустимые переходы" (PLAN.md, С2) turned out to mean one concrete rule,
not a state-machine graph: a lead can't be marked "отказ" without a reason.
Everything else — skipping a stage, moving backward after a misclick — is
left alone. A single person's sales process doesn't need a validator for
mistakes only a second person could make.
"""
from __future__ import annotations

NEW = "new"
QUALIFIED = "qualified"
QUOTE_SENT = "quote_sent"
NEGOTIATION = "negotiation"
WON = "won"
LOST = "lost"

# Left-to-right funnel axis (С8's conversion report) and what one click on
# the status pill advances through (leads_tab.py) — LOST sits outside it,
# reachable from anywhere, since a lead can be lost at any stage.
STATUS_ORDER = [NEW, QUALIFIED, QUOTE_SENT, NEGOTIATION, WON]
TERMINAL_STATUSES = {WON, LOST}
ALL_STATUSES = STATUS_ORDER + [LOST]

STATUS_LABELS = {
    NEW: "новый",
    QUALIFIED: "квалифицирован",
    QUOTE_SENT: "отправлено КП",
    NEGOTIATION: "переговоры",
    WON: "сделка",
    LOST: "отказ",
}

# (background, foreground, dot) rgba/hex triples for the status pill —
# plain string data, not a Qt dependency, so it lives here rather than
# being duplicated between leads_tab.py's list and lead_card.py's card.
# Progression: purple (new) → blue → gold → orange → green/red at the
# two ends of the funnel.
STATUS_COLORS = {
    NEW: ("rgba(145,132,217,46)", "#d2cefd", "#b5abfc"),
    QUALIFIED: ("rgba(100,150,220,40)", "#bcd8f7", "#8fbdf0"),
    QUOTE_SENT: ("rgba(220,180,90,40)", "#f5dfa0", "#f0cc70"),
    NEGOTIATION: ("rgba(220,150,90,46)", "#f0c6a0", "#f0c6a0"),
    WON: ("rgba(120,190,150,40)", "#bfe5cd", "#7fc79b"),
    LOST: ("rgba(180,70,90,40)", "#f0c6cf", "#c98a9a"),
}

# Where a lead came from — snapshot at creation, independent of whether
# the underlying chat/bot still exists later.
SOURCE_TYPE_CHAT = "chat"
SOURCE_TYPE_DM = "dm"
SOURCE_TYPE_BOT = "bot"
SOURCE_TYPE_MANUAL = "manual"
SOURCE_TYPE_LABELS = {
    SOURCE_TYPE_CHAT: "из чата",
    SOURCE_TYPE_DM: "из личных сообщений",
    SOURCE_TYPE_BOT: "от бота",
    SOURCE_TYPE_MANUAL: "добавлен вручную",
}

# lead_events.source — who/what made this entry in the history.
EVENT_SOURCE_MANUAL = "manual"
EVENT_SOURCE_SCENARIO = "scenario"
EVENT_SOURCE_RULE = "rule"
EVENT_SOURCE_INTEGRATION = "integration"
# Not in PLAN.md's original list — added for the one thing that writes
# history without a human or a bot involved: the С2 migration remapping
# old three-status leads onto the new funnel.
EVENT_SOURCE_MIGRATION = "migration"
EVENT_SOURCE_LABELS = {
    EVENT_SOURCE_MANUAL: "вручную",
    EVENT_SOURCE_SCENARIO: "сценарий",
    EVENT_SOURCE_RULE: "правило",
    EVENT_SOURCE_INTEGRATION: "интеграция",
    EVENT_SOURCE_MIGRATION: "при обновлении",
}

# lead_events.kind — 'reminder' and 'sync' are only listed here as
# documentation for С3/С6; nothing in С2 writes them yet.
EVENT_KIND_CREATED = "created"
EVENT_KIND_STATUS = "status"
EVENT_KIND_NOTE = "note"
EVENT_KIND_REMINDER = "reminder"
EVENT_KIND_SYNC = "sync"

# The one uppercase concession to a single-user tool that might grow a
# second one later (see PLAN.md invariant 5) — every lead gets this same
# value; nothing in the app currently reads it back.
DEFAULT_OWNER = "local_user"

REJECT_REASONS = [
    "не отвечает",
    "нашёл другого поставщика",
    "не устроила цена",
    "не устроили сроки",
    "передумал",
    "не тот объём",
    "другое",
]

# С3: which real bot_leads column a scenario step's answer can be mapped
# onto (scenario_screen.py's per-step "→ поле лида" picker), so a finished
# scenario can fill in the funnel's own fields instead of leaving
# everything in the free-form `content` JSON. Deliberately not
# direction_id — that needs an id from the directions catalogue, not a
# scenario answer's free text, so it stays a separate, explicit choice.
SCENARIO_LEAD_FIELDS = ["product", "volume", "unit", "deadline", "city", "delivery", "phone", "email"]
SCENARIO_LEAD_FIELD_LABELS = {
    "product": "товар", "volume": "объём", "unit": "единица", "deadline": "срок",
    "city": "город", "delivery": "доставка", "phone": "телефон", "email": "email",
}


def source_type_from_chat_type(chat_type: str | None) -> str:
    """rules_engine.IncomingEvent.chat_type ('dm' | 'group' | 'channel' |
    None) → the lead's own source_type vocabulary. A group or channel
    message is «из чата» regardless of which of the two; nothing in this
    app currently treats them differently for a lead's origin."""
    if chat_type == "dm":
        return SOURCE_TYPE_DM
    if chat_type in ("group", "channel"):
        return SOURCE_TYPE_CHAT
    return SOURCE_TYPE_BOT


def label_for_status(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def label_for_source_type(source_type: str) -> str:
    return SOURCE_TYPE_LABELS.get(source_type, source_type)


def label_for_event_source(source: str) -> str:
    return EVENT_SOURCE_LABELS.get(source, source)


def next_status(status: str) -> str:
    """One step along the funnel — what a single click on the status pill
    advances to. Loops back to NEW from WON ("новая продажа с тем же
    контактом") and from LOST ("даём ещё один шанс"), since both are
    ordinary outcomes for this tool, not something to guard against."""
    if status not in STATUS_ORDER:
        return NEW
    idx = STATUS_ORDER.index(status)
    if idx + 1 < len(STATUS_ORDER):
        return STATUS_ORDER[idx + 1]
    return NEW


def validate_transition(new_status: str, reject_reason: str | None) -> str | None:
    """Returns an error message if the change shouldn't be allowed, or
    None if it's fine. The only hard rule: LOST needs a reason, so a
    future report on why deals fall through has something to read."""
    if new_status not in ALL_STATUSES:
        return f"Неизвестный статус: {new_status!r}"
    if new_status == LOST and not (reject_reason or "").strip():
        return "Нужно указать причину отказа."
    return None


def next_action_due(next_action_at: str | None, now_iso_str: str) -> bool:
    """Whether a reminder should fire — a plain ISO-string compare, kept
    here rather than inline in a query so it's testable without sqlite."""
    return bool(next_action_at) and next_action_at <= now_iso_str


def remap_legacy_status(old_status: str) -> str:
    """The С2 migration's mapping from the three-status model (new /
    in_progress / closed) onto the funnel above. 'closed' is the lossy
    one — it used to mean both "sale done" and "we're done with this,
    lost or otherwise" — so the migration additionally logs a
    lead_events row flagging that guess for review, see db/migrations.py.
    """
    return {
        "new": NEW,
        "in_progress": NEGOTIATION,
        "closed": WON,
    }.get(old_status, NEW)
