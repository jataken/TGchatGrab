"""Schema creation and safe migrations.

All statements are idempotent (CREATE TABLE IF NOT EXISTS / column-presence
checks before ALTER TABLE), so calling migrate() on an existing user
database only ever adds what's missing. Schema version is tracked in
app_meta so future versions can add incremental migration steps.
"""
from __future__ import annotations

import sqlite3

CURRENT_SCHEMA_VERSION = 1

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
    views INTEGER,
    link TEXT,
    is_hidden INTEGER NOT NULL DEFAULT 0,
    char_len INTEGER NOT NULL DEFAULT 0,
    is_reply INTEGER NOT NULL DEFAULT 0,
    is_forward INTEGER NOT NULL DEFAULT 0,
    UNIQUE(chat_id, message_id)
);
"""

_DDL_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_messages_chat_msgid ON messages(chat_id, message_id);",
    "CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date);",
    "CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(chat_id, sender_id);",
    "CREATE INDEX IF NOT EXISTS idx_messages_hidden ON messages(is_hidden);",
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

_DDL_STAT_CACHE = """
CREATE TABLE IF NOT EXISTS chat_stat_cache (
    chat_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (chat_id, day)
);
"""

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


def _fts_needs_build(conn: sqlite3.Connection) -> bool:
    fts_count = conn.execute("SELECT count(*) FROM messages_fts").fetchone()[0]
    msg_count = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
    return msg_count > 0 and fts_count == 0


def migrate(conn: sqlite3.Connection, on_fts_progress=None) -> None:
    """Bring the database up to CURRENT_SCHEMA_VERSION. Safe to call every
    startup, on any existing user database."""
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=OFF;")

    conn.execute(_DDL_META)
    for ddl in _ALL_TABLE_DDL:
        conn.execute(ddl)
    for ddl in _DDL_INDEXES:
        conn.execute(ddl)
    conn.execute(_DDL_FTS)
    for ddl in _FTS_TRIGGERS:
        conn.execute(ddl)
    conn.commit()

    # One-off FTS index build for a database that had messages before the
    # FTS5 table existed (or the table was just created above).
    if _fts_needs_build(conn):
        _build_fts_index(conn, on_progress=on_fts_progress)

    version = _get_meta(conn, "schema_version")
    if version is None:
        _set_meta(conn, "schema_version", str(CURRENT_SCHEMA_VERSION))
        conn.commit()


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
