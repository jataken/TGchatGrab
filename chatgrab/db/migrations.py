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
from ..core import lead as lead_domain

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


def _up_leads(conn: sqlite3.Connection, _ctx: dict) -> None:
    """bot_leads becomes a real lead: identity/business columns, plus
    lead_events for history. The additive part is a one-liner per column
    (see schema._LEAD_NEW_COLUMNS); the part worth reading is the backfill
    below, which is the only place data actually changes shape.

    No down() — unlike 007's plain DROP TABLE, this touches an existing
    table's data (the status remap), not just adds one it can cleanly
    undo. The mandatory backup before this runs is the safety net here,
    same as for any other non-trivial step — see the module docstring.
    """
    for name, coltype in schema._LEAD_NEW_COLUMNS:
        if not schema._column_exists(conn, "bot_leads", name):
            conn.execute(f"ALTER TABLE bot_leads ADD COLUMN {name} {coltype};")
    conn.execute(schema._DDL_LEAD_EVENTS)
    for ddl in schema._DDL_LEAD_EVENTS_INDEXES:
        conn.execute(ddl)

    # Backfill: every pre-existing lead gets a first history row (an empty
    # timeline reads as broken, not as "nothing happened yet"), and the
    # old three-status model maps onto the funnel. 'closed' is the one
    # lossy mapping — it used to mean both "won" and "we're done here,
    # lost or otherwise" — so it additionally gets a flagged status event
    # instead of a silent rename, per PLAN.md С2's "риск" note.
    now = dt.datetime.now().isoformat(timespec="seconds")
    rows = conn.execute("SELECT id, status FROM bot_leads").fetchall()
    for lead_id, old_status in rows:
        conn.execute(
            "INSERT INTO lead_events(lead_id, kind, source, text, created_at) "
            "VALUES (?, 'created', 'migration', 'Заявка перенесена из старой модели.', ?)",
            (lead_id, now),
        )
        new_status = lead_domain.remap_legacy_status(old_status)
        if new_status == old_status:
            continue
        conn.execute("UPDATE bot_leads SET status = ? WHERE id = ?", (new_status, lead_id))
        if old_status == "closed":
            conn.execute(
                "INSERT INTO lead_events"
                "(lead_id, kind, from_status, to_status, source, text, created_at) "
                "VALUES (?, 'status', ?, ?, 'migration', ?, ?)",
                (lead_id, old_status, new_status,
                 "Статус перенесён из старой модели «closed» → «сделка». Возможно, "
                 "это был проигранный лид — проверьте и поставьте «отказ», если нужно.",
                 now),
            )


def _up_lead_lifecycle(conn: sqlite3.Connection, _ctx: dict) -> None:
    """С3: a lead can carry a reminder, and can exist without a bot or a
    contact behind it (created by hand, or from a plain collected message
    or watch hit). See schema._relax_bot_leads_ids for why that needs a
    table rebuild rather than a plain ALTER.

    No down() — same reasoning as 008: the rebuild isn't a single
    reversible statement, and the mandatory backup before this runs is
    the safety net, not a hand-written unwind.
    """
    # The rebuild's column list is fixed at the schema this migration was
    # written against — run it before adding the new columns, not after,
    # or the rebuild's own INSERT...SELECT silently drops them.
    schema._relax_bot_leads_ids(conn)
    for name, coltype in schema._LEAD_LIFECYCLE_COLUMNS:
        if not schema._column_exists(conn, "bot_leads", name):
            conn.execute(f"ALTER TABLE bot_leads ADD COLUMN {name} {coltype};")
    for ddl in schema._BOT_LEADS_INDEXES:
        conn.execute(ddl)


def _up_outbox(conn: sqlite3.Connection, _ctx: dict) -> None:
    """С4: three new, purely additive tables backing the outbox layer —
    a log of every send attempt (for the hour/day/first-message counters
    and "have we ever messaged them"), pending drafts, and a per-bot
    blacklist. Nothing existing is touched, so unlike 008/009 this one
    gets a clean down().
    """
    conn.execute(schema._DDL_OUTBOX_SENDS)
    for ddl in schema._DDL_OUTBOX_SENDS_INDEXES:
        conn.execute(ddl)
    conn.execute(schema._DDL_OUTBOX_DRAFTS)
    for ddl in schema._DDL_OUTBOX_DRAFTS_INDEXES:
        conn.execute(ddl)
    conn.execute(schema._DDL_OUTBOX_BLACKLIST)


