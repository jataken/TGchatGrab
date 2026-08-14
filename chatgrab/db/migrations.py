"""Numbered schema migrations: a tracked list of steps, applied in order,
with a mandatory backup before any of them touch an existing database.

Everything through schema v6 was one big idempotent function — safe to
re-run on every startup, self-healing if a table ever went missing, but
with no record of *what* had been applied and no way to test a single
change in isolation. That function is now step "006" below (baseline),
body untouched (see schema._apply_baseline) and still marked
self_healing=True, so it keeps that exact contract: it runs every single
call to run(), regardless of the tracking table. Only 006 physically
exists as an entry here — 001 through 005 were the pre-existing ad-hoc
column checks that already shipped inside that function; re-deriving them
as separate numbered steps would test nothing new, since every database
in the wild has already been through them.

From 007 onward, a schema change is a normal one-shot Migration: applied
once, recorded, then left alone — so a rollback (see rollback_last, used
by the migration test) actually stays rolled back rather than being
silently reapplied on the next launch. down() is written only where it's
a single reversible statement (a column or table this step alone added).
A fuller rollback story wasn't worth building here: the mandatory backup
below covers what down() can't undo (a column drop, a table rebuild), so
writing an unwind for every step would duplicate that safety net rather
than add to it — see PLAN.md, С1, "Решение по откату".

Requirement for anything added to MIGRATIONS after 007: up() must be safe
to call more than once if you ever set self_healing=True for it (don't —
that flag exists for the baseline alone). For an ordinary one-shot step,
up() only ever runs while its id is absent from schema_migrations, so it
doesn't need its own idempotency guard the way the baseline's internals
do.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from typing import Callable

from . import schema

_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Migration:
    id: str
    name: str
    up: Callable[[sqlite3.Connection, dict], None]
    down: Callable[[sqlite3.Connection], None] | None = None
    # True only for the 006 baseline — see module docstring. Every
    # migration after it is a normal tracked one-shot step instead.
    self_healing: bool = False


def _up_baseline(conn: sqlite3.Connection, ctx: dict) -> None:
    schema._apply_baseline(conn, on_fts_progress=ctx.get("on_fts_progress"))


def _up_directions(conn: sqlite3.Connection, _ctx: dict) -> None:
    conn.execute(schema._DDL_DIRECTION)
    for ddl in schema._DDL_DIRECTION_INDEXES:
        conn.execute(ddl)


def _down_directions(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS direction;")


MIGRATIONS: list[Migration] = [
    Migration("006", "baseline (schema through v6, folded from ad-hoc checks)",
              _up_baseline, self_healing=True),
    Migration("007", "direction catalogue", _up_directions, _down_directions),
]


def _applied_ids(conn: sqlite3.Connection) -> set[str]:
    conn.execute(_MIGRATIONS_TABLE)
    return {row[0] for row in conn.execute("SELECT id FROM schema_migrations")}


def _has_existing_schema(conn: sqlite3.Connection) -> bool:
    """Anything worth backing up before we touch it — a brand-new install
    has an empty file and nothing to lose."""
    row = conn.execute(
        "SELECT count(*) FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND name != 'schema_migrations'"
    ).fetchone()
    return row[0] > 0


def run(conn: sqlite3.Connection, on_fts_progress=None, on_backup=None) -> None:
    """Apply the baseline (always) and every not-yet-applied migration
    after it, in order.

    on_backup, if given, is called once — before anything runs — with the
    connection and the id of the first pending migration, but only when
    this database already had a schema to protect. It's the caller's job
    to actually copy the file (see Database.__init__): this module works
    with a bare sqlite3.Connection and has no path to back up from, and
    both existing migration tests call schema.migrate() the same way
    directly on a hand-built connection, so on_backup stays optional.
    """
    ids_before = _applied_ids(conn)
    pending = [m for m in MIGRATIONS if m.id not in ids_before]

    if pending and on_backup is not None and _has_existing_schema(conn):
        on_backup(conn, pending[0].id)

    ctx = {"on_fts_progress": on_fts_progress}
    now = dt.datetime.now().isoformat(timespec="seconds")
    for migration in MIGRATIONS:
        already_applied = migration.id in ids_before
        if migration.self_healing or not already_applied:
            migration.up(conn, ctx)
        if not already_applied:
            conn.execute(
                "INSERT INTO schema_migrations(id, name, applied_at) VALUES (?, ?, ?)",
                (migration.id, migration.name, now),
            )
    conn.commit()


def rollback_last(conn: sqlite3.Connection) -> str | None:
    """Undo the most recently applied migration that has a down() — used
    by the rollback test, and available for a future "undo last upgrade"
    button. Returns the id undone, or None if nothing was reversible.

    Deliberately doesn't call run() afterward and doesn't need to: this
    is a standalone check that down() cleans up exactly what up() added,
    on a copy of the database. Calling run() again on that copy would
    naturally reapply the step (that's normal migration semantics, not a
    bug in this function) — the caller decides whether that's wanted.
    """
    applied = sorted(_applied_ids(conn), reverse=True)
    by_id = {m.id: m for m in MIGRATIONS}
    for migration_id in applied:
        migration = by_id.get(migration_id)
        if migration is None or migration.down is None:
            continue
        migration.down(conn)
        conn.execute("DELETE FROM schema_migrations WHERE id = ?", (migration_id,))
        conn.commit()
        return migration_id
    return None
