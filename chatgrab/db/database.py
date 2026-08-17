"""Thin, thread-safe SQLite access layer. One connection, one file, guarded
by a lock so it can be safely called both from the asyncio/Qt main thread
and from executor threads (bulk export, backups, VACUUM).

Р1/Р2 (see PLAN.md): the 165 methods that used to all live in this one
file are now split by domain into db/mixins/*.py, composed back together
below. `Database` still has exactly the same public surface it always
did — `db.method(...)` means the same thing it meant before, for every
caller in the app — this file just stopped being where every method body
was written. The low-level layer (connection, lock, execute/query/
transaction) stays here: every mixin depends on it, so it can't be a
mixin itself without creating an import cycle.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from . import schema
from .mixins.accounts import AccountsMixin
from .mixins.chats import ChatsMixin
from .mixins.directions import DirectionsMixin
from .mixins.export import ExportMixin
from .mixins.ignore_rules import IgnoreRulesMixin
from .mixins.maintenance import MaintenanceMixin
from .mixins.productivity import ProductivityMixin
from .mixins.retention import RetentionMixin
from .mixins.search import SearchMixin
from .mixins.settings_kv import SettingsMixin
from .mixins.watch import WatchMixin
from .timeutil import now_iso
from ..core import lead as lead_domain

_logger = logging.getLogger("chatgrab")


class Database(
    SettingsMixin, ChatsMixin, IgnoreRulesMixin, ExportMixin, SearchMixin,
    DirectionsMixin, AccountsMixin, WatchMixin, RetentionMixin, ProductivityMixin,
    MaintenanceMixin,
):
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=OFF;")
        schema.migrate(self._conn, on_backup=self._backup_before_migration)

    def _backup_before_migration(self, conn: sqlite3.Connection, migration_id: str) -> None:
        """Copy the database before schema.migrate() changes anything.

        Uses the same online-backup API as backup_to() below, on the raw
        connection migrate() hands back — not through BackupService,
        which needs an already-migrated Database and would be circular
        this early. Runs once, right before the first migration a given
        database hasn't seen yet; a fresh install has nothing to protect
        (see migrations._has_existing_schema), so it never fires there.
        """
        backup_dir = self.path.parent / "backups" / "pre_migration"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        dest_path = backup_dir / f"chatgrab_before_{migration_id}_{stamp}.db"
        dest = sqlite3.connect(str(dest_path))
        try:
            conn.backup(dest)
        finally:
            dest.close()
        _logger.info("резервная копия перед миграцией %s: %s -> %s",
                     migration_id, self.path, dest_path)

    # ---- low level -------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def executemany(self, sql: str, seq: Iterable[Iterable[Any]]) -> None:
        with self._lock:
            self._conn.executemany(sql, seq)
            self._conn.commit()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def transaction(self):
        return _Transaction(self)

    # ---- bots -----------------------------------------------------------
    def add_bot(self, name: str, type_: str, token_encrypted: str | None,
                preset: str = "custom", manager_chat_id: str | None = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO bots(name, type, token_encrypted, preset, manager_chat_id,
                       status, created_at)
                   VALUES (?, ?, ?, ?, ?, 'stopped', ?)""",
                (name, type_, token_encrypted, preset, manager_chat_id, now_iso()),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_bot(self, bot_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM bots WHERE id = ?", (bot_id,))

    def list_bots(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM bots ORDER BY created_at")

    def set_bot_field(self, bot_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE bots SET {cols} WHERE id = ?", (*fields.values(), bot_id))

    def delete_bot(self, bot_id: int) -> None:
        """Removes the bot's own config (triggers/actions/scenarios/
        templates/scenario sessions). Leads, contacts and activity history
        are kept — they're shared records of real conversations, not bot
        configuration, so they outlive the bot that created them."""
        with self._lock:
            trigger_ids = [r["id"] for r in self._conn.execute(
                "SELECT id FROM bot_triggers WHERE bot_id = ?", (bot_id,)).fetchall()]
            for tid in trigger_ids:
                self._conn.execute("DELETE FROM bot_actions WHERE trigger_id = ?", (tid,))
            self._conn.execute("DELETE FROM bot_triggers WHERE bot_id = ?", (bot_id,))
            self._conn.execute("DELETE FROM bot_scenarios WHERE bot_id = ?", (bot_id,))
            self._conn.execute("DELETE FROM bot_scenario_sessions WHERE bot_id = ?", (bot_id,))
            self._conn.execute("DELETE FROM bot_templates WHERE bot_id = ?", (bot_id,))
            self._conn.execute("DELETE FROM bots WHERE id = ?", (bot_id,))
            self._conn.commit()

    # ---- bot triggers / actions ------------------------------------------
    def add_trigger(self, bot_id: int, type_: str, config: dict, enabled: bool = True) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO bot_triggers(bot_id, type, config, enabled, created_at) VALUES (?, ?, ?, ?, ?)",
                (bot_id, type_, json.dumps(config, ensure_ascii=False), 1 if enabled else 0, now_iso()),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_trigger(self, trigger_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM bot_triggers WHERE id = ?", (trigger_id,))

    def list_triggers(self, bot_id: int, type_: str | None = None) -> list[sqlite3.Row]:
        if type_ is not None:
            return self.query(
                "SELECT * FROM bot_triggers WHERE bot_id = ? AND type = ? AND enabled = 1 ORDER BY id",
                (bot_id, type_),
            )
        return self.query("SELECT * FROM bot_triggers WHERE bot_id = ? ORDER BY id", (bot_id,))

    def set_trigger_field(self, trigger_id: int, **fields: Any) -> None:
        if not fields:
            return
        if "config" in fields and isinstance(fields["config"], dict):
            fields["config"] = json.dumps(fields["config"], ensure_ascii=False)
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE bot_triggers SET {cols} WHERE id = ?", (*fields.values(), trigger_id))

    def delete_trigger(self, trigger_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM bot_actions WHERE trigger_id = ?", (trigger_id,))
            self._conn.execute("DELETE FROM bot_triggers WHERE id = ?", (trigger_id,))
            self._conn.commit()

    def add_action(self, trigger_id: int, type_: str, config: dict, order_index: int = 0) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO bot_actions(trigger_id, type, config, order_index) VALUES (?, ?, ?, ?)",
                (trigger_id, type_, json.dumps(config, ensure_ascii=False), order_index),
            )
            self._conn.commit()
            return cur.lastrowid

    def list_actions(self, trigger_id: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM bot_actions WHERE trigger_id = ? ORDER BY order_index, id", (trigger_id,)
        )

    def set_action_field(self, action_id: int, **fields: Any) -> None:
        if not fields:
            return
        if "config" in fields and isinstance(fields["config"], dict):
            fields["config"] = json.dumps(fields["config"], ensure_ascii=False)
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE bot_actions SET {cols} WHERE id = ?", (*fields.values(), action_id))

    def delete_action(self, action_id: int) -> None:
        self.execute("DELETE FROM bot_actions WHERE id = ?", (action_id,))

    # ---- bot contacts / leads / activity ---------------------------------
    def upsert_contact(self, telegram_id: int, username: str | None = None,
                        source: str = "organic") -> int:
        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM bot_contacts WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
            now = now_iso()
            if existing:
                self._conn.execute(
                    "UPDATE bot_contacts SET username = COALESCE(?, username), last_active = ? WHERE id = ?",
                    (username, now, existing["id"]),
                )
                self._conn.commit()
                return existing["id"]
            cur = self._conn.execute(
                """INSERT INTO bot_contacts(telegram_id, username, first_seen, last_active, source)
                   VALUES (?, ?, ?, ?, ?)""",
                (telegram_id, username, now, now, source),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_contact(self, contact_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM bot_contacts WHERE id = ?", (contact_id,))

    def get_contact_by_telegram_id(self, telegram_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM bot_contacts WHERE telegram_id = ?", (telegram_id,))

    def list_contacts(self, limit: int = 500) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM bot_contacts ORDER BY last_active DESC LIMIT ?", (limit,))

    def set_contact_tags(self, contact_id: int, tags: list[str]) -> None:
        self.execute(
            "UPDATE bot_contacts SET tags = ? WHERE id = ?",
            (json.dumps(tags, ensure_ascii=False), contact_id),
        )

    def add_lead(self, contact_id: int | None, bot_id: int | None, content: dict, status: str = "new",
                 manager: str | None = None, *, tg_user_id: int | None = None,
                 username: str | None = None, display_name: str | None = None,
                 phone: str | None = None, email: str | None = None,
                 source_chat_id: int | None = None,
                 source_type: str = lead_domain.SOURCE_TYPE_BOT,
                 direction_id: int | None = None, product: str | None = None,
                 volume: str | None = None, unit: str | None = None,
                 deadline: str | None = None, city: str | None = None,
                 delivery: str | None = None,
                 event_source: str = lead_domain.EVENT_SOURCE_RULE) -> int:
        """Creates a lead and its opening lead_events row in one go — a
        lead with no history is exactly the "silent database row" the
        card (see ui/screens/bots/lead_card.py) exists to stop being.

        Signature grew rather than gaining a second create_lead(): every
        existing caller (rules_engine's save_lead/run_scenario actions)
        still passes just contact_id/bot_id/content/status, and every new
        field here is optional with a sensible default — a bot-sourced
        lead just doesn't set the ones a human fills in on the card later.

        contact_id/bot_id are None for a lead that never touched a bot —
        created by hand, or from a plain collected message or watch hit
        (С3). Migration 009 made both columns nullable for exactly this.
        """
        with self._lock:
            now = now_iso()
            cur = self._conn.execute(
                """INSERT INTO bot_leads(
                       contact_id, bot_id, status, manager, created_at, updated_at, content,
                       tg_user_id, username, display_name, phone, email,
                       source_chat_id, source_type, direction_id, product, volume, unit,
                       deadline, city, delivery, owner
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (contact_id, bot_id, status, manager, now, now, json.dumps(content, ensure_ascii=False),
                 tg_user_id, username, display_name, phone, email,
                 source_chat_id, source_type, direction_id, product, volume, unit,
                 deadline, city, delivery, lead_domain.DEFAULT_OWNER),
            )
            lead_id = cur.lastrowid
            self._conn.execute(
                "INSERT INTO lead_events(lead_id, kind, source, created_at) "
                "VALUES (?, ?, ?, ?)",
                (lead_id, lead_domain.EVENT_KIND_CREATED, event_source, now),
            )
            self._conn.commit()
            return lead_id

    def get_lead(self, lead_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM bot_leads WHERE id = ?", (lead_id,))

    def list_leads(self, bot_id: int | None = None, status: str | None = None, *,
                    direction_id: int | None = None, source_type: str | None = None,
                    since: str | None = None, until: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM bot_leads"
        clauses, params = [], []
        if bot_id is not None:
            clauses.append("bot_id = ?")
            params.append(bot_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if direction_id is not None:
            clauses.append("direction_id = ?")
            params.append(direction_id)
        if source_type is not None:
            clauses.append("source_type = ?")
            params.append(source_type)
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("created_at <= ?")
            params.append(until)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        # id вторым ключом: время пишется с точностью до секунды, и две
        # заявки, пришедшие в одну секунду, иначе меняются местами между
        # обновлениями списка.
        sql += " ORDER BY created_at DESC, id DESC"
        return self.query(sql, params)

    def leads_status_counts(self, bot_id: int | None = None) -> dict[str, int]:
        """Per-status totals for the funnel row on the leads screen —
        pre-seeded with 0 for every status so the UI never has to guard
        against a missing key."""
        sql = "SELECT status, count(*) AS c FROM bot_leads"
        params: list[Any] = []
        if bot_id is not None:
            sql += " WHERE bot_id = ?"
            params.append(bot_id)
        sql += " GROUP BY status"
        counts = {r["status"]: r["c"] for r in self.query(sql, params)}
        return {s: counts.get(s, 0) for s in lead_domain.ALL_STATUSES}

    def due_lead_reminders(self, now_iso_str: str) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM bot_leads WHERE next_action_at IS NOT NULL AND next_action_at <= ? "
            "ORDER BY next_action_at",
            (now_iso_str,),
        )

    def fire_lead_reminder(self, lead_id: int) -> None:
        """Records the reminder in the lead's history and clears the
        field that made it due — the clearing itself is what keeps a
        reminder from firing twice, including across a restart, since
        due_lead_reminders only ever looks at that same field."""
        lead = self.get_lead(lead_id)
        if lead is None:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE bot_leads SET next_action_at = NULL, next_action_text = NULL, "
                "updated_at = ? WHERE id = ?",
                (now_iso(), lead_id),
            )
            self._conn.execute(
                "INSERT INTO lead_events(lead_id, kind, text, source, created_at) VALUES (?, ?, ?, ?, ?)",
                (lead_id, lead_domain.EVENT_KIND_REMINDER, lead["next_action_text"],
                 lead_domain.EVENT_SOURCE_RULE, now_iso()),
            )
            self._conn.commit()

    def set_lead_field(self, lead_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields = dict(fields)
        if "content" in fields and isinstance(fields["content"], dict):
            fields["content"] = json.dumps(fields["content"], ensure_ascii=False)
        if "attachments" in fields and isinstance(fields["attachments"], list):
            fields["attachments"] = json.dumps(fields["attachments"], ensure_ascii=False)
        fields.setdefault("updated_at", now_iso())
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE bot_leads SET {cols} WHERE id = ?", (*fields.values(), lead_id))

    def set_lead_status(self, lead_id: int, new_status: str, *, reject_reason: str | None = None,
                        source: str = lead_domain.EVENT_SOURCE_MANUAL, text: str | None = None) -> None:
        """The one validated path for changing a lead's stage — enforces
        core.lead's single hard rule (LOST needs a reason) and always logs
        the move, which is what makes set_lead_field unsuitable for this
        one field: a status written through it would change the funnel
        with no trace in the history the card shows.

        Raises ValueError on an invalid move rather than silently
        refusing, so a caller (the card, a future scenario action) finds
        out immediately instead of the change quietly not happening.
        """
        error = lead_domain.validate_transition(new_status, reject_reason)
        if error:
            raise ValueError(error)
        lead = self.get_lead(lead_id)
        if lead is None:
            raise ValueError(f"Заявка {lead_id} не найдена.")
        old_status = lead["status"]
        with self._lock:
            fields = {"status": new_status, "updated_at": now_iso()}
            if new_status == lead_domain.LOST:
                fields["reject_reason"] = reject_reason.strip()
            cols = ", ".join(f"{k} = ?" for k in fields)
            self._conn.execute(f"UPDATE bot_leads SET {cols} WHERE id = ?", (*fields.values(), lead_id))
            self._conn.execute(
                "INSERT INTO lead_events(lead_id, kind, from_status, to_status, text, source, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (lead_id, lead_domain.EVENT_KIND_STATUS, old_status, new_status, text, source, now_iso()),
            )
            self._conn.commit()

    def add_lead_note(self, lead_id: int, text: str,
                      source: str = lead_domain.EVENT_SOURCE_MANUAL) -> None:
        text = text.strip()
        if not text:
            return
        self.execute(
            "INSERT INTO lead_events(lead_id, kind, text, source, created_at) VALUES (?, ?, ?, ?, ?)",
            (lead_id, lead_domain.EVENT_KIND_NOTE, text, source, now_iso()),
        )

    def list_lead_events(self, lead_id: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM lead_events WHERE lead_id = ? ORDER BY created_at, id", (lead_id,))

    def lead_correspondence(self, telegram_id: int, limit: int = 200) -> list[sqlite3.Row]:
        """What this contact has actually said, across every tracked
        chat — the "переписка" on the card. A plain filter on sender_id,
        not an FTS query: there's no search phrase here, just "everything
        from this person," and messages already carries that column."""
        return self.query(
            "SELECT * FROM messages WHERE sender_id = ? AND is_hidden = 0 "
            "ORDER BY date DESC LIMIT ?",
            (telegram_id, limit),
        )

    def log_activity(self, contact_id: int | None, bot_id: int | None, chat_id: int | None,
                      message_id: int | None, chat_type: str | None, kind: str = "message") -> None:
        self.execute(
            """INSERT INTO bot_activity_log(contact_id, bot_id, chat_id, message_id, timestamp, chat_type, kind)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (contact_id, bot_id, chat_id, message_id, now_iso(), chat_type, kind),
        )

    def contacts_silent_since(self, bot_id: int, cutoff_iso: str) -> list[sqlite3.Row]:
        """Contacts this bot has actually interacted with whose last
        activity predates the cutoff — the candidate set for an
        inactivity trigger. Scoped by bot so one bot's reminders don't
        reach into another bot's audience."""
        return self.query(
            """
            SELECT c.* FROM bot_contacts c
            WHERE c.last_active < ?
              AND EXISTS (SELECT 1 FROM bot_activity_log a
                          WHERE a.contact_id = c.id AND a.bot_id = ?)
            ORDER BY c.last_active
            """,
            (cutoff_iso, bot_id),
        )

    def has_activity_since(self, contact_id: int, kind: str, since_iso: str) -> bool:
        row = self.query_one(
            "SELECT 1 FROM bot_activity_log WHERE contact_id = ? AND kind = ? "
            "AND timestamp >= ? LIMIT 1",
            (contact_id, kind, since_iso),
        )
        return row is not None

    def has_trigger_activity_since(self, bot_id: int, kind: str, since_iso: str) -> bool:
        row = self.query_one(
            "SELECT 1 FROM bot_activity_log WHERE bot_id = ? AND kind = ? "
            "AND timestamp >= ? LIMIT 1",
            (bot_id, kind, since_iso),
        )
        return row is not None

    def activity_for_contact(self, contact_id: int, limit: int = 200) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM bot_activity_log WHERE contact_id = ? ORDER BY timestamp DESC LIMIT ?",
            (contact_id, limit),
        )

    def contact_ranking(self, limit: int = 50, half_life_days: float = 14.0) -> list[dict]:
        """Recency+frequency activity score per contact: each activity_log
        row contributes exp(-age_days / half_life_days) — a message from
        today counts close to 1, one from half_life_days ago counts 0.5,
        older ones fade further, so someone active a lot three months ago
        doesn't outrank someone active daily this week.

        Bounded to the last 10 half-lives — beyond that a row's own
        contribution is under 0.005, negligible to the score, so there's
        no reason to keep pulling the *entire* activity_log into Python on
        every refresh as it grows over months of use."""
        import math
        now = dt.datetime.now().astimezone()
        cutoff = (now - dt.timedelta(days=half_life_days * 10)).isoformat()
        rows = self.query(
            """SELECT contact_id, timestamp FROM bot_activity_log
               WHERE contact_id IS NOT NULL AND timestamp >= ? ORDER BY contact_id""",
            (cutoff,),
        )
        scores: dict[int, float] = {}
        counts: dict[int, int] = {}
        for r in rows:
            try:
                ts = dt.datetime.fromisoformat(r["timestamp"])
            except ValueError:
                continue
            age_days = max(0.0, (now - ts).total_seconds() / 86400)
            scores[r["contact_id"]] = scores.get(r["contact_id"], 0.0) + math.exp(-age_days / half_life_days)
            counts[r["contact_id"]] = counts.get(r["contact_id"], 0) + 1
        out = []
        for contact_id, score in scores.items():
            contact = self.get_contact(contact_id)
            if not contact:
                continue
            out.append({
                "contact_id": contact_id, "telegram_id": contact["telegram_id"],
                "username": contact["username"], "score": round(score, 3),
                "activity_count": counts[contact_id], "last_active": contact["last_active"],
            })
        out.sort(key=lambda r: r["score"], reverse=True)
        return out[:limit]

    def leads_funnel(self, bot_id: int | None = None) -> dict[str, int]:
        """Three coarse buckets for analytics_tab.py's summary row — the
        funnel's own five-stage detail (С8) doesn't fit that layout, so
        qualified/quote_sent/negotiation collapse into "в работе" and
        won/lost collapse into "закрыты", same grouping analytics_tab and
        today.py already use elsewhere."""
        sql = "SELECT status, count(*) AS c FROM bot_leads"
        params: list[Any] = []
        if bot_id is not None:
            sql += " WHERE bot_id = ?"
            params.append(bot_id)
        sql += " GROUP BY status"
        counts = {r["status"]: r["c"] for r in self.query(sql, params)}
        in_progress = sum(counts.get(s, 0) for s in
                           (lead_domain.QUALIFIED, lead_domain.QUOTE_SENT, lead_domain.NEGOTIATION))
        closed = sum(counts.get(s, 0) for s in (lead_domain.WON, lead_domain.LOST))
        return {"new": counts.get(lead_domain.NEW, 0), "in_progress": in_progress, "closed": closed}

    # ---- funnel/source reports (С8) ---------------------------------------
    # All four scoped by bot_leads.created_at, not by when an event inside
    # the range happened — a lead created just before date_from that later
    # got a status change inside the range would otherwise show up in the
    # "срок до КП"/"причины отказов" numbers but not in "конверсия по
    # источникам", two different populations under one report. Scoping all
    # four the same way keeps them describing the same set of leads.
    def leads_report_by_source(self, date_from: str | None = None,
                                date_to: str | None = None) -> list[sqlite3.Row]:
        """One row per source chat, plus a NULL bucket (source_chat_id
        IS NULL — bot-triggered or manually created leads never had one)
        — see core/lead_report.conversion() for what a caller does with
        total/won/lost."""
        sql = (
            "SELECT bl.source_chat_id AS chat_id, c.title AS chat_title, "
            "count(*) AS total, "
            "sum(CASE WHEN bl.status = ? THEN 1 ELSE 0 END) AS won, "
            "sum(CASE WHEN bl.status = ? THEN 1 ELSE 0 END) AS lost "
            "FROM bot_leads bl LEFT JOIN chats c ON c.chat_id = bl.source_chat_id"
        )
        params: list[Any] = [lead_domain.WON, lead_domain.LOST]
        clauses = []
        if date_from is not None:
            clauses.append("bl.created_at >= ?")
            params.append(date_from)
        if date_to is not None:
            clauses.append("bl.created_at <= ?")
            params.append(date_to)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " GROUP BY bl.source_chat_id ORDER BY total DESC"
        return self.query(sql, params)

    def leads_report_by_direction(self, date_from: str | None = None,
                                   date_to: str | None = None) -> list[sqlite3.Row]:
        """Same shape as leads_report_by_source, grouped by direction_id
        instead — including its own NULL bucket for leads with no
        direction set."""
        sql = (
            "SELECT bl.direction_id AS direction_id, d.name AS direction_name, "
            "count(*) AS total, "
            "sum(CASE WHEN bl.status = ? THEN 1 ELSE 0 END) AS won, "
            "sum(CASE WHEN bl.status = ? THEN 1 ELSE 0 END) AS lost "
            "FROM bot_leads bl LEFT JOIN direction d ON d.id = bl.direction_id"
        )
        params: list[Any] = [lead_domain.WON, lead_domain.LOST]
        clauses = []
        if date_from is not None:
            clauses.append("bl.created_at >= ?")
            params.append(date_from)
        if date_to is not None:
            clauses.append("bl.created_at <= ?")
            params.append(date_to)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " GROUP BY bl.direction_id ORDER BY total DESC"
        return self.query(sql, params)

    def avg_days_to_quote(self, date_from: str | None = None,
                           date_to: str | None = None) -> float | None:
        """Mean days from a lead's created_at to the first time it reached
        quote_sent — the earliest matching lead_events row, in case a
        lead bounced back to an earlier stage and through quote_sent
        again later. None (not 0) when nothing in range ever reached that
        stage: there's no average of an empty set."""
        sql = (
            "SELECT bl.created_at AS created_at, MIN(le.created_at) AS quoted_at "
            "FROM bot_leads bl JOIN lead_events le ON le.lead_id = bl.id "
            "WHERE le.kind = ? AND le.to_status = ?"
        )
        params: list[Any] = [lead_domain.EVENT_KIND_STATUS, lead_domain.QUOTE_SENT]
        if date_from is not None:
            sql += " AND bl.created_at >= ?"
            params.append(date_from)
        if date_to is not None:
            sql += " AND bl.created_at <= ?"
            params.append(date_to)
        sql += " GROUP BY bl.id"
        rows = self.query(sql, params)
        diffs = []
        for r in rows:
            try:
                created = dt.datetime.fromisoformat(r["created_at"])
                quoted = dt.datetime.fromisoformat(r["quoted_at"])
            except (ValueError, TypeError):
                continue
            diffs.append((quoted - created).total_seconds() / 86400)
        return sum(diffs) / len(diffs) if diffs else None

    def reject_reasons_report(self, date_from: str | None = None,
                               date_to: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT reject_reason, count(*) AS c FROM bot_leads WHERE status = ?"
        params: list[Any] = [lead_domain.LOST]
        if date_from is not None:
            sql += " AND created_at >= ?"
            params.append(date_from)
        if date_to is not None:
            sql += " AND created_at <= ?"
            params.append(date_to)
        sql += " GROUP BY reject_reason ORDER BY c DESC"
        return self.query(sql, params)

    # ---- outbox ------------------------------------------------------
    def log_outbox(self, bot_id: int, target: str, status: str, text: str,
                    is_first: bool = False) -> int:
        cur = self.execute(
            "INSERT INTO outbox_sends(bot_id, target, status, is_first, text, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (bot_id, target, status, 1 if is_first else 0, text, now_iso()),
        )
        return cur.lastrowid

    def last_outbox_send(self, bot_id: int, target: str) -> str | None:
        row = self.query_one(
            "SELECT created_at FROM outbox_sends WHERE bot_id = ? AND target = ? AND status = 'sent' "
            "ORDER BY created_at DESC LIMIT 1",
            (bot_id, target),
        )
        return row["created_at"] if row else None

    def outbox_count_since(self, bot_id: int, since_iso: str, *, first_only: bool = False) -> int:
        sql = "SELECT count(*) AS c FROM outbox_sends WHERE bot_id = ? AND status = 'sent' AND created_at >= ?"
        params: list[Any] = [bot_id, since_iso]
        if first_only:
            sql += " AND is_first = 1"
        row = self.query_one(sql, params)
        return row["c"] if row else 0

    def outbox_counts(self, bot_id: int) -> dict:
        """Sent so far in the current hour/day windows — just the observed
        counts, not the limits themselves: db/ doesn't import bots/settings
        (the reverse of the app's normal dependency direction), so pairing
        these with a limit is SendLimitsDialog's job, which already loads
        bot_settings for its own inputs anyway."""
        now_dt = dt.datetime.now().astimezone()
        hour = self.outbox_count_since(bot_id, (now_dt - dt.timedelta(hours=1)).isoformat())
        day = self.outbox_count_since(bot_id, (now_dt - dt.timedelta(days=1)).isoformat())
        first_today = self.outbox_count_since(
            bot_id, (now_dt - dt.timedelta(days=1)).isoformat(), first_only=True)
        return {"hour": hour, "day": day, "first_today": first_today}

    def is_blacklisted(self, bot_id: int, target: str) -> bool:
        return self.query_one(
            "SELECT 1 FROM outbox_blacklist WHERE bot_id = ? AND target = ?", (bot_id, target)
        ) is not None

    def add_to_blacklist(self, bot_id: int, target: str, reason: str | None = None) -> None:
        self.execute(
            "INSERT OR IGNORE INTO outbox_blacklist(bot_id, target, reason, created_at) VALUES (?, ?, ?, ?)",
            (bot_id, target, reason, now_iso()),
        )

    def remove_from_blacklist(self, bot_id: int, target: str) -> None:
        self.execute("DELETE FROM outbox_blacklist WHERE bot_id = ? AND target = ?", (bot_id, target))

    def list_blacklist(self, bot_id: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM outbox_blacklist WHERE bot_id = ? ORDER BY created_at DESC", (bot_id,))

    def add_draft(self, bot_id: int, target: str, text: str, reason: str | None = None) -> int:
        cur = self.execute(
            "INSERT INTO outbox_drafts(bot_id, target, text, reason, created_at) VALUES (?, ?, ?, ?, ?)",
            (bot_id, target, text, reason, now_iso()),
        )
        return cur.lastrowid

    def get_draft(self, draft_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM outbox_drafts WHERE id = ?", (draft_id,))

    def list_drafts(self, bot_id: int | None = None, pending_only: bool = True) -> list[sqlite3.Row]:
        sql = "SELECT * FROM outbox_drafts"
        clauses, params = [], []
        if bot_id is not None:
            clauses.append("bot_id = ?")
            params.append(bot_id)
        if pending_only:
            clauses.append("sent_at IS NULL AND dismissed_at IS NULL")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at"
        return self.query(sql, params)

    def mark_draft_sent(self, draft_id: int) -> None:
        self.execute("UPDATE outbox_drafts SET sent_at = ? WHERE id = ?", (now_iso(), draft_id))

    def dismiss_draft(self, draft_id: int) -> None:
        self.execute("UPDATE outbox_drafts SET dismissed_at = ? WHERE id = ?", (now_iso(), draft_id))

    # ---- Bitrix24 / CRM sync (С6) -----------------------------------------
    def enqueue_crm_sync(self, lead_id: int) -> None:
        """Queues a lead for the next drain tick — due immediately. Safe
        to call on a lead that's already queued: resets it to due-now
        rather than adding a second row (UNIQUE(lead_id))."""
        self.execute(
            "INSERT INTO crm_queue(lead_id, next_attempt_at, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(lead_id) DO UPDATE SET next_attempt_at = excluded.next_attempt_at",
            (lead_id, now_iso(), now_iso()),
        )

    def due_crm_queue(self, now_iso_str: str) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM crm_queue WHERE next_attempt_at <= ? ORDER BY next_attempt_at", (now_iso_str,))

    def get_crm_queue_entry(self, lead_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM crm_queue WHERE lead_id = ?", (lead_id,))

    def dequeue_crm_sync(self, queue_id: int) -> None:
        self.execute("DELETE FROM crm_queue WHERE id = ?", (queue_id,))

    def retry_crm_queue(self, queue_id: int, error: str, backoff_seconds: float) -> None:
        row = self.query_one("SELECT attempts FROM crm_queue WHERE id = ?", (queue_id,))
        if row is None:
            return
        next_attempt = (dt.datetime.now().astimezone() + dt.timedelta(seconds=backoff_seconds)) \
            .isoformat(timespec="seconds")
        self.execute(
            "UPDATE crm_queue SET attempts = attempts + 1, next_attempt_at = ?, last_error = ? WHERE id = ?",
            (next_attempt, error, queue_id),
        )

    def log_crm_sync(self, lead_id: int, crm_id: str) -> None:
        self.execute(
            "INSERT INTO lead_events(lead_id, kind, text, source, created_at) VALUES (?, ?, ?, ?, ?)",
            (lead_id, lead_domain.EVENT_KIND_SYNC, f"Синхронизировано с Bitrix24 (ID {crm_id}).",
             lead_domain.EVENT_SOURCE_INTEGRATION, now_iso()),
        )

    def leads_due_for_auto_crm_sync(self, statuses: list[str] | None = None) -> list[sqlite3.Row]:
        """С7: candidates for BitrixSyncService's auto-enqueue phase — a
        lead that's never been synced, or whose status/fields changed
        since its last sync (crm_synced_at predates updated_at), and
        isn't already sitting in crm_queue (enqueue_crm_sync's own
        UNIQUE(lead_id) would just no-op there, but checking here avoids
        resetting a failing entry's backoff on every single tick).

        statuses, when given, restricts this to the "qualified" auto-send
        policy's stages — see integrations/bitrix.py's AUTO_SEND_*. None
        means the "all leads" policy: every status qualifies.
        """
        sql = ("SELECT * FROM bot_leads WHERE "
               "(crm_id IS NULL OR crm_synced_at IS NULL OR crm_synced_at < updated_at) "
               "AND id NOT IN (SELECT lead_id FROM crm_queue)")
        params: list[Any] = []
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            sql += f" AND status IN ({placeholders})"
            params.extend(statuses)
        return self.query(sql, params)

    def crm_sync_journal(self, limit: int = 100) -> list[dict]:
        """С7's "журнал синхронизации": what went and what didn't, merged
        from the two places that already record it — no dedicated table
        needed. lead_events(kind='sync') is every successful send
        (log_crm_sync); crm_queue is everything still pending or actively
        failing, with last_error saying why. Sorted newest first and
        capped, same as any other activity feed in this app."""
        sent = self.query(
            "SELECT lead_id, created_at AS at, text AS detail, 'ok' AS outcome "
            "FROM lead_events WHERE kind = ? ORDER BY created_at DESC LIMIT ?",
            (lead_domain.EVENT_KIND_SYNC, limit),
        )
        pending = self.query(
            "SELECT lead_id, created_at AS at, last_error AS detail, "
            "CASE WHEN attempts = 0 THEN 'pending' ELSE 'retrying' END AS outcome "
            "FROM crm_queue ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        rows = [dict(r) for r in sent] + [dict(r) for r in pending]
        rows.sort(key=lambda r: r["at"], reverse=True)
        return rows[:limit]

    # ---- bot templates ---------------------------------------------------
    def add_template(self, bot_id: int | None, name: str, text: str, variables: list[str]) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO bot_templates(bot_id, name, text, variables, created_at) VALUES (?, ?, ?, ?, ?)",
                (bot_id, name, text, json.dumps(variables, ensure_ascii=False), now_iso()),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_template(self, template_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM bot_templates WHERE id = ?", (template_id,))

    def list_templates(self, bot_id: int | None = None) -> list[sqlite3.Row]:
        if bot_id is not None:
            return self.query(
                "SELECT * FROM bot_templates WHERE bot_id = ? ORDER BY created_at", (bot_id,)
            )
        return self.query("SELECT * FROM bot_templates ORDER BY created_at")

    def update_template(self, template_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields = dict(fields)
        if "variables" in fields and isinstance(fields["variables"], list):
            fields["variables"] = json.dumps(fields["variables"], ensure_ascii=False)
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE bot_templates SET {cols} WHERE id = ?", (*fields.values(), template_id))

    def template_usage(self, template_id: int) -> dict[str, int]:
        """What would break if this template were deleted: actions that
        send it, and scenarios that use it as their closing message.
        Referenced from JSON config, so there is no foreign key to lean on
        — this is the only way to warn before the fact instead of showing
        a broken reference afterwards."""
        actions = self.query(
            "SELECT id FROM bot_actions WHERE json_extract(config, '$.template_id') = ?",
            (template_id,),
        )
        scenarios = self.query(
            "SELECT id FROM bot_scenarios WHERE done_template_id = ?", (template_id,)
        )
        return {"actions": len(actions), "scenarios": len(scenarios)}

    def scenario_usage(self, scenario_id: int) -> dict[str, int]:
        """Actions that launch this scenario, plus how many contacts are
        part-way through it right now — losing those mid-dialog is the
        part a user is least likely to expect."""
        actions = self.query(
            "SELECT id FROM bot_actions WHERE json_extract(config, '$.scenario_id') = ?",
            (scenario_id,),
        )
        active = self.query(
            "SELECT id FROM bot_scenario_sessions WHERE scenario_id = ? AND status = 'active'",
            (scenario_id,),
        )
        return {"actions": len(actions), "active_dialogs": len(active)}

    def delete_template(self, template_id: int) -> None:
        self.execute("DELETE FROM bot_templates WHERE id = ?", (template_id,))

    # ---- bot scenarios -----------------------------------------------------
    def add_scenario(self, bot_id: int, name: str, steps: list[dict]) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO bot_scenarios(bot_id, name, steps, created_at) VALUES (?, ?, ?, ?)",
                (bot_id, name, json.dumps(_with_step_ids(steps), ensure_ascii=False), now_iso()),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_scenario(self, scenario_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM bot_scenarios WHERE id = ?", (scenario_id,))

    def list_scenarios(self, bot_id: int) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM bot_scenarios WHERE bot_id = ? ORDER BY created_at", (bot_id,))

    def update_scenario(self, scenario_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields = dict(fields)
        if "steps" in fields and isinstance(fields["steps"], list):
            fields["steps"] = json.dumps(_with_step_ids(fields["steps"]), ensure_ascii=False)
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE bot_scenarios SET {cols} WHERE id = ?", (*fields.values(), scenario_id))

    def delete_scenario(self, scenario_id: int) -> None:
        self.execute("DELETE FROM bot_scenarios WHERE id = ?", (scenario_id,))

    # ---- bot scenario sessions (FSM state) --------------------------------
    def last_finished_session(self, bot_id: int, contact_telegram_id: int) -> sqlite3.Row | None:
        """The most recently completed run for this contact — used to find
        which scenario they just finished, since the active session row is
        already marked done by the time the confirmation is sent."""
        return self.query_one(
            "SELECT * FROM bot_scenario_sessions WHERE bot_id = ? AND contact_telegram_id = ? "
            "AND status = 'done' ORDER BY updated_at DESC, id DESC LIMIT 1",
            (bot_id, contact_telegram_id),
        )

    def scenario_funnel(self, scenario_id: int) -> list[dict]:
        """How far contacts got through a scenario: for each step, how many
        runs reached it and how many stopped there.

        Reads the accumulated session history — which only became possible
        once schema v3 stopped collapsing every contact to one row per
        status. `step_index` is where a run stopped, so a run that reached
        step N passed through every step before it."""
        scenario = self.get_scenario(scenario_id)
        if not scenario:
            return []
        steps = json.loads(scenario["steps"])
        if not steps:
            return []
        rows = self.query(
            "SELECT step_index, status FROM bot_scenario_sessions WHERE scenario_id = ?",
            (scenario_id,),
        )
        total = len(rows)
        funnel = []
        for i, step in enumerate(steps):
            reached = sum(1 for r in rows if r["step_index"] >= i or r["status"] == "done")
            dropped = sum(
                1 for r in rows
                if r["status"] in ("abandoned", "active") and r["step_index"] == i
            )
            funnel.append({
                "index": i,
                "question": step.get("question", ""),
                "field": step.get("field", ""),
                "reached": reached,
                "dropped": dropped,
                "share": (reached / total) if total else 0.0,
            })
        return funnel

    def get_active_scenario_session(self, bot_id: int, contact_telegram_id: int) -> sqlite3.Row | None:
        return self.query_one(
            "SELECT * FROM bot_scenario_sessions WHERE bot_id = ? AND contact_telegram_id = ? AND status = 'active'",
            (bot_id, contact_telegram_id),
        )

    def start_scenario_session(self, bot_id: int, scenario_id: int, contact_telegram_id: int,
                                step_id: str | None = None) -> int:
        with self._lock:
            now = now_iso()
            self._conn.execute(
                "UPDATE bot_scenario_sessions SET status = 'abandoned', updated_at = ? "
                "WHERE bot_id = ? AND contact_telegram_id = ? AND status = 'active'",
                (now, bot_id, contact_telegram_id),
            )
            cur = self._conn.execute(
                """INSERT INTO bot_scenario_sessions(bot_id, scenario_id, contact_telegram_id,
                       step_index, step_id, answers, status, started_at, updated_at)
                   VALUES (?, ?, ?, 0, ?, '{}', 'active', ?, ?)""",
                (bot_id, scenario_id, contact_telegram_id, step_id, now, now),
            )
            self._conn.commit()
            return cur.lastrowid

    def update_scenario_session(self, session_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields = dict(fields)
        if "answers" in fields and isinstance(fields["answers"], dict):
            fields["answers"] = json.dumps(fields["answers"], ensure_ascii=False)
        fields.setdefault("updated_at", now_iso())
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE bot_scenario_sessions SET {cols} WHERE id = ?", (*fields.values(), session_id))


def _with_step_ids(steps: list[dict]) -> list[dict]:
    """Give every scenario step a stable `id`, assigned once and preserved
    across edits.

    The engine walks steps by position and doesn't read this yet. It exists
    so that when branching lands, a jump can name its destination by id
    instead of by index — otherwise inserting a step in the middle of a
    live scenario would silently repoint every existing branch, and fixing
    that later would mean migrating real customer data. Cheap now,
    expensive to retrofit.
    """
    used = {s["id"] for s in steps if isinstance(s, dict) and s.get("id")}
    out = []
    for step in steps:
        step = dict(step)
        if not step.get("id"):
            n = len(used) + 1
            while f"s{n}" in used:
                n += 1
            step["id"] = f"s{n}"
            used.add(step["id"])
        out.append(step)
    return out


class _Transaction:
    def __init__(self, db: Database):
        self.db = db

    def __enter__(self):
        self.db._lock.acquire()
        return self.db._conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.db._conn.commit()
            else:
                self.db._conn.rollback()
        finally:
            self.db._lock.release()
        return False
