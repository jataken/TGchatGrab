"""Schema DDL and the pre-numbered-migrations baseline.

Everything below CURRENT_SCHEMA_VERSION and _apply_baseline() is the schema
as it stood through v6, unchanged in behaviour — column-presence checks
before ALTER TABLE, idempotent CREATE TABLE IF NOT EXISTS. It is now step
"006_baseline" in db/migrations.py rather than a version number: databases
that skipped a release still get repaired, exactly as before.

Schema changes from here on are numbered migrations — see db/migrations.py
for the runner, the tracking table, and how the pre-upgrade backup is
wired in. This file keeps owning the DDL text itself (new tables/columns
get their CREATE/ALTER statements here, referenced by a migration step
there) since that's where every existing table already lives.
"""
from __future__ import annotations

import sqlite3

CURRENT_SCHEMA_VERSION = 6

_DDL_META = """
CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

_DDL_SETTINGS = """
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

_DDL_CHATS = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    username TEXT,
    is_tracked INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    depth_mode TEXT NOT NULL DEFAULT 'all',
    depth_from_date TEXT,
    status TEXT NOT NULL DEFAULT 'idle',
    last_error TEXT,
    history_done INTEGER NOT NULL DEFAULT 0,
    oldest_loaded_id INTEGER,
    newest_loaded_id INTEGER,
    approx_total INTEGER,
    created_at TEXT NOT NULL,
    queue_order INTEGER
);
"""

_DDL_MESSAGES = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    chat_title TEXT,
    date TEXT NOT NULL,
    edited_date TEXT,
    sender_id INTEGER,
    sender_username TEXT,
    sender_display_name TEXT,
    text TEXT NOT NULL DEFAULT '',
    reply_to_message_id INTEGER,
    forwarded_from TEXT,
    media_type TEXT,
    media_caption TEXT,
    photo_path TEXT,
    media_path TEXT,
    views INTEGER,
    link TEXT,
    is_hidden INTEGER NOT NULL DEFAULT 0,
    char_len INTEGER NOT NULL DEFAULT 0,
    is_reply INTEGER NOT NULL DEFAULT 0,
    is_forward INTEGER NOT NULL DEFAULT 0,
    text_hash TEXT,                       -- normalized-text fingerprint, see db/dedup.py
    UNIQUE(chat_id, message_id)
);
"""

_DDL_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_messages_chat_msgid ON messages(chat_id, message_id);",
    "CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date);",
    "CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(chat_id, sender_id);",
    "CREATE INDEX IF NOT EXISTS idx_messages_hidden ON messages(is_hidden);",
    # Finding the earliest message carrying a given fingerprint within a
    # chat — the query «только уникальные» runs on every export.
    "CREATE INDEX IF NOT EXISTS idx_messages_text_hash ON messages(chat_id, text_hash, message_id);",
    "CREATE INDEX IF NOT EXISTS idx_watch_hit_seen ON watch_hit(seen, matched_at);",
    "CREATE INDEX IF NOT EXISTS idx_watch_hit_msg ON watch_hit(chat_id, message_id);",
]

_DDL_EXPORT_LOG = """
CREATE TABLE IF NOT EXISTS export_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    chat_ids TEXT NOT NULL,
    format TEXT NOT NULL,
    date_from TEXT,
    date_to TEXT,
    merge INTEGER NOT NULL,
    split_mode TEXT NOT NULL,
    token_limit INTEGER,
    incremental INTEGER NOT NULL,
    zip_photos INTEGER NOT NULL,
    include_hidden INTEGER NOT NULL DEFAULT 0,
    max_message_id_by_chat TEXT NOT NULL,
    output_paths TEXT NOT NULL,
    preset_name TEXT
);
"""

