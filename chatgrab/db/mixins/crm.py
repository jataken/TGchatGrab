"""С6/С7's Bitrix24 send queue and sync journal. Named crm.py rather than
bitrix.py to avoid sitting next to integrations/bitrix.py (the actual
Bitrix24 REST client) under a name that reads the same in a file list —
this mixin only ever touches crm_queue/lead_events, never the network."""
from __future__ import annotations

import datetime as dt
import sqlite3

from ..timeutil import now_iso
from ...core import lead as lead_domain


class CrmMixin:
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

    def leads_due_for_auto_crm_sync(self, qualified_only: bool = False) -> list[sqlite3.Row]:
        """С7: candidates for BitrixSyncService's auto-enqueue phase — a
        lead that's never been synced, or whose status/fields changed
        since its last sync (crm_synced_at predates updated_at), and
        isn't already sitting in crm_queue (enqueue_crm_sync's own
        UNIQUE(lead_id) would just no-op there, but checking here avoids
        resetting a failing entry's backoff on every single tick).

        qualified_only restricts this to the "qualified" auto-send
        policy's stages (see integrations/bitrix.py's AUTO_SEND_*) — С10
        made this a join against each lead's *own* funnel's stages
        instead of a hardcoded status list (the old
        QUALIFIED_AUTO_STATUSES): "qualified" now means "past this
        funnel's own first open stage, and not lost", which is the same
        rule as before for the one funnel that existed when it was
        written, and the only version of that rule that means anything
        once a second funnel (П9) has its own different stage codes.
        False means the "all leads" policy: every status qualifies.
        """
        sql = (
            "SELECT bl.* FROM bot_leads bl "
            "JOIN funnel_stage fs ON fs.funnel_id = bl.funnel_id AND fs.code = bl.status "
            "WHERE (bl.crm_id IS NULL OR bl.crm_synced_at IS NULL OR bl.crm_synced_at < bl.updated_at) "
            "AND bl.id NOT IN (SELECT lead_id FROM crm_queue)"
        )
        if qualified_only:
            sql += (
                " AND fs.kind != 'lost' AND NOT (fs.kind = 'open' AND fs.order_index = "
                "(SELECT MIN(order_index) FROM funnel_stage "
                " WHERE funnel_id = bl.funnel_id AND kind = 'open'))"
            )
        return self.query(sql)

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
