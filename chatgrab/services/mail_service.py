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
import json
import logging
import shutil
import threading
from pathlib import Path

from ..bots.templating import render as render_template
from ..core import mail_attachment_text, mail_compose, mail_labels, mail_thread
from ..db.database import Database, now_iso
from ..integrations.mail import imap_client
from ..integrations.mail.imap_client import ImapClient, ImapError
from ..integrations.mail.smtp_client import SmtpClient
from ..paths import Paths
from ..security import SecurityService

_logger = logging.getLogger("chatgrab")

TICK_SECONDS = 180


class MailService:
    def __init__(self, db: Database, paths: Paths, security: SecurityService,
                 on_log=None, client_factory=None, smtp_factory=None):
        self.db = db
        self.paths = paths
        self.security = security
        self.on_log = on_log or (lambda text, tone="": None)
        # Overridable so a test can hand in a fake IMAP client instead of a
        # real socket-backed ImapClient — same seam BitrixSyncService's
        # client_factory uses for aiohttp.
        self._client_factory = client_factory or ImapClient
        # П5: same seam, for the one place this app sends mail — see
        # send_draft() and integrations/mail/smtp_client.py.
        self._smtp_factory = smtp_factory or SmtpClient
        self._task: asyncio.Task | None = None
        # П4: IDLE workers, one per enabled mailbox, live only between
        # start() and stop() — see _ensure_idle_workers(). Kept inert
        # (self._idle_active stays False) unless start() was actually
        # called, so calling tick() directly — every existing test does,
        # none of them call start() — never touches IDLE or its executor
        # threads at all.
        self._idle_active = False
        self._idle_stop_events: dict[int, threading.Event] = {}
        self._idle_tasks: dict[int, asyncio.Task] = {}

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._loop())
        self._idle_active = True
        self._ensure_idle_workers()

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        self._idle_active = False
        for stop_event in self._idle_stop_events.values():
            stop_event.set()
        self._idle_stop_events.clear()
        self._idle_tasks.clear()

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
        new messages were stored, across all mailboxes.

        Also drains each mailbox's offline action queue and (once
        start() has run) reconciles IDLE workers — both self-healing on
        every tick rather than needing an explicit "a mailbox was
        added/enabled" notification from the UI."""
        total = 0
        loop = asyncio.get_event_loop()
        for mailbox in self.db.list_mailboxes(enabled_only=True):
            try:
                total += await loop.run_in_executor(None, self._sync_mailbox, mailbox["id"])
            except Exception as e:
                self.db.set_mailbox_field(mailbox["id"], last_error=str(e))
                self.on_log(f"{mailbox['address']}: {e}", "warn")
            try:
                await loop.run_in_executor(None, self.drain_queue, mailbox["id"])
            except Exception:
                _logger.info("Почта: не удалось разобрать очередь действий ящика %s", mailbox["id"])
        if self._idle_active:
            self._ensure_idle_workers()
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
        for info in client.list_folders_detailed():
            self.db.upsert_mail_folder(mailbox_id, info["name"], enabled=(info["name"].upper() == "INBOX"))
            if info["special_use"]:
                self.db.set_mail_folder_state(mailbox_id, info["name"], special_use=info["special_use"])
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

        triples = client.fetch_new_headers(name, folder["last_uid"])
        if not triples:
            return 0
        highest = folder["last_uid"]
        for uid, raw, flags in triples:
            fields = imap_client.parse_headers(raw)
            message_id = self.db.upsert_mail_message(mailbox_id, name, uid, **fields)
            self._assign_thread(mailbox_id, message_id, fields)
            self.db.sync_message_flags(mailbox_id, name, uid, flags)
            highest = max(highest, uid)
        self.db.set_mail_folder_state(mailbox_id, name, last_uid=highest, last_synced_at=now_iso())
        return len(triples)

    # ---- threads (П2) — core/mail_thread.py decides, this just does the
    # database lookups that decision needs ------------------------------
    def _assign_thread(self, mailbox_id: int, message_id: int, fields: dict) -> None:
        if self.db.get_mail_message(message_id)["thread_id"]:
            return  # уже привязано — повторный забор того же письма
        thread_id = self._find_reference_thread(mailbox_id, fields)
        if thread_id is None:
            thread_id = self._find_subject_thread(mailbox_id, fields)
        if thread_id is None:
            subject_norm = mail_thread.normalize_subject(fields.get("subject"))
            thread_id = self.db.create_mail_thread(mailbox_id, subject_norm)
        self.db.set_message_thread(message_id, thread_id)

    def _find_reference_thread(self, mailbox_id: int, fields: dict) -> int | None:
        ids = mail_thread.reference_ids(fields.get("refs"), fields.get("in_reply_to"))
        for msg_id in reversed(ids):  # ближайший родитель — последним в списке
            row = self.db.get_mail_message_by_message_id(mailbox_id, msg_id)
            if row is not None and row["thread_id"] is not None:
                return row["thread_id"]
        return None

    def _find_subject_thread(self, mailbox_id: int, fields: dict) -> int | None:
        subject_norm = mail_thread.normalize_subject(fields.get("subject"))
        if not subject_norm:
            return None
        # Excluded on both sides of the comparison below: the mailbox's
        # own address is on every message in it, so leaving it in would
        # make "shares a participant" trivially true for any two
        # same-subject messages regardless of who they're actually with.
        mailbox = self.db.get_mailbox(mailbox_id)
        own_address = (mailbox["address"] or "").strip().lower() if mailbox else None

        candidates = [
            {
                "id": t["id"],
                "participants": self.db.thread_participants(t["id"], exclude=own_address),
                "last_date": self.db.thread_last_date(t["id"]),
            }
            for t in self.db.list_mail_threads_by_subject(mailbox_id, subject_norm)
        ]
        to_addresses = [a for a in json.loads(fields.get("to_addresses") or "[]")
                        if (a or "").strip().lower() != own_address]
        sender_address = fields.get("sender_address")
        if sender_address and own_address and sender_address.strip().lower() == own_address:
            sender_address = None
        message = {
            "subject": fields.get("subject"),
            "sender_address": sender_address,
            "to_addresses": to_addresses,
            "date": fields.get("date"),
        }
        return mail_thread.find_subject_fallback_thread(message, candidates)

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

    # ---- текст вложения -> поиск (П3) -----------------------------------
    def extract_attachment_text(self, attachment_id: int) -> None:
        """docx/xlsx/plain-text attachments only — blocking, pure Python,
        safe off the GUI thread via run_in_executor exactly like
        fetch_body(). PDF text comes from QPdfDocument.getAllText()
        instead, called from ui/screens/mail/attachment_view.py while the
        viewer is already open: QPdfDocument is a Qt object and has to be
        built on the GUI thread, so it doesn't go through this method.

        Best-effort like every other network/parsing call here: a corrupt
        or booby-trapped attachment (see core/mail_attachment_text.py's
        zip-bomb guard) just stays unindexed, it doesn't fail whatever
        triggered this call."""
        att = self.db.get_mail_attachment(attachment_id)
        if att is None or att["extracted_text"] is not None or not att["path"]:
            return
        try:
            text = mail_attachment_text.extract_text_for_search(
                att["path"], att["filename"], att["content_type"])
        except (mail_attachment_text.AttachmentParseError,
                mail_attachment_text.AttachmentTooLargeError) as e:
            _logger.info("Почта: текст вложения %r не извлечён: %s", att["filename"], e)
            return
        if text:
            self.db.set_attachment_extracted_text(attachment_id, text)

    # ---- поиск на сервере (П2) ------------------------------------------
    def search_server(self, mailbox_id: int, folder: str, query: str) -> int:
        """Reaches mail this app hasn't synced yet — local search_mail()
        only covers what's already stored. A hit is fetched and stored
        the same way an ordinary sync would (headers + thread assignment),
        so it becomes part of the normal local index from then on rather
        than a one-off result that vanishes next time. Blocking, like
        every other network call here — route through run_in_executor
        from the UI. Returns how many *new* messages this pulled in."""
        mailbox = self.db.get_mailbox(mailbox_id)
        if mailbox is None:
            return 0
        client = self._client_factory(mailbox["imap_host"], mailbox["imap_port"])
        client.connect(mailbox["address"], self._password_for(mailbox))
        try:
            uids = client.search_uids(folder, query)
            missing = [u for u in uids
                       if self.db.get_mail_message_by_uid(mailbox_id, folder, u) is None]
            triples = client.fetch_headers_for_uids(folder, missing) if missing else []
        finally:
            client.close()
        for uid, raw, flags in triples:
            fields = imap_client.parse_headers(raw)
            message_id = self.db.upsert_mail_message(mailbox_id, folder, uid, **fields)
            self._assign_thread(mailbox_id, message_id, fields)
            self.db.sync_message_flags(mailbox_id, folder, uid, flags)
        return len(triples)

    # ---- «прочитано» — локально и на сервере (П2) -----------------------
    def push_read_flags(self, mailbox_id: int, items: list[tuple[str, int]]) -> None:
        """items: [(folder, uid), ...] from db.mark_thread_read() — grouped
        by folder since STORE is one command per folder, not per message.
        Best-effort: the local mark already happened and stays, whatever
        happens here — a failed push just means the server's copy stays
        unread until the next successful one, the same "network hiccup,
        not data loss" shape as every other sync failure in this module."""
        if not items:
            return
        by_folder: dict[str, list[int]] = {}
        for folder, uid in items:
            by_folder.setdefault(folder, []).append(uid)
        mailbox = self.db.get_mailbox(mailbox_id)
        if mailbox is None:
            return
        client = self._client_factory(mailbox["imap_host"], mailbox["imap_port"])
        client.connect(mailbox["address"], self._password_for(mailbox))
        try:
            for folder, uids in by_folder.items():
                client.store_seen(folder, uids)
        finally:
            client.close()

    # ---- папки: создание/переименование/удаление/подписка (П4) ----------
    # Always synchronous and connection-required, unlike the per-message
    # actions below — "действия (пометки, перемещения) складываются в
    # очередь" in PLAN.md's checklist names exactly those two, not folder
    # administration, and there's no meaningful "offline create a folder"
    # to defer in the first place.
    def create_folder(self, mailbox_id: int, name: str) -> None:
        self._with_client(mailbox_id, lambda c: c.create_folder(name))
        self.db.upsert_mail_folder(mailbox_id, name, enabled=False)

    def rename_folder(self, mailbox_id: int, old_name: str, new_name: str) -> None:
        self._with_client(mailbox_id, lambda c: c.rename_folder(old_name, new_name))
        self.db.rename_mail_folder_record(mailbox_id, old_name, new_name)

    def delete_folder(self, mailbox_id: int, name: str) -> None:
        self._with_client(mailbox_id, lambda c: c.delete_folder(name))
        self.db.delete_mail_folder_record(mailbox_id, name)

    def set_folder_subscribed(self, mailbox_id: int, name: str, subscribed: bool) -> None:
        if subscribed:
            self._with_client(mailbox_id, lambda c: c.subscribe_folder(name))
        else:
            self._with_client(mailbox_id, lambda c: c.unsubscribe_folder(name))
        self.db.set_mail_folder_state(mailbox_id, name, enabled=1 if subscribed else 0)

    def _with_client(self, mailbox_id: int, fn) -> None:
        mailbox = self.db.get_mailbox(mailbox_id)
        if mailbox is None:
            raise ImapError("ящик не найден")
        client = self._client_factory(mailbox["imap_host"], mailbox["imap_port"])
        client.connect(mailbox["address"], self._password_for(mailbox))
        try:
            fn(client)
        finally:
            client.close()

    # ---- действия с письмом: местно сразу, на сервер — через очередь
    # (П4) --------------------------------------------------------------
    # Every action here applies to the local database immediately (so
    # the UI never waits on the network) and enqueues a mail_action_queue
    # row before making one best-effort attempt to push it — offline or
    # not, the row survives either way, and drain_queue() (called from
    # every tick(), and again right after enqueuing here) is what
    # actually gets it to the server, retried for as long as it takes.
    def mark_read(self, message_id: int, read: bool = True) -> None:
        message = self.db.get_mail_message(message_id)
        if message is None:
            return
        self.db.set_message_flags(message_id, is_read=read)
        self._enqueue_flags(message)

    def set_flagged(self, message_id: int, flagged: bool) -> None:
        message = self.db.get_mail_message(message_id)
        if message is None:
            return
        self.db.set_message_flags(message_id, is_flagged=flagged)
        self._enqueue_flags(message)

    def _enqueue_flags(self, message) -> None:
        """One "flags" action always pushes the message's *current*
        local is_read/is_flagged state, not a delta — idempotent by
        construction, so replaying it (a retried push, a queue drained
        twice by accident) never needs special-casing."""
        self.db.enqueue_mail_action(message["mailbox_id"], message["id"], "flags", {})
        self.drain_queue(message["mailbox_id"])

    def move_message(self, message_id: int, dest_folder: str, dest_mailbox_id: int | None = None) -> None:
        """Same-mailbox move applies locally right away (folder changes
        immediately, so the UI reflects it before the network round trip
        even starts) and gets a durable "moved elsewhere" placeholder
        until drain_queue() confirms it server-side and re-syncs the
        destination folder for the message's real, server-assigned UID
        (a move gets a brand new UID in its new folder — the same reason
        а fresh row, not an edited one, is what ends up there; see
        _apply_move()). Cross-mailbox move has no useful "local-only"
        half — the destination row doesn't exist in this app's database
        at all until the server confirms the copy — so it's applied only
        by drain_queue(), same as if the mailbox were offline the whole
        time; the message simply doesn't move in the UI until that
        succeeds."""
        message = self.db.get_mail_message(message_id)
        if message is None:
            return
        source_mailbox_id = message["mailbox_id"]
        cross = dest_mailbox_id is not None and dest_mailbox_id != source_mailbox_id
        if not cross:
            trash = self.db.get_mail_folder_by_special_use(source_mailbox_id, "Trash")
            if trash is not None and dest_folder == trash["name"]:
                self.db.move_message_to_trash_local(message_id, dest_folder)
            else:
                self.db.move_message_local(message_id, dest_folder)
        self.db.enqueue_mail_action(source_mailbox_id, message_id, "move", {
            "source_folder": message["folder"],
            "dest_folder": dest_folder,
            "dest_mailbox_id": dest_mailbox_id if cross else None,
        })
        self.drain_queue(source_mailbox_id)

    def move_to_trash(self, message_id: int) -> bool:
        """True if the mailbox actually has a \\Trash-special-use folder
        to move into — false means "nothing to do", not an error; a
        caller (the UI) decides what to show for a mailbox where Trash
        hasn't been detected yet."""
        message = self.db.get_mail_message(message_id)
        if message is None:
            return False
        trash = self.db.get_mail_folder_by_special_use(message["mailbox_id"], "Trash")
        if trash is None:
            return False
        self.move_message(message_id, trash["name"])
        return True

    def restore_from_trash(self, message_id: int) -> bool:
        """The delete half of "перемещение и удаление обратимы, пока
        письмо в корзине" — reverses move_to_trash() by moving back to
        restore_folder, at any point while the message is still sitting
        in Trash, not just immediately after. False if there was nothing
        to restore (see db.restore_message_from_trash)."""
        message = self.db.get_mail_message(message_id)
        if message is None:
            return False
        target = self.db.restore_message_from_trash(message_id)
        if target is None:
            return False
        self.db.enqueue_mail_action(message["mailbox_id"], message_id, "move", {
            "source_folder": message["folder"], "dest_folder": target, "dest_mailbox_id": None,
        })
        self.drain_queue(message["mailbox_id"])
        return True

    def permanently_delete(self, message_id: int) -> None:
        """No undo past this call — see permanently_delete() on
        ImapClient. Folder+uid are captured into the queued action's
        payload *before* the local row disappears, since drain_queue()
        won't have a message row left to read them from afterward."""
        message = self.db.get_mail_message(message_id)
        if message is None:
            return
        mailbox_id = message["mailbox_id"]
        self.db.enqueue_mail_action(mailbox_id, message_id, "delete", {
            "folder": message["folder"], "uid": message["uid"],
        })
        self.db.delete_mail_message_local(message_id)
        self.drain_queue(mailbox_id)

    def archive_thread(self, thread_id: int) -> int:
        """«E» in triage (П6): moves every message of the thread that
        isn't already in the mailbox's Archive folder there, via the same
        move_message() every other move in the app goes through — so it
        gets the exact same "applies locally right away, confirmed and
        given its real server UID by drain_queue()" behaviour, not a
        parallel path. Returns how many messages actually moved (0 if
        there's no detected Archive folder yet, or the thread was already
        fully archived — both "nothing to do", not an error)."""
        messages = self.db.list_thread_messages(thread_id)
        if not messages:
            return 0
        mailbox_id = messages[0]["mailbox_id"]
        archive = self.db.get_mail_folder_by_special_use(mailbox_id, "Archive")
        if archive is None:
            return 0
        moved = 0
        for message in messages:
            if message["folder"] == archive["name"]:
                continue
            self.move_message(message["id"], archive["name"])
            moved += 1
        return moved

    # ---- labels (П6) -------------------------------------------------
    def create_label(self, mailbox_id: int, name: str, color: str,
                      hotkey: int | None = None) -> int | None:
        """None means the hotkey's taken — checked here rather than left
        to mail_label's own partial-unique index, so the caller (the
        label manager dialog) gets a clean "no" instead of an
        IntegrityError to unpack."""
        if hotkey is not None and self.db.get_mail_label_by_hotkey(mailbox_id, hotkey) is not None:
            return None
        return self.db.create_mail_label(mailbox_id, name, color, hotkey)

    def update_label(self, label_id: int, **fields) -> bool:
        if "hotkey" in fields and fields["hotkey"] is not None:
            label = self.db.get_mail_label(label_id)
            if label is None:
                return False
            existing = self.db.get_mail_label_by_hotkey(label["mailbox_id"], fields["hotkey"])
            if existing is not None and existing["id"] != label_id:
                return False
        self.db.update_mail_label(label_id, **fields)
        return True

    def delete_label(self, label_id: int) -> None:
        """Deleting a label has to reach every message it was ever
        pushed to as a keyword, not just the mail_thread_label rows — so
        the server-side cleanup is enqueued *before*
        db.delete_mail_label() runs, since that call is what erases the
        local record of which threads even had it (see that method's own
        docstring)."""
        label = self.db.get_mail_label(label_id)
        if label is None:
            return
        keyword = mail_labels.label_keyword(label_id)
        for thread_id in self.db.list_thread_ids_with_label(label_id):
            for message in self.db.list_thread_messages(thread_id):
                self.db.enqueue_mail_action(
                    message["mailbox_id"], message["id"], "label_remove", {"keyword": keyword})
        self.db.delete_mail_label(label_id)
        self.drain_queue(label["mailbox_id"])

    def set_thread_label(self, thread_id: int, label_id: int, on: bool, _drain: bool = True) -> int | None:
        """Ярлык на цепочке, не на письме (checklist) — mail_thread_label
        is keyed by thread_id, and every message currently in that
        thread gets the same IMAP keyword pushed, same best-effort/
        offline-queue path as flags and moves (П4): applies to
        mail_thread_label instantly (a click removes/adds the plaque
        with no round trip needed to show it), server push is
        fire-and-forget via drain_queue(). Returns the mailbox_id acted
        on (or None if the thread had no messages), so
        apply_label_to_threads() can share one drain_queue() call across
        a whole bulk selection instead of one per thread (_drain=False)."""
        if on:
            self.db.add_thread_label(thread_id, label_id)
        else:
            self.db.remove_thread_label(thread_id, label_id)
        keyword = mail_labels.label_keyword(label_id)
        kind = "label_add" if on else "label_remove"
        messages = self.db.list_thread_messages(thread_id)
        mailbox_id = messages[0]["mailbox_id"] if messages else None
        for message in messages:
            self.db.enqueue_mail_action(message["mailbox_id"], message["id"], kind, {"keyword": keyword})
        if _drain and mailbox_id is not None:
            self.drain_queue(mailbox_id)
        return mailbox_id

    def apply_label_to_threads(self, thread_ids: list[int], label_id: int, on: bool = True) -> None:
        """Массовое действие (checklist): one label onto every
        Shift-selected thread, one shared drain_queue() call at the end
        instead of one per thread."""
        mailbox_id = None
        for thread_id in thread_ids:
            mb = self.set_thread_label(thread_id, label_id, on, _drain=False)
            mailbox_id = mb if mb is not None else mailbox_id
        if mailbox_id is not None:
            self.drain_queue(mailbox_id)

    # ---- offline action queue: drain (П4) --------------------------------
    def drain_queue(self, mailbox_id: int) -> int:
        """Applies every still-pending action for one mailbox, in the
        order they were enqueued, over a single connection. Best-effort
        per mailbox, same shape as _sync_mailbox(): a connection failure
        here leaves every remaining action queued for the next tick (or
        the next explicit call right after some other action), it never
        raises out to the caller. Returns how many actions were applied.
        Safe to call as often as needed — "не применяется дважды" holds
        because a row is only ever read here while applied_at IS NULL,
        and is marked applied in the same call that pushed it."""
        pending = self.db.list_pending_mail_actions(mailbox_id)
        if not pending:
            return 0
        mailbox = self.db.get_mailbox(mailbox_id)
        if mailbox is None or not mailbox["enabled"]:
            return 0
        client = self._client_factory(mailbox["imap_host"], mailbox["imap_port"])
        try:
            client.connect(mailbox["address"], self._password_for(mailbox))
        except Exception as e:
            _logger.info("Почта: очередь ящика %s не разобрана — нет соединения: %s", mailbox_id, e)
            return 0
        applied = 0
        try:
            for action in pending:
                try:
                    self._apply_queued_action(client, mailbox_id, action)
                except Exception as e:
                    _logger.info(
                        "Почта: действие %s (%s) для ящика %s отложено: %s",
                        action["id"], action["kind"], mailbox_id, e)
                    continue
                self.db.mark_mail_action_applied(action["id"])
                applied += 1
        finally:
            client.close()
        return applied

    def _apply_queued_action(self, client, mailbox_id: int, action) -> None:
        payload = json.loads(action["payload"] or "{}")
        kind = action["kind"]
        if kind == "flags":
            self._apply_flags_action(client, action)
        elif kind == "move":
            self._apply_move_action(client, mailbox_id, action, payload)
        elif kind == "delete":
            client.permanently_delete(payload["folder"], payload["uid"])
        elif kind == "label_add":
            self._apply_label_action(client, action, payload, add=True)
        elif kind == "label_remove":
            self._apply_label_action(client, action, payload, add=False)
        else:
            raise ImapError(f"неизвестное действие в очереди: {kind!r}")

    def _apply_label_action(self, client, action, payload: dict, add: bool) -> None:
        message = self.db.get_mail_message(action["message_id"])
        if message is None:
            return  # сообщение с тех пор удалено локально — применять нечего
        client.store_flag(message["folder"], [message["uid"]], payload["keyword"], add=add)

    def _apply_flags_action(self, client, action) -> None:
        message = self.db.get_mail_message(action["message_id"])
        if message is None:
            return  # сообщение с тех пор удалено — применять нечего
        client.store_flag(message["folder"], [message["uid"]], "\\Seen", add=bool(message["is_read"]))
        client.store_flag(message["folder"], [message["uid"]], "\\Flagged", add=bool(message["is_flagged"]))

    def _apply_move_action(self, client, mailbox_id: int, action, payload: dict) -> None:
        message = self.db.get_mail_message(action["message_id"])
        if message is None:
            return  # уже перемещено и подчищено более ранней попыткой
        source_folder = payload["source_folder"]
        dest_folder = payload["dest_folder"]
        dest_mailbox_id = payload.get("dest_mailbox_id")
        if dest_mailbox_id:
            self._apply_cross_mailbox_move(client, mailbox_id, message, source_folder,
                                            dest_folder, dest_mailbox_id)
            return
        message_id_header = message["message_id"]
        client.move_message(source_folder, message["uid"], dest_folder)
        # A move gets a brand-new UID in its destination folder on any
        # real IMAP server — this row's uid is now stale for dest_folder,
        # so it's dropped rather than "fixed up", and a targeted resync
        # of just the destination folder re-discovers the message under
        # its real UID the normal way (headers, flags, thread assignment
        # all run exactly as they would for any newly-seen message).
        self.db.delete_mail_message_local(message["id"])
        dest_row = self.db.get_mail_folder(mailbox_id, dest_folder)
        if dest_row is None:
            self.db.upsert_mail_folder(mailbox_id, dest_folder)
            dest_row = self.db.get_mail_folder(mailbox_id, dest_folder)
        self._sync_one_folder(client, mailbox_id, dest_row)
        # The resync above just created a brand-new row for this message
        # with restore_folder NULL, same as any newly-upserted message —
        # if this move landed it in Trash, that would silently throw away
        # move_message_to_trash_local()'s optimistic "restore to
        # source_folder" the instant the server confirmed the move, which
        # is exactly backwards: confirmation is when it needs to become
        # durable, not when it should disappear. Re-attached here, by
        # Message-ID, onto whichever row now carries the real UID.
        trash = self.db.get_mail_folder_by_special_use(mailbox_id, "Trash")
        if trash is not None and dest_folder == trash["name"] and message_id_header:
            fresh = self.db.get_mail_message_by_message_id(mailbox_id, message_id_header)
            if fresh is not None:
                self.db.set_message_restore_folder(fresh["id"], source_folder)

    def _apply_cross_mailbox_move(self, client, mailbox_id: int, message, source_folder: str,
                                   dest_folder: str, dest_mailbox_id: int) -> None:
        dest_mailbox = self.db.get_mailbox(dest_mailbox_id)
        if dest_mailbox is None:
            raise ImapError("ящик назначения не найден")
        raw = client.fetch_full_message(source_folder, message["uid"])
        flags = [f for f, present in (
            ("\\Seen", message["is_read"]), ("\\Flagged", message["is_flagged"]),
            ("\\Answered", message["is_answered"]),
        ) if present]
        dest_client = self._client_factory(dest_mailbox["imap_host"], dest_mailbox["imap_port"])
        dest_client.connect(dest_mailbox["address"], self._password_for(dest_mailbox))
        try:
            dest_client.append_message(dest_folder, raw, flags=flags or None)
            # The appended copy's own UID isn't read back from APPEND's
            # response (APPENDUID needs UIDPLUS, not guaranteed) — a
            # targeted resync of just the destination folder, over this
            # same still-open connection, discovers it the ordinary way
            # instead: headers, flags, and thread assignment all run
            # exactly as they would for any newly-seen message, so the
            # message shows up in mailbox B immediately rather than
            # waiting for B's own next periodic tick.
            dest_folder_row = self.db.get_mail_folder(dest_mailbox_id, dest_folder)
            if dest_folder_row is None:
                self.db.upsert_mail_folder(dest_mailbox_id, dest_folder)
                dest_folder_row = self.db.get_mail_folder(dest_mailbox_id, dest_folder)
            self._sync_one_folder(dest_client, dest_mailbox_id, dest_folder_row)
        finally:
            dest_client.close()
        client.permanently_delete(source_folder, message["uid"])
        self.db.delete_mail_message_local(message["id"])

    # ---- IDLE (П4) ---------------------------------------------------
    def _ensure_idle_workers(self) -> None:
        """Starts an IDLE worker for every currently-enabled mailbox that
        doesn't already have one, and stops any worker whose mailbox was
        disabled or deleted since — called from tick(), so this needs no
        separate notification path when a mailbox is added/enabled after
        start() already ran."""
        enabled_ids = {m["id"] for m in self.db.list_mailboxes(enabled_only=True)}
        for mailbox_id in list(self._idle_stop_events):
            if mailbox_id not in enabled_ids:
                self._idle_stop_events.pop(mailbox_id).set()
                self._idle_tasks.pop(mailbox_id, None)
        loop = asyncio.get_event_loop()
        for mailbox_id in enabled_ids:
            task = self._idle_tasks.get(mailbox_id)
            if task is not None and not task.done():
                continue
            stop_event = threading.Event()
            self._idle_stop_events[mailbox_id] = stop_event
            self._idle_tasks[mailbox_id] = asyncio.ensure_future(
                loop.run_in_executor(None, self._idle_worker, mailbox_id, stop_event, loop))

    def _idle_worker(self, mailbox_id: int, stop_event: threading.Event, loop) -> None:
        """Runs on its own executor thread for as long as the mailbox
        stays enabled — reconnects with backoff on any drop. The
        ordinary TICK_SECONDS loop keeps running independently the
        entire time and remains correct with or without this thread:
        IDLE is a pure latency optimisation layered on top of it, never
        the only path a message can arrive through — "откат на опрос"
        (PLAN.md) is therefore not special-cased code here, it's simply
        what already happens whenever IDLE isn't running."""
        backoff = 5
        while not stop_event.is_set():
            mailbox = self.db.get_mailbox(mailbox_id)
            if mailbox is None or not mailbox["enabled"]:
                return
            folder = self._idle_folder(mailbox_id)
            if folder is None:
                return
            client = self._client_factory(mailbox["imap_host"], mailbox["imap_port"])
            try:
                client.connect(mailbox["address"], self._password_for(mailbox))
                if not client.supports_idle():
                    return
                client.idle(folder, lambda: self._on_idle_event(mailbox_id, loop), stop_event)
                backoff = 5  # a clean return (stop_event was set) resets it
            except Exception as e:
                _logger.info("Почта: IDLE ящика %s прервался, переподключаюсь через %sс: %s",
                              mailbox_id, backoff, e)
                stop_event.wait(backoff)
                backoff = min(backoff * 2, 300)
            finally:
                client.close()

    def _idle_folder(self, mailbox_id: int) -> str | None:
        inbox = next((f for f in self.db.list_mail_folders(mailbox_id)
                      if f["enabled"] and f["name"].upper() == "INBOX"), None)
        return inbox["name"] if inbox else None

    def _on_idle_event(self, mailbox_id: int, loop) -> None:
        """Called from the IDLE worker thread itself — hands off to the
        event loop rather than syncing right here: _sync_mailbox() does
        its own blocking IMAP I/O on a *different* connection, which has
        no business running on the IDLE thread while that thread's own
        connection is still mid-command."""
        asyncio.run_coroutine_threadsafe(self._resync_after_idle(mailbox_id), loop)

    async def _resync_after_idle(self, mailbox_id: int) -> None:
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._sync_mailbox, mailbox_id)
        except Exception:
            _logger.warning("Почта: ресинк по IDLE-событию ящика %s не удался", mailbox_id, exc_info=True)

    # ---- личности / подпись (П5) -----------------------------------------
    def render_signature(self, identity_id: int | None) -> str:
        if not identity_id:
            return ""
        identity = self.db.get_mail_identity(identity_id)
        if identity is None or not identity["signature"]:
            return ""
        values = {"имя": identity["display_name"] or "", "email": identity["from_address"] or "",
                  "дата": now_iso()[:10]}
        return render_template(identity["signature"], values)

    def _compose_body(self, tail_block: str, identity_id: int | None) -> str:
        """tail_block is the quoted/forwarded text, or "" for a new
        message. Placed *after* the signature, since that's where a
        person types their own new text: above the signature, above any
        quote — the same layout every mainstream mail client uses."""
        signature = self.render_signature(identity_id)
        parts = [""]  # место для собственного текста
        if signature:
            parts.append(f"--\n{signature}")
        if tail_block:
            parts.append(tail_block)
        return "\n\n".join(parts)

    # ---- черновики: написать / ответить / переслать (П5) ------------------
    def start_new_draft(self, mailbox_id: int) -> int:
        identity = self.db.get_default_mail_identity(mailbox_id)
        identity_id = identity["id"] if identity else None
        return self.db.create_mail_draft(
            mailbox_id, kind="new", identity_id=identity_id, body_text=self._compose_body("", identity_id))

    def start_reply_draft(self, message_id: int, reply_all: bool = False) -> int | None:
        message = self.db.get_mail_message(message_id)
        if message is None:
            return None
        to, cc, identity_id = self._reply_recipients(message, reply_all)
        quoted = mail_compose.quote_body(
            message["body_text"] or "",
            mail_compose.quote_header(message["sender_name"], message["sender_address"], message["date"]))
        return self.db.create_mail_draft(
            message["mailbox_id"], kind="reply_all" if reply_all else "reply", identity_id=identity_id,
            in_reply_to_message_id=message_id, to_addresses=to, cc_addresses=cc,
            subject=mail_compose.reply_subject(message["subject"]),
            body_text=self._compose_body(quoted, identity_id))

    def _reply_recipients(self, message, reply_all: bool) -> tuple[list[str], list[str], int | None]:
        mailbox = self.db.get_mailbox(message["mailbox_id"])
        identity = self.db.get_default_mail_identity(message["mailbox_id"])
        own_address = (mailbox["address"] or "").strip().lower() if mailbox else None
        to = [message["sender_address"]] if message["sender_address"] else []
        cc: list[str] = []
        if reply_all:
            already = {a.strip().lower() for a in to if a}
            for addr in json.loads(message["to_addresses"] or "[]"):
                key = (addr or "").strip().lower()
                if key and key != own_address and key not in already:
                    cc.append(addr)
                    already.add(key)
        return to, cc, (identity["id"] if identity else None)

    def start_forward_draft(self, message_id: int) -> int | None:
        message = self.db.get_mail_message(message_id)
        if message is None:
            return None
        identity = self.db.get_default_mail_identity(message["mailbox_id"])
        identity_id = identity["id"] if identity else None
        block = mail_compose.forward_block(
            message["sender_name"], message["sender_address"], message["date"],
            json.loads(message["to_addresses"] or "[]"), message["subject"], message["body_text"])
        draft_id = self.db.create_mail_draft(
            message["mailbox_id"], kind="forward", identity_id=identity_id,
            in_reply_to_message_id=message_id, subject=mail_compose.forward_subject(message["subject"]),
            body_text=self._compose_body(block, identity_id))
        for att in self.db.list_mail_attachments(message_id):
            if att["path"] and Path(att["path"]).exists():
                self._copy_into_draft(draft_id, att["filename"], att["path"])
        return draft_id

    def create_llm_draft(self, message_id: int, generated_text: str) -> int | None:
        """П-1: the LLM path (ui/screens/mail/compose.py calls
        integrations/llm.py directly, the same way lead_card_assistant.py
        already does for Telegram leads) lands here and *only* here —
        this method writes a draft, author='assistant', and does not
        send anything; sending still needs the same human click through
        the same confirmation screen as any other draft."""
        message = self.db.get_mail_message(message_id)
        if message is None:
            return None
        to, cc, identity_id = self._reply_recipients(message, reply_all=False)
        quoted = mail_compose.quote_body(
            message["body_text"] or "",
            mail_compose.quote_header(message["sender_name"], message["sender_address"], message["date"]))
        body = self._compose_body(f"{generated_text}\n\n{quoted}" if generated_text else quoted, identity_id)
        return self.db.create_mail_draft(
            message["mailbox_id"], kind="reply", identity_id=identity_id,
            in_reply_to_message_id=message_id, to_addresses=to, cc_addresses=cc,
            subject=mail_compose.reply_subject(message["subject"]), body_text=body, author="assistant")

    # ---- вложения черновика (П5) ------------------------------------------
    def add_draft_attachment(self, draft_id: int, source_path: str) -> None:
        source = Path(source_path)
        dest_dir = self.paths.mail_draft_dir(draft_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / source.name
        shutil.copy2(source, dest)
        self.db.add_mail_draft_attachment(draft_id, source.name, str(dest), dest.stat().st_size)

    def _copy_into_draft(self, draft_id: int, filename: str, source_path: str) -> None:
        dest_dir = self.paths.mail_draft_dir(draft_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / filename
        shutil.copy2(source_path, dest)
        self.db.add_mail_draft_attachment(draft_id, filename, str(dest), dest.stat().st_size)

    # ---- сохранение черновика на сервере (П5) ------------------------------
    def sync_draft_to_server(self, draft_id: int) -> None:
        """Best-effort, like every other network call here — a failure
        leaves the draft exactly as saved locally (autosave, separate
        from this), just not mirrored to the server's Drafts folder
        yet. Deletes any previous server copy before appending the new
        one — IMAP has no "replace an APPEND", so a re-saved draft is a
        fresh message server-side, same as every real mail client's own
        drafts sync. The new copy's own UID isn't returned reliably by
        APPEND (APPENDUID needs UIDPLUS) — read back afterward from
        mail_folder.last_uid straight after resyncing Drafts, the same
        "ask the server what's actually there" approach used for a
        confirmed move (П4), on the same single-writer assumption that
        holds for one person's own drafts folder."""
        draft = self.db.get_mail_draft(draft_id)
        if draft is None:
            return
        mailbox = self.db.get_mailbox(draft["mailbox_id"])
        if mailbox is None:
            return
        drafts_folder = self.db.get_mail_folder_by_special_use(mailbox["id"], "Drafts")
        if drafts_folder is None:
            return
        raw = self._build_draft_mime(draft, mailbox)
        client = self._client_factory(mailbox["imap_host"], mailbox["imap_port"])
        try:
            client.connect(mailbox["address"], self._password_for(mailbox))
        except Exception as e:
            _logger.info("Почта: черновик %s не сохранён на сервере — нет соединения: %s", draft_id, e)
            return
        try:
            if draft["server_uid"]:
                try:
                    client.permanently_delete(drafts_folder["name"], draft["server_uid"])
                except ImapError:
                    pass
            client.append_message(drafts_folder["name"], raw, flags=["\\Draft"])
            self._sync_one_folder(client, mailbox["id"], self.db.get_mail_folder(mailbox["id"], drafts_folder["name"]))
        finally:
            client.close()
        updated_folder = self.db.get_mail_folder(mailbox["id"], drafts_folder["name"])
        if updated_folder and updated_folder["last_uid"]:
            self.db.update_mail_draft(draft_id, server_uid=updated_folder["last_uid"])

    def _build_draft_mime(self, draft, mailbox) -> bytes:
        identity = self.db.get_mail_identity(draft["identity_id"]) if draft["identity_id"] else None
        from_address = identity["from_address"] if identity else mailbox["address"]
        from_name = identity["display_name"] if identity else (mailbox["display_name"] or "")
        to = json.loads(draft["to_addresses"] or "[]")
        cc = json.loads(draft["cc_addresses"] or "[]")
        attachments = [(a["filename"], a["path"]) for a in self.db.list_mail_draft_attachments(draft["id"])
                       if a["path"]]
        return mail_compose.build_mime_message(
            from_address, from_name, to, cc, draft["subject"] or "(без темы)", draft["body_text"],
            attachments=attachments)

    # ---- отправка (П5) — единственная функция во всём приложении, которая
    # отправляет письмо; вызывается только с экрана подтверждения отправки,
    # который нельзя проскочить (П-1) --------------------------------------
    def send_draft(self, draft_id: int) -> None:
        draft = self.db.get_mail_draft(draft_id)
        if draft is None:
            raise ValueError("черновик не найден")
        mailbox = self.db.get_mailbox(draft["mailbox_id"])
        if mailbox is None:
            raise ValueError("ящик не найден")
        to = json.loads(draft["to_addresses"] or "[]")
        cc = json.loads(draft["cc_addresses"] or "[]")
        if not to:
            raise ValueError("не указан получатель")
        if not mailbox["smtp_host"]:
            raise ValueError("для этого ящика не задан SMTP-сервер — укажите его в настройках ящика")

        identity = self.db.get_mail_identity(draft["identity_id"]) if draft["identity_id"] else None
        from_address = identity["from_address"] if identity else mailbox["address"]

        reply_message = (self.db.get_mail_message(draft["in_reply_to_message_id"])
                          if draft["in_reply_to_message_id"] else None)
        in_reply_to = reply_message["message_id"] if reply_message else None
        references = (mail_compose.build_references(reply_message["refs"], reply_message["message_id"])
                      if reply_message else None)
        attachments = [(a["filename"], a["path"]) for a in self.db.list_mail_draft_attachments(draft_id)
                       if a["path"]]
        raw = mail_compose.build_mime_message(
            from_address, identity["display_name"] if identity else (mailbox["display_name"] or ""),
            to, cc, draft["subject"] or "(без темы)", draft["body_text"],
            in_reply_to=in_reply_to, references=references, attachments=attachments)

        smtp = self._smtp_factory(mailbox["smtp_host"], mailbox["smtp_port"] or 465)
        smtp.send(mailbox["address"], self._password_for(mailbox), from_address, to + cc, raw)

        self._append_sent_copy(mailbox, raw)
        self._cleanup_server_draft(mailbox, draft)
        self.db.mark_mail_draft_sent(draft_id)

    def _append_sent_copy(self, mailbox, raw: bytes) -> None:
        """Best-effort — the message has already left the network by the
        time this runs, so a failure here means "Sent doesn't have a
        copy yet", never "undo the send that already happened"."""
        try:
            sent_folder = self.db.get_mail_folder_by_special_use(mailbox["id"], "Sent")
            if sent_folder is None:
                return
            client = self._client_factory(mailbox["imap_host"], mailbox["imap_port"])
            client.connect(mailbox["address"], self._password_for(mailbox))
            try:
                client.append_message(sent_folder["name"], raw, flags=["\\Seen"])
                self._sync_one_folder(client, mailbox["id"], self.db.get_mail_folder(mailbox["id"], sent_folder["name"]))
            finally:
                client.close()
        except Exception:
            _logger.warning("Почта: письмо отправлено, но копия в Sent не сохранена (ящик %s)",
                             mailbox["id"], exc_info=True)

    def _cleanup_server_draft(self, mailbox, draft) -> None:
        if not draft["server_uid"]:
            return
        try:
            drafts_folder = self.db.get_mail_folder_by_special_use(mailbox["id"], "Drafts")
            if drafts_folder is None:
                return
            client = self._client_factory(mailbox["imap_host"], mailbox["imap_port"])
            client.connect(mailbox["address"], self._password_for(mailbox))
            try:
                client.permanently_delete(drafts_folder["name"], draft["server_uid"])
            finally:
                client.close()
        except Exception:
            _logger.info("Почта: письмо отправлено, серверная копия черновика не убрана (ящик %s)", mailbox["id"])

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