_DDL_EXPORT_PRESET = """
CREATE TABLE IF NOT EXISTS export_preset (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    params TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

_DDL_IGNORE_RULE = """
CREATE TABLE IF NOT EXISTS ignore_rule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_type TEXT NOT NULL,
    value TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'global',
    chat_id INTEGER,
    created_at TEXT NOT NULL
);
"""

_DDL_SEARCH_PRESET = """
CREATE TABLE IF NOT EXISTS search_preset (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    params TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

# Words worth being told about the moment they appear, without standing up
# a bot for it. A rule matches new messages as they arrive; every match is
# recorded so the feed survives a restart and can be marked read.
_DDL_WATCH_RULE = """
CREATE TABLE IF NOT EXISTS watch_rule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phrase TEXT NOT NULL,
    chat_id INTEGER,                          -- NULL = во всех отслеживаемых чатах
    enabled INTEGER NOT NULL DEFAULT 1,
    notify INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
"""

_DDL_WATCH_HIT = """
CREATE TABLE IF NOT EXISTS watch_hit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    matched_at TEXT NOT NULL,
    seen INTEGER NOT NULL DEFAULT 0,
    UNIQUE(rule_id, chat_id, message_id)
);
"""

# One row per saved export that should run by itself.
_DDL_EXPORT_SCHEDULE = """
CREATE TABLE IF NOT EXISTS export_schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    preset_name TEXT NOT NULL,
    every_hours INTEGER NOT NULL DEFAULT 168,
    at_hour INTEGER NOT NULL DEFAULT 9,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run_at TEXT,
    last_result TEXT,
    created_at TEXT NOT NULL
);
"""

_DDL_ACCOUNT = """
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT,
    session_file TEXT NOT NULL UNIQUE,   -- имя файла внутри sessions_dir
    enabled INTEGER NOT NULL DEFAULT 1,
    is_default INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL
);
"""

# A flat list, deliberately — one person's five directions don't need
# line items, units, or an owner field. Feeds the price file into price
# requests, the keywords into chat search and monitoring, and later the
# direction into the lead form and the Bitrix24 mapping.
_DDL_DIRECTION = """
CREATE TABLE IF NOT EXISTS direction (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    keywords TEXT NOT NULL DEFAULT '[]',
    stop_words TEXT NOT NULL DEFAULT '[]',
    price_file TEXT,
    note TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    order_index INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""

_DDL_DIRECTION_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_direction_order ON direction(order_index);",
]

_DDL_STAT_CACHE = """
CREATE TABLE IF NOT EXISTS chat_stat_cache (
    chat_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (chat_id, day)
);
"""

# ---- bot constructor ---------------------------------------------------
# bot_id is the isolation key throughout: every table below either carries
# it directly or reaches it via a foreign row, so a future server-side
# multi-account version only needs to add a tenant/user key alongside it,
# not restructure the model.

_DDL_BOTS = """
CREATE TABLE IF NOT EXISTS bots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT NOT NULL,                       -- userbot | bot_api
    token_encrypted TEXT,                     -- bot_api only
    preset TEXT NOT NULL DEFAULT 'custom',    -- b2b | b2c | custom
    manager_chat_id TEXT,
    status TEXT NOT NULL DEFAULT 'stopped',   -- running | stopped | error
    last_error TEXT,
    created_at TEXT NOT NULL,
    settings TEXT NOT NULL DEFAULT '{}'       -- sending limits, see bots/settings.py
);
"""

_DDL_BOT_TRIGGERS = """
CREATE TABLE IF NOT EXISTS bot_triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    type TEXT NOT NULL,                       -- keyword | command | incoming_dm | chat_message | schedule | inactivity
    config TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
