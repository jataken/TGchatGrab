"""Drains crm_queue — the one path a lead's Bitrix24 sync goes through,
whether it was enqueued by the "Отправить в Битрикс24" button on the lead
card or (С7) an automatic policy. Same tick-loop shape as
LeadReminderService, except tick() is a real network call rather than
DB-only work, so it's async.

Exponential backoff on failure, capped, and it never gives up — a stuck
row just keeps growing its `attempts` count and pushing its
`next_attempt_at` back, staying visible via crm_queue.last_error rather
than silently dropping the lead. Network-down and "Bitrix keeps
rejecting this" look identical from here: both just mean "still
pending," which is what "лиды не теряются" actually requires — deciding
which failures are worth surfacing differently is С7's "журнал
синхронизации", not this session's.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from ..core import lead as lead_domain
from ..db.database import Database, now_iso
from ..integrations import bitrix

_logger = logging.getLogger("chatgrab")

TICK_SECONDS = 30
BASE_BACKOFF_SECONDS = 60
MAX_BACKOFF_SECONDS = 30 * 60


def _backoff(attempts: int) -> float:
    return min(BASE_BACKOFF_SECONDS * (2 ** attempts), MAX_BACKOFF_SECONDS)


class BitrixSyncService:
    def __init__(self, db: Database, security, on_log=None, client_factory=None):
        self.db = db
        self.security = security
        self.on_log = on_log or (lambda text, tone="": None)
        # Overridable so a test can hand in a fake client instead of a
        # real aiohttp-backed BitrixClient — same seam outbox.py's tests
        # use an injectable raw_send for, just at the HTTP layer instead.
        self._client_factory = client_factory or bitrix.BitrixClient
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._loop())

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(TICK_SECONDS)
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.warning("Bitrix: сбой тика синхронизации", exc_info=True)

    def enqueue(self, lead_id: int) -> None:
        self.db.enqueue_crm_sync(lead_id)

    async def tick(self, now: dt.datetime | None = None) -> int:
        now = now or dt.datetime.now().astimezone()
        webhook_url = bitrix.get_webhook_url(self.db, self.security)
        if not webhook_url:
            return 0
        due = self.db.due_crm_queue(now.isoformat())
        if not due:
            return 0
        client = self._client_factory(webhook_url)
        for row in due:
            await self._process_one(client, row)
        return len(due)

    async def _process_one(self, client: bitrix.BitrixClient, row) -> None:
        lead_id = row["lead_id"]
        lead = self.db.get_lead(lead_id)
        if lead is None:
            # Deleted since being queued — nothing left to sync.
            self.db.dequeue_crm_sync(row["id"])
            return
        direction = self.db.get_direction(lead["direction_id"]) if lead["direction_id"] else None
        fields = bitrix.lead_fields(lead, direction)
        try:
            if lead["crm_id"]:
                await client.update_lead(lead["crm_id"], fields)
                crm_id = lead["crm_id"]
            else:
                dup_id = await client.find_duplicate_lead(lead["phone"], lead["email"])
                crm_id = str(dup_id) if dup_id is not None else str(await client.add_lead(fields))
            self.db.set_lead_field(lead_id, crm_id=crm_id, crm_synced_at=now_iso())
            self.db.log_crm_sync(lead_id, crm_id)
            self.db.dequeue_crm_sync(row["id"])
            self.on_log(f"заявка №{lead_id} синхронизирована с Bitrix24 (ID {crm_id})", "ok")
        except Exception as e:
            self.db.retry_crm_queue(row["id"], str(e), _backoff(row["attempts"]))
            self.on_log(f"не удалось отправить заявку №{lead_id} в Bitrix24: {e}", "warn")