def _down_outbox(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS outbox_sends;")
    conn.execute("DROP TABLE IF EXISTS outbox_drafts;")
    conn.execute("DROP TABLE IF EXISTS outbox_blacklist;")


def _up_bitrix(conn: sqlite3.Connection, _ctx: dict) -> None:
    """С6: a lead's link to its Bitrix24 CRM record, and the send queue
    that gets it there without ever losing one over a dropped connection.

    No down() — same reasoning as 008/009: this touches bot_leads (two
    new columns), not just adds a standalone table the way 007/010 did,
    so it doesn't get the clean-rollback treatment those two did. The
    mandatory backup before this runs is the safety net.
    """
    for name, coltype in schema._LEAD_CRM_COLUMNS:
        if not schema._column_exists(conn, "bot_leads", name):
            conn.execute(f"ALTER TABLE bot_leads ADD COLUMN {name} {coltype};")
    conn.execute(schema._DDL_CRM_QUEUE)
    for ddl in schema._DDL_CRM_QUEUE_INDEXES:
        conn.execute(ddl)


def _up_bitrix_mapping(conn: sqlite3.Connection, _ctx: dict) -> None:
    """С7: a direction's own Bitrix24 CRM source, set from the mapping UI
    on the new dedicated integrations screen. No down() — same reasoning
    as 008/009/011: an ALTER on an existing table, not a standalone
    addition 007/010's clean rollback fits.
    """
    for name, coltype in schema._DIRECTION_CRM_COLUMNS:
        if not schema._column_exists(conn, "direction", name):
            conn.execute(f"ALTER TABLE direction ADD COLUMN {name} {coltype};")


def _up_mail(conn: sqlite3.Connection, _ctx: dict) -> None:
    """П1: mailboxes, folders, messages, threads (empty until П2 populates
    them), attachments, and full-text search over subject/body — six new,
    standalone tables. Nothing existing is touched (see the П-2 invariant:
    no foreign key from mail_* to chats/messages/bot_leads), so this is
    the clean-rollback shape 007/010 are, not 008/009/011's ALTER-an-
    existing-table one.
    """
    for ddl in (schema._DDL_MAILBOX, schema._DDL_MAIL_FOLDER, schema._DDL_MAIL_THREAD,
                schema._DDL_MAIL_MESSAGE, schema._DDL_MAIL_ATTACHMENT):
        conn.execute(ddl)
    for ddl in schema._DDL_MAIL_INDEXES:
        conn.execute(ddl)
    conn.execute(schema._DDL_MAIL_FTS)
    for ddl in schema._MAIL_FTS_TRIGGERS:
        conn.execute(ddl)


def _down_mail(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS mail_fts;")
    conn.execute("DROP TABLE IF EXISTS mail_attachment;")
    conn.execute("DROP TABLE IF EXISTS mail_message;")
    conn.execute("DROP TABLE IF EXISTS mail_thread;")
    conn.execute("DROP TABLE IF EXISTS mail_folder;")
    conn.execute("DROP TABLE IF EXISTS mailbox;")


def _up_mail_attachments(conn: sqlite3.Connection, _ctx: dict) -> None:
    """П3: attachment text (PDF/docx/xlsx/plain text, extracted on first
    view — see core/mail_attachment_text.py and ui/screens/mail/
    attachment_view.py) becomes searchable. mail_attachment.extracted_text
    already exists — added by 014 itself, ahead of need, per that
    session's journal — so the only new column is mail_message.
    attachments_text, an aggregate kept in step by a trigger on
    mail_attachment (see _MAIL_ATTACHMENT_TEXT_TRIGGERS's docstring).

    FTS5 can't ALTER an existing virtual table's column list, so mail_fts
    itself has to be dropped and recreated at the 3-column shape — which
    empties it, since it's an external-content table with its own
    storage separate from mail_message. The INSERT...SELECT at the end
    repopulates it from every existing message so a pre-015 database
    doesn't silently lose subject/body search until something happens to
    touch each row again.

    No down() — like 008/011, this alters existing tables (an ADD COLUMN
    plus a table rebuild) rather than only adding new standalone ones, so
    it doesn't get 007/010/014's clean-rollback treatment. The mandatory
    backup before this runs is the safety net, same as those two.
    """
    if not schema._column_exists(conn, "mail_message", "attachments_text"):
        conn.execute("ALTER TABLE mail_message ADD COLUMN attachments_text TEXT;")
    for name in ("mail_message_ai", "mail_message_ad", "mail_message_au"):
        conn.execute(f"DROP TRIGGER IF EXISTS {name};")
    conn.execute("DROP TABLE IF EXISTS mail_fts;")
    conn.execute(schema._DDL_MAIL_FTS_V2)
    for ddl in schema._MAIL_FTS_TRIGGERS_V2:
        conn.execute(ddl)
    for ddl in schema._MAIL_ATTACHMENT_TEXT_TRIGGERS:
        conn.execute(ddl)
    conn.execute(
        "UPDATE mail_message SET attachments_text = ("
        " SELECT group_concat(a.extracted_text, ' ') FROM mail_attachment a"
        " WHERE a.message_id = mail_message.id AND a.extracted_text IS NOT NULL"
        ") WHERE EXISTS (SELECT 1 FROM mail_attachment a "
        " WHERE a.message_id = mail_message.id AND a.extracted_text IS NOT NULL);"
    )
    conn.execute(
        "INSERT INTO mail_fts(rowid, subject, body_text, attachments_text) "
        "SELECT id, subject, body_text, attachments_text FROM mail_message;"
    )


def _up_mail_ops(conn: sqlite3.Connection, _ctx: dict) -> None:
    """П4: folder admin (create/rename/delete/subscribe, SPECIAL-USE),
    move/copy/permanent-delete, \\Flagged/\\Answered/forwarded alongside
    \\Seen, and an offline action queue for tag/move/delete actions.
    Three ALTERs plus one new standalone table — no down(), same
    reasoning as 008/011/015: this touches existing tables' shape, not
    only adds ones a rollback could cleanly drop.
    """
    for name, coltype in schema._MAIL_FOLDER_OPS_COLUMNS:
        if not schema._column_exists(conn, "mail_folder", name):
            conn.execute(f"ALTER TABLE mail_folder ADD COLUMN {name} {coltype};")
    for name, coltype in schema._MAIL_MESSAGE_OPS_COLUMNS:
        if not schema._column_exists(conn, "mail_message", name):
            conn.execute(f"ALTER TABLE mail_message ADD COLUMN {name} {coltype};")
    conn.execute(schema._DDL_MAIL_ACTION_QUEUE)
    for ddl in schema._DDL_MAIL_ACTION_QUEUE_INDEXES:
        conn.execute(ddl)


def _up_mail_send(conn: sqlite3.Connection, _ctx: dict) -> None:
    """П5: identities and drafts — two new, standalone tables (plus
    attachments-for-a-draft), nothing existing altered, so this gets the
    clean-rollback shape 007/010/014 do, not 008/011/015/016's ALTER one.
    """
    for ddl in (schema._DDL_MAIL_IDENTITY, schema._DDL_MAIL_DRAFT, schema._DDL_MAIL_DRAFT_ATTACHMENT):
        conn.execute(ddl)
    for ddl in schema._DDL_MAIL_SEND_INDEXES:
        conn.execute(ddl)


def _down_mail_send(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS mail_draft_attachment;")
    conn.execute("DROP TABLE IF EXISTS mail_draft;")
    conn.execute("DROP TABLE IF EXISTS mail_identity;")


MIGRATIONS: list[Migration] = [
    Migration("006", "baseline (schema through v6, folded from ad-hoc checks)",
              _up_baseline, self_healing=True),
    Migration("007", "direction catalogue", _up_directions, _down_directions),
    Migration("008", "lead model + history", _up_leads),
    Migration("009", "lead lifecycle: reminders, leads without a bot/contact", _up_lead_lifecycle),
    Migration("010", "outbox: send log, drafts, blacklist", _up_outbox, _down_outbox),
    Migration("011", "Bitrix24: crm_id on leads, send queue", _up_bitrix),
    Migration("012", "Bitrix24: direction -> CRM source mapping", _up_bitrix_mapping),
    # "013" is reserved for С10 (configurable funnels, PLAN.md) — not yet
    # implemented. Migration ids don't need to be contiguous: the runner
    # tracks applied ids by presence in schema_migrations, not by counting,
    # so "014" landing before "013" exists costs nothing and needs no
    # renumbering once С10 is done.
    Migration("014", "mail: mailboxes, folders, messages, threads, attachments, search",
              _up_mail, _down_mail),
    Migration("015", "mail: attachment text feeds search (attachments_text + mail_fts rebuild)",
              _up_mail_attachments),
    Migration("016", "mail: folder special-use, message flags, offline action queue",
              _up_mail_ops),
    Migration("017", "mail: identities and drafts (sending)", _up_mail_send, _down_mail_send),
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