"""

_DDL_BOT_ACTIONS = """
CREATE TABLE IF NOT EXISTS bot_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_id INTEGER NOT NULL,
    type TEXT NOT NULL,                       -- send_dm | run_scenario | save_lead | forward_lead | tag | notify
    config TEXT NOT NULL DEFAULT '{}',
    order_index INTEGER NOT NULL DEFAULT 0
);
"""

_DDL_BOT_CONTACTS = """
CREATE TABLE IF NOT EXISTS bot_contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL UNIQUE,
    username TEXT,
    first_seen TEXT NOT NULL,
    last_active TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT 'organic'    -- organic | parsed
);
"""

_DDL_BOT_LEADS = """
CREATE TABLE IF NOT EXISTS bot_leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER NOT NULL,
    bot_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',       -- new | in_progress | closed
    manager TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '{}'
);
"""

# New columns folded onto bot_leads by migration "008" — see
# db/migrations.py. Kept as a list rather than inline ALTER statements so
# the migration step can loop over it and stay a one-liner per column.
# contact_id/status/manager/content/created_at/updated_at above are
# untouched: existing callers of add_lead() keep working unmodified.
_LEAD_NEW_COLUMNS = [
    ("tg_user_id", "INTEGER"),
    ("username", "TEXT"),
    ("display_name", "TEXT"),
    ("phone", "TEXT"),
    ("email", "TEXT"),
    ("source_chat_id", "INTEGER"),
    ("source_type", "TEXT NOT NULL DEFAULT 'bot'"),      # chat | dm | bot | manual
    ("direction_id", "INTEGER"),
    ("product", "TEXT"),
    ("volume", "TEXT"),
    ("unit", "TEXT"),
    ("deadline", "TEXT"),
    ("city", "TEXT"),
    ("delivery", "TEXT"),
    ("owner", "TEXT NOT NULL DEFAULT 'local_user'"),      # see core/lead.py, DEFAULT_OWNER
    ("reject_reason", "TEXT"),
    ("attachments", "TEXT NOT NULL DEFAULT '[]'"),        # КП files — paths, same as direction.price_file
]

# One row per change to a lead worth remembering — status moves, notes,
# eventually reminders and CRM sync. What makes a lead a *card* instead of
# a database row: the history is what the card actually shows.
_DDL_LEAD_EVENTS = """
CREATE TABLE IF NOT EXISTS lead_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL,
    kind TEXT NOT NULL,                       -- created | status | note | reminder | sync
    from_status TEXT,
    to_status TEXT,
    text TEXT,
    source TEXT NOT NULL,                     -- manual | scenario | rule | integration | migration
    created_at TEXT NOT NULL
);
"""

_DDL_LEAD_EVENTS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_lead_events_lead ON lead_events(lead_id, created_at);",
]

# Migration "009" — see db/migrations.py._up_lead_lifecycle. A reminder is
# just two more nullable columns, added the same one-liner-per-column way
# as migration 008's batch.
_LEAD_LIFECYCLE_COLUMNS = [
    ("next_action_at", "TEXT"),        # напоминание: когда вернуться к лиду
    ("next_action_text", "TEXT"),
]

_BOT_LEADS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_bot_leads_bot ON bot_leads(bot_id);",
    "CREATE INDEX IF NOT EXISTS idx_bot_leads_contact ON bot_leads(contact_id);",
]


def _bot_leads_needs_nullable_ids(conn: sqlite3.Connection) -> bool:
    info = conn.execute("PRAGMA table_info(bot_leads)").fetchall()
    return any(r[1] == "contact_id" and r[3] for r in info)  # r[3] is the notnull flag


def _relax_bot_leads_ids(conn: sqlite3.Connection) -> None:
    """Drops NOT NULL from bot_leads.contact_id/bot_id — С3 adds leads
    that never touch a bot (created by hand, or from a collected message
    or watch hit), and both columns were only ever NOT NULL because every
    lead used to come from a bot rule. SQLite can't alter a column
    constraint in place, so the table is rebuilt: the same copy/drop/
    rename dance as `_migrate_scenario_sessions_unique` above, just for a
    different constraint. A lead's own identity fields (tg_user_id/
    username/display_name/phone/email, migration 008) and source_type
    already cover "who" and "where from" without a contact or a bot, so
    this is schema catching up to that, not a new idea.

    ВНИМАНИЕ: список колонок здесь фиксирован на момент миграции 009.
    Всё, что добавляется в bot_leads позже, должно добавляться в
    migrate() ПОСЛЕ вызова этой функции — иначе перестройка молча
    выбросит новую колонку.
    """
    if not _bot_leads_needs_nullable_ids(conn):
        return
    conn.execute("DROP TABLE IF EXISTS bot_leads_new;")
    conn.execute(
        """
        CREATE TABLE bot_leads_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_id INTEGER,
            bot_id INTEGER,
            status TEXT NOT NULL DEFAULT 'new',
            manager TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '{}',
            tg_user_id INTEGER,
            username TEXT,
            display_name TEXT,
            phone TEXT,
            email TEXT,
            source_chat_id INTEGER,
            source_type TEXT NOT NULL DEFAULT 'bot',
            direction_id INTEGER,
            product TEXT,
            volume TEXT,
            unit TEXT,
            deadline TEXT,
            city TEXT,
            delivery TEXT,
            owner TEXT NOT NULL DEFAULT 'local_user',
            reject_reason TEXT,
            attachments TEXT NOT NULL DEFAULT '[]'
        );
        """
    )
    conn.execute(
        """
        INSERT INTO bot_leads_new
            (id, contact_id, bot_id, status, manager, created_at, updated_at, content,
             tg_user_id, username, display_name, phone, email,
             source_chat_id, source_type, direction_id, product, volume, unit,
             deadline, city, delivery, owner, reject_reason, attachments)
        SELECT id, contact_id, bot_id, status, manager, created_at, updated_at, content,
               tg_user_id, username, display_name, phone, email,
               source_chat_id, source_type, direction_id, product, volume, unit,
               deadline, city, delivery, owner, reject_reason, attachments
        FROM bot_leads;
        """
    )
    conn.execute("DROP TABLE bot_leads;")
    conn.execute("ALTER TABLE bot_leads_new RENAME TO bot_leads;")


# Migration "010" — see db/migrations.py._up_outbox. Every send a bot
# attempts, whatever the outcome, so "have we ever messaged this contact"
# and the hour/day/first-message counters have something to query instead
# of guessing from bot_activity_log (which only ever logs inbound events).
_DDL_OUTBOX_SENDS = """
CREATE TABLE IF NOT EXISTS outbox_sends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    target TEXT NOT NULL,
    status TEXT NOT NULL,                     -- sent | blocked | dry_run
    is_first INTEGER NOT NULL DEFAULT 0,
    text TEXT,
    created_at TEXT NOT NULL
);
"""
_DDL_OUTBOX_SENDS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_outbox_sends_bot_time ON outbox_sends(bot_id, created_at);",
    "CREATE INDEX IF NOT EXISTS idx_outbox_sends_target ON outbox_sends(bot_id, target, created_at);",
]

# A cold first message (no reply just triggered it, and this contact has
# never been sent anything) waits here for a click instead of going out —
# invariant 6. sent_at/dismissed_at both NULL means still pending.
_DDL_OUTBOX_DRAFTS = """
CREATE TABLE IF NOT EXISTS outbox_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    target TEXT NOT NULL,
    text TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    dismissed_at TEXT
);
"""
_DDL_OUTBOX_DRAFTS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_outbox_drafts_pending "
    "ON outbox_drafts(bot_id, sent_at, dismissed_at);",
]

# Scoped per bot, same as bots.settings — not per Telegram account. Several
# bots sharing one account each keep their own list rather than a shared
# one; see PLAN.md's С4 journal for why that's an accepted simplification.
_DDL_OUTBOX_BLACKLIST = """
CREATE TABLE IF NOT EXISTS outbox_blacklist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    target TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(bot_id, target)
);
"""

# Migration "011" — see db/migrations.py._up_bitrix. A lead's own link to
# its Bitrix24 CRM record — crm_id absent means never synced.
_LEAD_CRM_COLUMNS = [
    ("crm_id", "TEXT"),
    ("crm_synced_at", "TEXT"),
]

# One pending send per lead (UNIQUE(lead_id) — enqueueing an already-queued
# lead just resets next_attempt_at rather than piling up a second row).
# Rows are deleted on success; a failure stays here with a growing
# attempts count and a pushed-back next_attempt_at, so "the network was
# down" and "Bitrix is still rejecting this" look the same from here —
# both just mean "still pending" — and neither ever loses the lead.
_DDL_CRM_QUEUE = """
CREATE TABLE IF NOT EXISTS crm_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL UNIQUE,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error TEXT,
    created_at TEXT NOT NULL
);
"""
_DDL_CRM_QUEUE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_crm_queue_due ON crm_queue(next_attempt_at);",
]

# Migration "012" — see db/migrations.py._up_bitrix_mapping. Which Bitrix24
# CRM source a direction's leads get tagged with — NULL means unmapped,
# integrations/bitrix.py's lead_fields() falls back to SOURCE_ID "OTHER".
# The lead-status -> Bitrix STATUS_ID mapping doesn't get a column: it's
# one dict for the whole app, not a per-row attribute, so it lives as JSON
# in app_settings instead (see bitrix.STATUS_MAP_KEY).
_DIRECTION_CRM_COLUMNS = [
    ("crm_source_id", "TEXT"),
]

_DDL_BOT_ACTIVITY_LOG = """
CREATE TABLE IF NOT EXISTS bot_activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER,
    bot_id INTEGER,
    chat_id INTEGER,
    message_id INTEGER,
    timestamp TEXT NOT NULL,
    chat_type TEXT,                           -- dm | group | channel
    kind TEXT NOT NULL DEFAULT 'message'      -- message | trigger_fired | error
);
"""

_DDL_BOT_TEMPLATES = """
CREATE TABLE IF NOT EXISTS bot_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER,
    name TEXT NOT NULL,
    text TEXT NOT NULL,
    variables TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
