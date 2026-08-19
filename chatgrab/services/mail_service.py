"""П1: fetches new mail for every enabled mailbox on a timer, incrementally
by UID (see integrations/mail/imap_client.py's module docstring for why
UIDVALIDITY+UID, not date, is what drives a resync).

Same tick-loop shape as BitrixSyncService — start()/stop()/tick(), retried
every TICK_SECONDS, one failure logged per mailbox without touching the
others. The one real difference: imaplib has no asyncio-native client, so
each mailbox's blocking IMAP round-trip runs in the default executor
(loop.run_in_executor) rather than being awaited directly. That's also
what the П1 invariant "сетевой обмен вне блокировки базы" actually means
in code: the socket I/O happens on a worker thread holding no lock at all,
and Database._lock is only ever taken afterward, briefly, to write what
came back — the same discipline telegram/collector.py already uses for
its own network calls, just via an executor instead of native async.
"""
from __future__ import annotations

import asyncio
import logging

from ..db.database import Database, now_iso
from ..integrations.mail import imap_client
from ..integrations.mail.imap_client import ImapClient
from ..paths import Paths
from ..security import SecurityService

_logger = logging.getLogger("chatgrab")

TICK_SECONDS = 180


class MailService:
    def __init__(self, db: Database, paths: Paths, security: SecurityService,
                 on_log=None, client_factory=None):
        self.db = db
        self.paths = paths
        self.security = security
        self.on_log = on_log or (lambda text, tone="": None)
        # Overridable so a test can hand in a fake IMAP client instead of a
        # real socket-backed ImapClient — same seam BitrixSyncService's
        # client_factory uses for aiohttp.
        self._client_factory = client_factory or ImapClient
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
                _logger.warning("Почта: сбой тика синхронизации", exc_info=True)

    async def tick(self) -> int:
        """One pass over every enabled mailbox. A mailbox whose sync fails
        (bad password, unreachable server) is skipped, not fatal — its
        last_error is recorded and the rest still run. Returns how many
        new messages were stored, across all mailboxes."""
        total = 0
        loop = asyncio.get_event_loop()
        for mailbox in self.db.list_mailboxes(enabled_only=True):
            try:
                total += await loop.run_in_executor(None, self._sync_mailbox, mailbox["id"])
            except Exception as e:
                self.db.set_mailbox_field(mailbox["id"], last_error=str(e))
                self.on_log(f"{mailbox['address']}: {e}", "warn")
        return total

    # ---- one mailbox, off the event loop ------------------------------
    def _sync_mailbox(self, mailbox_id: int) -> int:
        mailbox = self.db.get_mailbox(mailbox_id)
        if mailbox is None or not mailbox["enabled"]:
            return 0
        client = self._client_factory(mailbox["imap_host"], mailbox["imap_port"])
        client.connect(mailbox["address"], self._password_for(mailbox))
        try:
            stored = self._sync_folders(client, mailbox_id)
        finally:
            client.close()
        self.db.set_mailbox_field(mailbox_id, last_sync_at=now_iso(), last_error=None)
        return stored

    def _password_for(self, mailbox) -> str:
        return self.security.decrypt_secret(mailbox["password_enc"]) if mailbox["password_enc"] else ""

    def _sync_folders(self, client, mailbox_id: int) -> int:
        for name in client.list_folders():
            self.db.upsert_mail_folder(mailbox_id, name, enabled=(name.upper() == "INBOX"))
        stored = 0
        for folder in self.db.list_mail_folders(mailbox_id):
            if folder["enabled"]:
                stored += self._sync_one_folder(client, mailbox_id, folder)
        return stored

    def _sync_one_folder(self, client, mailbox_id: int, folder) -> int:
        name = folder["name"]
        uidvalidity = client.folder_uidvalidity(name)
        if folder["uidvalidity"] is None:
            self.db.set_mail_folder_state(mailbox_id, name, uidvalidity=uidvalidity)
        elif uidvalidity != folder["uidvalidity"]:
            _logger.info("Почта: UIDVALIDITY папки %r ящика %s изменился, перечитываю", name, mailbox_id)
            self.db.reset_mail_folder(mailbox_id, name, uidvalidity)
            folder = self.db.get_mail_folder(mailbox_id, name)

        pairs = client.fetch_new_headers(name, folder["last_uid"])
        if not pairs:
            return 0
        highest = folder["last_uid"]
        for uid, raw in pairs:
            fields = imap_client.parse_headers(raw)
            self.db.upsert_mail_message(mailbox_id, name, uid, **fields)
            highest = max(highest, uid)
        self.db.set_mail_folder_state(mailbox_id, name, last_uid=highest, last_synced_at=now_iso())
        return len(pairs)

    # ---- «тело по требованию» -----------------------------------------
    def fetch_body(self, message_id: int) -> None:
        """Full body + attachments for one already-synced message,
        fetched only when something asks (the reading screen, in П2) —
        blocking, like _sync_mailbox; a caller on the event loop should
        route this through run_in_executor the same way tick() does."""
        message = self.db.get_mail_message(message_id)
        if message is None or message["body_fetched"]:
            return
        mailbox = self.db.get_mailbox(message["mailbox_id"])
        if mailbox is None:
            return
        client = self._client_factory(mailbox["imap_host"], mailbox["imap_port"])
        client.connect(mailbox["address"], self._password_for(mailbox))
        try:
            raw = client.fetch_full_message(message["folder"], message["uid"])
        finally:
            client.close()
        mail_dir = self.paths.mail_message_dir(mailbox["id"], message["uid"])
        parsed = imap_client.parse_full_message(raw, mail_dir)
        self.db.set_mail_message_body(
            message_id, parsed["body_text"], parsed["body_html_path"], bool(parsed["attachments"]))
        for att in parsed["attachments"]:
            self.db.add_mail_attachment(
                message_id, att["filename"], att["content_type"], att["size_bytes"], att["path"])

    # ---- "Проверить подключение" ---------------------------------------
    def test_connection(self, imap_host: str, imap_port: int, address: str, password: str) -> str:
        """Synchronous — the settings card calls this via run_in_executor,
        same as everything else network-bound here. Raises ImapError (or
        whatever the client raises) on failure; the card's own error
        message tells a bad password apart from an unreachable server."""
        client = self._client_factory(imap_host, imap_port)
        client.connect(address, password)
        try:
            folders = client.list_folders()
        finally:
            client.close()
        return f"Подключено, папок: {len(folders)}."
