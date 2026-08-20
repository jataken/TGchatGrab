"""Lead domain: the funnel-agnostic vocabulary (source types, event kinds,
reject reasons) plus the pure functions that operate on a *given* funnel's
stage list.

С10 moved the funnel itself (what used to be this module's STATUS_ORDER/
ALL_STATUSES/STATUS_LABELS/STATUS_COLORS constants) into database rows —
`funnel`/`funnel_stage`, see db/mixins/funnels.py — because a mailbox's
funnel (П9) needs its own different stages, and a single hardcoded
five-stage vocabulary can't be two different things at once. What's left
here stays pure (no sqlite, no Qt) by taking that stage list as an
explicit parameter instead of importing it — same discipline core/mail_
triage.py and friends already follow: a stage is any object exposing
"code"/"label"/"kind"/"order_index"/"requires_reason" by [] indexing
(a plain dict or a sqlite3.Row both work), `kind` is one of KIND_OPEN/
KIND_WON/KIND_LOST.

Codes stay English/snake_case, matching every other enum-shaped column in
this schema (chats.status, bots.status, bot_scenarios.kind, and so on) —
Russian only ever appears in a label, never as a stored value. That keeps
comparisons and migrations boring.

"допустимые переходы" (PLAN.md, С2) turned out to mean one concrete rule,
not a state-machine graph: a lead can't be marked as a `requires_reason`
stage without a reason. Everything else — skipping a stage, moving
backward after a misclick — is left alone. A single person's sales
process doesn't need a validator for mistakes only a second person could
make.
"""
from __future__ import annotations

KIND_OPEN = "open"
KIND_WON = "won"
KIND_LOST = "lost"
STAGE_KINDS = [KIND_OPEN, KIND_WON, KIND_LOST]

# The default ("Телеграм · биржа") funnel's own stage codes — seeded by
# migration 013 (db/migrations.py: _up_configurable_funnels) and by
# remap_legacy_status() below for a base even older than that. Every
# other funnel is free to reuse or ignore these; they're not "the"
# funnel any more, just this one's.
NEW = "new"
QUALIFIED = "qualified"
QUOTE_SENT = "quote_sent"
NEGOTIATION = "negotiation"
WON = "won"
LOST = "lost"

DEFAULT_FUNNEL_NAME = "Телеграм · биржа"
DEFAULT_FUNNEL_CHANNEL = "telegram"

# (background, foreground, dot) rgba/hex triples — the exact colors the
# old flat STATUS_COLORS dict had, carried over unchanged so a fresh
# migration changes zero pixels for an existing install (С10's own
# acceptance criterion). Progression: purple (new) → blue → gold →
# orange → green/red at the two ends of the funnel.
DEFAULT_FUNNEL_STAGES = [
    {"code": NEW, "label": "новый", "kind": KIND_OPEN, "order_index": 0,
     "requires_reason": False,
     "color_bg": "rgba(145,132,217,46)", "color_fg": "#d2cefd", "color_dot": "#b5abfc"},
    {"code": QUALIFIED, "label": "квалифицирован", "kind": KIND_OPEN, "order_index": 1,
     "requires_reason": False,
     "color_bg": "rgba(100,150,220,40)", "color_fg": "#bcd8f7", "color_dot": "#8fbdf0"},
    {"code": QUOTE_SENT, "label": "отправлено КП", "kind": KIND_OPEN, "order_index": 2,
     "requires_reason": False,
     "color_bg": "rgba(220,180,90,40)", "color_fg": "#f5dfa0", "color_dot": "#f0cc70"},
    {"code": NEGOTIATION, "label": "переговоры", "kind": KIND_OPEN, "order_index": 3,
     "requires_reason": False,
     "color_bg": "rgba(220,150,90,46)", "color_fg": "#f0c6a0", "color_dot": "#f0c6a0"},
    {"code": WON, "label": "сделка", "kind": KIND_WON, "order_index": 4,
     "requires_reason": False,
     "color_bg": "rgba(120,190,150,40)", "color_fg": "#bfe5cd", "color_dot": "#7fc79b"},
    {"code": LOST, "label": "отказ", "kind": KIND_LOST, "order_index": 5,
     "requires_reason": True,
     "color_bg": "rgba(180,70,90,40)", "color_fg": "#f0c6cf", "color_dot": "#c98a9a"},
]

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