"""

_DDL_BOT_SCENARIOS = """
CREATE TABLE IF NOT EXISTS bot_scenarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    steps TEXT NOT NULL DEFAULT '[]',
    kind TEXT NOT NULL DEFAULT 'linear',  -- linear | branching, см. bots/scenario_engine.py
    created_at TEXT NOT NULL,
    done_template_id INTEGER          -- confirmation sent once all steps are answered
);
"""

# One row per in-flight (or finished) scripted dialog with a contact — not
# in the original spec's table list, but required for it to actually work:
# without persisted step/answer state, restarting the app mid-conversation
# would silently lose where a contact was in the scenario.
#
# Uniqueness is deliberately NOT a table constraint here. Schema v2 carried
# UNIQUE(bot_id, contact_telegram_id, status), which allowed only one row
# per status per contact: a contact going through a scenario a *second*
# time collided with their own earlier 'done' row on the final step, the
# write failed, and their finished answers never became a lead. What the
# rule actually needs to say is "at most one *active* dialog per contact" —
# expressed as the partial unique index below, leaving finished runs free
# to accumulate as the per-contact history the funnel is computed from.
_DDL_BOT_SCENARIO_SESSIONS = """
CREATE TABLE IF NOT EXISTS bot_scenario_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL,
    scenario_id INTEGER NOT NULL,
    contact_telegram_id INTEGER NOT NULL,
    step_index INTEGER NOT NULL DEFAULT 0,
    step_id TEXT,                             -- ветвящийся сценарий ходит по id, не по номеру
    answers TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',    -- active | done | abandoned
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_DDL_BOT_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_bot_triggers_bot ON bot_triggers(bot_id);",
    "CREATE INDEX IF NOT EXISTS idx_bot_actions_trigger ON bot_actions(trigger_id);",
    "CREATE INDEX IF NOT EXISTS idx_bot_leads_bot ON bot_leads(bot_id);",
    "CREATE INDEX IF NOT EXISTS idx_bot_leads_contact ON bot_leads(contact_id);",
    "CREATE INDEX IF NOT EXISTS idx_bot_activity_contact ON bot_activity_log(contact_id);",
    "CREATE INDEX IF NOT EXISTS idx_bot_activity_bot ON bot_activity_log(bot_id);",
    "CREATE INDEX IF NOT EXISTS idx_bot_scenarios_bot ON bot_scenarios(bot_id);",
    "CREATE INDEX IF NOT EXISTS idx_bot_templates_bot ON bot_templates(bot_id);",
    "CREATE INDEX IF NOT EXISTS idx_bot_contacts_telegram_id ON bot_contacts(telegram_id);",
    "CREATE INDEX IF NOT EXISTS idx_bot_scenario_sessions_lookup "
    "ON bot_scenario_sessions(bot_id, contact_telegram_id, status);",
    # The real invariant: one active dialog per contact per bot. Finished
    # ('done'/'abandoned') runs are unconstrained history.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_bot_scenario_sessions_one_active "
    "ON bot_scenario_sessions(bot_id, contact_telegram_id) WHERE status = 'active';",
]

