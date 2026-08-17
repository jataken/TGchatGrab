"""С6/С7's Bitrix24 send queue and sync journal. Named crm.py rather than
bitrix.py to avoid sitting next to integrations/bitrix.py (the actual
Bitrix24 REST client) under a name that reads the same in a file list —
this mixin only ever touches crm_queue/lead_events, never the network."""
from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any

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