# bot_leads.origin_channel — С10: attribution by first touch, doesn't
# change if a lead is later moved to a different funnel (see PLAN.md's
# "не меняется" bullet and db/mixins/leads.py: transfer_lead_funnel()).
ORIGIN_CHANNEL_TELEGRAM = "telegram"
ORIGIN_CHANNEL_EMAIL = "email"
ORIGIN_CHANNEL_LABELS = {
    ORIGIN_CHANNEL_TELEGRAM: "Telegram",
    ORIGIN_CHANNEL_EMAIL: "почта",
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
# С10: a lead moved to a different funnel (see transfer_lead_funnel()) —
# `text` on this event records old/new funnel+stage for the history tab.
EVENT_KIND_FUNNEL = "funnel"

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


def origin_channel_from_source_type(source_type: str) -> str:
    """add_lead()'s default for origin_channel when the caller doesn't
    say otherwise — every existing source_type is Telegram-side; П9 is
    what will ever pass ORIGIN_CHANNEL_EMAIL explicitly."""
    return ORIGIN_CHANNEL_TELEGRAM


def label_for_source_type(source_type: str) -> str:
    return SOURCE_TYPE_LABELS.get(source_type, source_type)


def label_for_origin_channel(channel: str) -> str:
    return ORIGIN_CHANNEL_LABELS.get(channel, channel)


def label_for_event_source(source: str) -> str:
    return EVENT_SOURCE_LABELS.get(source, source)


def stage_for_code(stages: list, code: str):
    """The stage row/dict whose "code" matches, or None — every function
    below that needs one signal's worth of stage data goes through this
    rather than re-scanning `stages` itself."""
    for stage in stages:
        if stage["code"] == code:
            return stage
    return None


def label_for_stage(stages: list, code: str) -> str:
    stage = stage_for_code(stages, code)
    return stage["label"] if stage is not None else code


def open_stages(stages: list) -> list:
    """`stages`, filtered to kind == open and sorted by order_index —
    what next_stage()/bucket_counts() both walk."""
    return sorted((s for s in stages if s["kind"] == KIND_OPEN), key=lambda s: s["order_index"])


def _advanceable_stages(stages: list) -> list:
    """open + won, sorted by order_index — what next_stage() walks. Not
    `lost`: a click never advances a lead *to* lost, same as before С10
    (the old STATUS_ORDER ended at WON, LOST was reachable only by
    explicitly picking it)."""
    return sorted((s for s in stages if s["kind"] in (KIND_OPEN, KIND_WON)),
                  key=lambda s: s["order_index"])


def next_stage(stages: list, current_code: str) -> str:
    """One step along the funnel's open-then-won stages, in order_index
    order — what a single click on the status pill advances to. Loops
    back to the first open stage from the last advanceable one, from a
    `won` stage reached that way, or from a `lost` stage ("новая продажа
    с тем же контактом" / "даём ещё один шанс") — all ordinary outcomes
    for this tool, not something to guard against. Returns
    `current_code` unchanged if the funnel has no open stage at all (a
    degenerate, hand-misconfigured funnel) — nothing sane to advance to."""
    advanceable = _advanceable_stages(stages)
    codes = [s["code"] for s in advanceable]
    opens = [s["code"] for s in open_stages(stages)]
    if not opens:
        return current_code
    if current_code not in codes:
        return opens[0]
    idx = codes.index(current_code)
    return codes[idx + 1] if idx + 1 < len(codes) else opens[0]


def validate_transition(stages: list, new_code: str, reject_reason: str | None) -> str | None:
    """Returns an error message if the change shouldn't be allowed, or
    None if it's fine. The only hard rule: a `requires_reason` stage
    needs one, so a future report on why deals fall through has
    something to read — the same single rule С2 had, now a per-stage
    flag instead of a hardcoded "== LOST" check, settable on any stage
    of any funnel from the funnel-management screen."""
    stage = stage_for_code(stages, new_code)
    if stage is None:
        return f"Неизвестный этап: {new_code!r}"
    if stage["requires_reason"] and not (reject_reason or "").strip():
        return "Нужно указать причину отказа."
    return None


def bucket_for_stage(stages: list, code: str) -> str | None:
    """Which of bucket_counts()'s three buckets ("new"/"in_progress"/
    "closed") a single status code falls into, for a caller that needs
    to filter/list actual lead rows rather than just count them (a
    dashboard's "N новых заявок" row that also shows the newest one, a
    per-lead classification across a flat list spanning several
    funnels). None if `code` isn't any stage of `stages` at all (a
    stale/foreign status) — a caller decides what "unknown" means for
    its own list, this just doesn't guess."""
    stage = stage_for_code(stages, code)
    if stage is None:
        return None
    if stage["kind"] in (KIND_WON, KIND_LOST):
        return "closed"
    opens = open_stages(stages)
    if opens and stage["code"] == opens[0]["code"]:
        return "new"
    return "in_progress"


def bucket_counts(stages: list, status_counts: dict) -> dict:
    """Collapses a funnel's arbitrary stage set into the three coarse
    buckets analytics_tab.py's summary row (and leads_tab.py's header
    line) show — derived from stage `kind` and position instead of
    hardcoded status names, so it works the same for any funnel: the
    first open-kind stage (by order_index) is "new", every other
    open-kind stage is "in_progress", won+lost together are "closed".
    status_counts is {code: count}, e.g. from db.leads_status_counts()."""
    opens = open_stages(stages)
    new_code = opens[0]["code"] if opens else None
    new = status_counts.get(new_code, 0) if new_code else 0
    in_progress = sum(status_counts.get(s["code"], 0) for s in opens if s["code"] != new_code)
    closed = sum(status_counts.get(s["code"], 0) for s in stages if s["kind"] in (KIND_WON, KIND_LOST))
    return {"new": new, "in_progress": in_progress, "closed": closed}


def next_action_due(next_action_at: str | None, now_iso_str: str) -> bool:
    """Whether a reminder should fire — a plain ISO-string compare, kept
    here rather than inline in a query so it's testable without sqlite."""
    return bool(next_action_at) and next_action_at <= now_iso_str


def remap_legacy_status(old_status: str) -> str:
    """The С2 migration's mapping from the three-status model (new /
    in_progress / closed) onto the default funnel's codes above. 'closed'
    is the lossy one — it used to mean both "sale done" and "we're done
    with this, lost or otherwise" — so the migration additionally logs a
    lead_events row flagging that guess for review, see db/migrations.py.
    """
    return {
        "new": NEW,
        "in_progress": NEGOTIATION,
        "closed": WON,
    }.get(old_status, NEW)