_DDL_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text, media_caption, content='messages', content_rowid='id'
);
"""

_FTS_TRIGGERS = [
    """
    CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
        INSERT INTO messages_fts(rowid, text, media_caption)
        VALUES (new.id, new.text, new.media_caption);
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, text, media_caption)
        VALUES ('delete', old.id, old.text, old.media_caption);
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, text, media_caption)
        VALUES ('delete', old.id, old.text, old.media_caption);
        INSERT INTO messages_fts(rowid, text, media_caption)
        VALUES (new.id, new.text, new.media_caption);
    END;
    """,
]

_ALL_TABLE_DDL = [
    _DDL_META, _DDL_SETTINGS, _DDL_CHATS, _DDL_MESSAGES,
    _DDL_EXPORT_LOG, _DDL_EXPORT_PRESET, _DDL_IGNORE_RULE, _DDL_STAT_CACHE,
    _DDL_SEARCH_PRESET, _DDL_WATCH_RULE, _DDL_WATCH_HIT, _DDL_EXPORT_SCHEDULE,
    _DDL_ACCOUNT,
    _DDL_BOTS, _DDL_BOT_TRIGGERS, _DDL_BOT_ACTIONS, _DDL_BOT_CONTACTS,
    _DDL_BOT_LEADS, _DDL_BOT_ACTIVITY_LOG, _DDL_BOT_TEMPLATES,
    _DDL_BOT_SCENARIOS, _DDL_BOT_SCENARIO_SESSIONS,
]


def _get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _migrate_scenario_sessions_unique(conn: sqlite3.Connection) -> None:
    """Drop schema v2's UNIQUE(bot_id, contact_telegram_id, status) from
    bot_scenario_sessions.

    That constraint made a returning contact's second run through a
    scenario fail on its last step (their own earlier 'done' row was in the
    way), losing the collected answers instead of filing a lead. SQLite
    cannot drop a table constraint in place, so the table is rebuilt: the
    12-step dance is unnecessary here since there are no foreign keys or
    views pointing at it — copy, drop, rename, reindex.

    Detected from the stored DDL rather than a version number, so a
    database that skipped versions (or was created by a build in between)
    is still repaired exactly once.

    ВНИМАНИЕ: список колонок здесь зафиксирован. Всё, что добавляется в эту
    таблицу позже, должно добавляться в migrate() ПОСЛЕ вызова этой
    функции — иначе перестройка молча выбросит новую колонку.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'bot_scenario_sessions'"
    ).fetchone()
    if not row or not row[0] or "UNIQUE" not in row[0].upper():
        return

    conn.execute("DROP TABLE IF EXISTS bot_scenario_sessions_new;")
    conn.execute(
        """
        CREATE TABLE bot_scenario_sessions_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id INTEGER NOT NULL,
            scenario_id INTEGER NOT NULL,
            contact_telegram_id INTEGER NOT NULL,
            step_index INTEGER NOT NULL DEFAULT 0,
            answers TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'active',
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        INSERT INTO bot_scenario_sessions_new
            (id, bot_id, scenario_id, contact_telegram_id, step_index,
             answers, status, started_at, updated_at)
        SELECT id, bot_id, scenario_id, contact_telegram_id, step_index,
               answers, status, started_at, updated_at
        FROM bot_scenario_sessions;
        """
    )
    conn.execute("DROP TABLE bot_scenario_sessions;")
    conn.execute("ALTER TABLE bot_scenario_sessions_new RENAME TO bot_scenario_sessions;")
    conn.commit()


