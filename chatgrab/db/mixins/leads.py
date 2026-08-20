"""Contacts, leads and their history, and bot_activity_log — the "who
talked to a bot, what came of it" side of the app. See core/lead.py for
the funnel/status vocabulary this leans on throughout."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Any

from ..timeutil import now_iso
from ...core import lead as lead_domain


class LeadsMixin:
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

    def has_lead_with_email(self, address: str) -> bool:
        """П7's "known sender" triage signal: an exact match (this exact
        person led before) or same domain (someone at their company did)
        against bot_leads.email — the one field that already exists for
        this regardless of source (Telegram lead with an email left in
        conversation, Bitrix import, manual entry); mail's own leads
        (П9) will land in the same table and column, nothing extra to
        wire up here later."""
        address = (address or "").strip().lower()
        if not address or "@" not in address:
            return False
        domain = address.split("@", 1)[1]
        row = self.query_one(
            "SELECT 1 FROM bot_leads WHERE email IS NOT NULL "
            "AND (LOWER(email) = ? OR LOWER(email) LIKE ?) LIMIT 1",
            (address, f"%@{domain}"),
        )
        return row is not None

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
