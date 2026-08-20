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
                 event_source: str = lead_domain.EVENT_SOURCE_RULE,
                 funnel_id: int | None = None, origin_channel: str | None = None) -> int:
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

        funnel_id defaults to default_funnel_id() (С10) — every caller
        that predates funnels lands its lead there unchanged.
        origin_channel defaults from source_type (every existing caller
        is Telegram-side); П9 is what will ever pass
        lead_domain.ORIGIN_CHANNEL_EMAIL explicitly. Both are first-touch
        attribution — see transfer_lead_funnel()'s docstring for why
        origin_channel never changes after this.
        """
        if funnel_id is None:
            funnel_id = self.default_funnel_id()
        if origin_channel is None:
            origin_channel = lead_domain.origin_channel_from_source_type(source_type)
        with self._lock:
            now = now_iso()
            cur = self._conn.execute(
                """INSERT INTO bot_leads(
                       contact_id, bot_id, status, manager, created_at, updated_at, content,
                       tg_user_id, username, display_name, phone, email,
                       source_chat_id, source_type, direction_id, product, volume, unit,
                       deadline, city, delivery, owner, funnel_id, origin_channel
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (contact_id, bot_id, status, manager, now, now, json.dumps(content, ensure_ascii=False),
                 tg_user_id, username, display_name, phone, email,
                 source_chat_id, source_type, direction_id, product, volume, unit,
                 deadline, city, delivery, lead_domain.DEFAULT_OWNER, funnel_id, origin_channel),
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

    def leads_status_counts(self, bot_id: int | None = None,
                             funnel_id: int | None = None) -> dict[str, int]:
        """Per-status totals for the funnel row on the leads screen —
        pre-seeded with 0 for every stage *of the given funnel* (default
        default_funnel_id() — every pre-С10 lead already lives there, so
        omitting funnel_id keeps this returning exactly what it always
        did) so the UI never has to guard against a missing key."""
        if funnel_id is None:
            funnel_id = self.default_funnel_id()
        sql = "SELECT status, count(*) AS c FROM bot_leads WHERE funnel_id = ?"
        params: list[Any] = [funnel_id]
        if bot_id is not None:
            sql += " AND bot_id = ?"
            params.append(bot_id)
        sql += " GROUP BY status"
        counts = {r["status"]: r["c"] for r in self.query(sql, params)}
        stages = self.list_funnel_stages(funnel_id) if funnel_id is not None else []
        return {s["code"]: counts.get(s["code"], 0) for s in stages}

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
        core.lead's single hard rule (a `requires_reason` stage needs a
        reason, С10) against the lead's *own* funnel's stages, and always
        logs the move, which is what makes set_lead_field unsuitable for
        this one field: a status written through it would change the
        funnel with no trace in the history the card shows.

        Raises ValueError on an invalid move rather than silently
        refusing, so a caller (the card, a future scenario action) finds
        out immediately instead of the change quietly not happening.
        """
        lead = self.get_lead(lead_id)
        if lead is None:
            raise ValueError(f"Заявка {lead_id} не найдена.")
        stages = self.list_funnel_stages(lead["funnel_id"]) if lead["funnel_id"] else []
        error = lead_domain.validate_transition(stages, new_status, reject_reason)
        if error:
            raise ValueError(error)
        stage = lead_domain.stage_for_code(stages, new_status)
        old_status = lead["status"]
        with self._lock:
            fields = {"status": new_status, "updated_at": now_iso()}
            if stage["requires_reason"]:
                fields["reject_reason"] = reject_reason.strip()
            cols = ", ".join(f"{k} = ?" for k in fields)
            self._conn.execute(f"UPDATE bot_leads SET {cols} WHERE id = ?", (*fields.values(), lead_id))
            self._conn.execute(
                "INSERT INTO lead_events(lead_id, kind, from_status, to_status, text, source, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (lead_id, lead_domain.EVENT_KIND_STATUS, old_status, new_status, text, source, now_iso()),
            )
            self._conn.commit()

    def transfer_lead_funnel(self, lead_id: int, new_funnel_id: int, new_status: str, *,
                              source: str = lead_domain.EVENT_SOURCE_MANUAL) -> None:
        """С10's "перенос заявки между воронками вручную" — changes both
        funnel_id and status together (a status code only means anything
        within its own funnel, so moving one without the other would
        leave the lead pointing at a stage that isn't even in its new
        funnel) and logs an EVENT_KIND_FUNNEL row naming both funnels and
        both stages, distinct from an ordinary EVENT_KIND_STATUS move.

        origin_channel is deliberately left untouched — PLAN.md's "не
        меняется" rule: attribution is by first touch, and a manual
        transfer between funnels must not silently rewrite which channel
        actually brought the lead in (see core/lead.py's
        origin_channel_from_source_type() and its docstring).

        Raises ValueError if either funnel/stage doesn't exist — same
        "fail loud, not silently" contract as set_lead_status().
        """
        lead = self.get_lead(lead_id)
        if lead is None:
            raise ValueError(f"Заявка {lead_id} не найдена.")
        new_funnel = self.get_funnel(new_funnel_id)
        if new_funnel is None:
            raise ValueError(f"Воронка {new_funnel_id} не найдена.")
        new_stages = self.list_funnel_stages(new_funnel_id)
        new_stage = lead_domain.stage_for_code(new_stages, new_status)
        if new_stage is None:
            raise ValueError(f"Этап {new_status!r} не найден в воронке {new_funnel['name']!r}.")
        old_funnel = self.get_funnel(lead["funnel_id"]) if lead["funnel_id"] else None
        old_funnel_name = old_funnel["name"] if old_funnel is not None else "—"
        old_stages = self.list_funnel_stages(lead["funnel_id"]) if lead["funnel_id"] else []
        old_label = lead_domain.label_for_stage(old_stages, lead["status"])
        with self._lock:
            self._conn.execute(
                "UPDATE bot_leads SET funnel_id = ?, status = ?, updated_at = ? WHERE id = ?",
                (new_funnel_id, new_status, now_iso(), lead_id),
            )
            self._conn.execute(
                "INSERT INTO lead_events(lead_id, kind, from_status, to_status, text, source, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (lead_id, lead_domain.EVENT_KIND_FUNNEL, lead["status"], new_status,
                 f"Перенесена из воронки «{old_funnel_name}» ({old_label}) "
                 f"в «{new_funnel['name']}» ({new_stage['label']}).",
                 source, now_iso()),
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

    def leads_funnel(self, bot_id: int | None = None, funnel_id: int | None = None) -> dict[str, int]:
        """Three coarse buckets for analytics_tab.py's summary row — С10
        derives them from each stage's `kind` and position instead of
        the old hardcoded status names (see core.lead.bucket_counts),
        which is what lets this keep working for any funnel's own stage
        set, not just the five-stage one this used to assume. funnel_id
        defaults to default_funnel_id(), same "omit it, get exactly the
        old global behaviour" contract as leads_status_counts()."""
        if funnel_id is None:
            funnel_id = self.default_funnel_id()
        sql = "SELECT status, count(*) AS c FROM bot_leads WHERE funnel_id = ?"
        params: list[Any] = [funnel_id]
        if bot_id is not None:
            sql += " AND bot_id = ?"
            params.append(bot_id)
        sql += " GROUP BY status"
        counts = {r["status"]: r["c"] for r in self.query(sql, params)}
        stages = self.list_funnel_stages(funnel_id) if funnel_id is not None else []
        return lead_domain.bucket_counts(stages, counts)