def _fts_needs_build(conn: sqlite3.Connection) -> bool:
    fts_count = conn.execute("SELECT count(*) FROM messages_fts").fetchone()[0]
    msg_count = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
    return msg_count > 0 and fts_count == 0


def _apply_baseline(conn: sqlite3.Connection, on_fts_progress=None) -> None:
    """Bring the database up to CURRENT_SCHEMA_VERSION (v6). This is
    migration step "006_baseline" in db/migrations.py — body unchanged
    from when it was the whole of migrate(), safe to call every startup
    on any existing user database."""
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=OFF;")

    conn.execute(_DDL_META)
    for ddl in _ALL_TABLE_DDL:
        conn.execute(ddl)

    # media_path generalizes the old photo-only column to cover video/voice/
    # document downloads too — added after photo_path existed in the wild,
    # so an existing database needs it added and backfilled explicitly.
    if not _column_exists(conn, "messages", "media_path"):
        conn.execute("ALTER TABLE messages ADD COLUMN media_path TEXT;")
        conn.execute(
            "UPDATE messages SET media_path = photo_path WHERE photo_path IS NOT NULL AND photo_path != '';"
        )

    # The confirmation a contact gets once they've answered every step.
    # Added after bot_scenarios shipped, so existing databases need it.
    if not _column_exists(conn, "bot_scenarios", "done_template_id"):
        conn.execute("ALTER TABLE bot_scenarios ADD COLUMN done_template_id INTEGER;")

    # Per-bot sending limits (cooldown, gap between sends, reminders per
    # tick). Previously a module constant, which meant the one setting
    # protecting the user's phone number from a restriction was invisible
    # and unchangeable.
    if not _column_exists(conn, "bots", "settings"):
        conn.execute("ALTER TABLE bots ADD COLUMN settings TEXT NOT NULL DEFAULT '{}';")

    # Repeated-text fingerprint. Backfilled for an existing database so
    # «только уникальные» works on already-collected history, not just on
    # messages arriving from now on.
    if not _column_exists(conn, "messages", "text_hash"):
        conn.execute("ALTER TABLE messages ADD COLUMN text_hash TEXT;")
        conn.commit()
        _backfill_text_hashes(conn, on_progress=on_fts_progress)

    # Which account collects a chat, and which account a userbot speaks
    # from. NULL means «основной» — that is what every existing row is,
    # so a single-account database keeps behaving exactly as before.
    if not _column_exists(conn, "chats", "account_id"):
        conn.execute("ALTER TABLE chats ADD COLUMN account_id INTEGER;")
    if not _column_exists(conn, "bots", "account_id"):
        conn.execute("ALTER TABLE bots ADD COLUMN account_id INTEGER;")

    # Ветвление рядом с линейным, а не вместо него: у пользователя
    # планируются разные боты под разные задачи, и уже настроенный
    # пошаговый сценарий должен продолжать работать как работал.
    if not _column_exists(conn, "bot_scenarios", "kind"):
        conn.execute("ALTER TABLE bot_scenarios ADD COLUMN kind TEXT NOT NULL DEFAULT 'linear';")

    # Must run before the bot indexes below: the partial unique index they
    # create belongs on the rebuilt table, not the old constrained one.
    _migrate_scenario_sessions_unique(conn)

    # ПОСЛЕ перестройки таблицы выше, не до неё. Перестройка пересоздаёт
    # bot_scenario_sessions по фиксированному списку колонок, поэтому
    # колонка, добавленная раньше, тихо исчезала бы — база из v2 получала
    # схему без step_id, и ветвящийся сценарий падал бы только у тех, кто
    # обновился с очень старой версии.
    if not _column_exists(conn, "bot_scenario_sessions", "step_id"):
        conn.execute("ALTER TABLE bot_scenario_sessions ADD COLUMN step_id TEXT;")

    for ddl in _DDL_INDEXES:
        conn.execute(ddl)
    for ddl in _DDL_BOT_INDEXES:
        conn.execute(ddl)
    conn.execute(_DDL_FTS)
    for ddl in _FTS_TRIGGERS:
        conn.execute(ddl)
    conn.commit()

    # One-off FTS index build for a database that had messages before the
    # FTS5 table existed (or the table was just created above).
    if _fts_needs_build(conn):
        _build_fts_index(conn, on_progress=on_fts_progress)

    # Migrations above are all detected from the actual schema rather than
    # this number, so the stored version is a record of what ran, not the
    # gate for it — a database that skipped a release still gets repaired.
    version = _get_meta(conn, "schema_version")
    if version != str(CURRENT_SCHEMA_VERSION):
        _set_meta(conn, "schema_version", str(CURRENT_SCHEMA_VERSION))
        conn.commit()


def migrate(conn: sqlite3.Connection, on_fts_progress=None, on_backup=None) -> None:
    """Public entry point — unchanged signature plus one new optional
    kwarg, so every existing caller (Database.__init__, both test files
    that hand-build an old database and migrate it directly) keeps working
    without modification.

    Delegates to db/migrations.py, which tracks numbered steps beyond this
    baseline and backs up the file before applying any of them. Imported
    here rather than at module level: migrations.py imports this module
    for the DDL and _apply_baseline, so importing it back at the top of
    this file would be circular.
    """
    from . import migrations
    migrations.run(conn, on_fts_progress=on_fts_progress, on_backup=on_backup)


def _backfill_text_hashes(conn: sqlite3.Connection, on_progress=None, batch_size: int = 2000) -> None:
    """Fill text_hash for rows collected before the column existed. Batched
    and committed as it goes, the same way the FTS build is, so a large
    existing database doesn't hold one enormous transaction."""
    from .dedup import fingerprint

    total = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
    done = 0
    last_id = 0
    while True:
        rows = conn.execute(
            "SELECT id, text FROM messages WHERE id > ? ORDER BY id LIMIT ?",
            (last_id, batch_size),
        ).fetchall()
        if not rows:
            break
        conn.executemany(
            "UPDATE messages SET text_hash = ? WHERE id = ?",
            [(fingerprint(text or ""), row_id) for row_id, text in rows],
        )
        conn.commit()
        last_id = rows[-1][0]
        done += len(rows)
        if on_progress:
            on_progress(done, total)


def _build_fts_index(conn: sqlite3.Connection, on_progress=None, batch_size: int = 2000) -> None:
    total = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
    done = 0
    last_id = 0
    while True:
        rows = conn.execute(
            "SELECT id, text, media_caption FROM messages WHERE id > ? ORDER BY id LIMIT ?",
            (last_id, batch_size),
        ).fetchall()
        if not rows:
            break
        conn.executemany(
            "INSERT INTO messages_fts(rowid, text, media_caption) VALUES (?, ?, ?)",
            rows,
        )
        conn.commit()
        last_id = rows[-1][0]
        done += len(rows)
        if on_progress:
            on_progress(done, total)
