"""IMAP: one connection to one mailbox's server — connect, list folders,
fetch new headers incrementally, fetch one message's full body on demand.

Key design decision (PLAN.md, П1): sync is driven by **UIDVALIDITY + UID**,
not by message date. A UID is only stable within one (folder,
UIDVALIDITY) pair — the server hands out ever-increasing UIDs per folder,
and remembers the last one this app has seen, so a normal sync is just
"UID FETCH <last_seen+1>:*". If the server ever reports a *different*
UIDVALIDITY than what's stored (the mailbox moved, or the folder was
recreated), every previously stored UID is meaningless — the only correct
response is to wipe that folder's messages and start over, which
services/mail_service.py does via db.reset_mail_folder(). Syncing by "the
newest date we've seen" instead breaks on backdated delivery and on
messages moved between folders — both routine, not edge cases, in real
mailboxes.

Nothing here touches sqlite or Qt, and nothing here opens a real socket
directly: `connection_factory` is the seam a test replaces with a fake
object exposing the same handful of imaplib.IMAP4_SSL methods (login,
logout, list, status, select, uid) — the same shape test_accounts.py
already uses to stand in for a Telethon client, just for IMAP.
"""
from __future__ import annotations

import email
import email.policy
import email.utils
import imaplib
import json
import ntpath
import posixpath
import re
import socket
import time
from email.header import decode_header
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

# ---- known providers: "адрес + пароль", not a six-field form -------------
# domain -> (imap_host, imap_port, smtp_host, smtp_port). Only the
# providers named in PLAN.md's П1 — a custom/corporate server always goes
# through the manual fields the settings card falls back to.
KNOWN_PROVIDERS: dict[str, tuple[str, int, str, int]] = {
    "yandex.ru": ("imap.yandex.ru", 993, "smtp.yandex.ru", 465),
    "ya.ru": ("imap.yandex.ru", 993, "smtp.yandex.ru", 465),
    "yandex.com": ("imap.yandex.com", 993, "smtp.yandex.com", 465),
    "mail.ru": ("imap.mail.ru", 993, "smtp.mail.ru", 465),
    "bk.ru": ("imap.mail.ru", 993, "smtp.mail.ru", 465),
    "inbox.ru": ("imap.mail.ru", 993, "smtp.mail.ru", 465),
    "list.ru": ("imap.mail.ru", 993, "smtp.mail.ru", 465),
    "gmail.com": ("imap.gmail.com", 993, "smtp.gmail.com", 465),
    "googlemail.com": ("imap.gmail.com", 993, "smtp.gmail.com", 465),
    "rambler.ru": ("imap.rambler.ru", 993, "smtp.rambler.ru", 465),
}


def autodetect(address: str) -> tuple[str, int, str, int] | None:
    domain = address.strip().rsplit("@", 1)[-1].lower()
    return KNOWN_PROVIDERS.get(domain)


class ImapError(Exception):
    pass


# ---- header decoding -------------------------------------------------
def decode_mime_header(raw: str | None) -> str:
    """RFC 2047 encoded-word decoding (=?UTF-8?B?...?=) with a hard
    fallback: KOI8-R and CP1251 still show up in Russian business mail,
    and an unrecognized or mis-declared charset must not take the whole
    message down with it — errors="replace" and move on, same contract
    short_dt() and every other display-formatting helper in this app
    keeps: never raise on bad input, degrade visibly instead."""
    if not raw:
        return ""
    try:
        parts = decode_header(raw)
    except Exception:
        return raw
    out = []
    for text, charset in parts:
        if isinstance(text, bytes):
            for enc in (charset, "utf-8"):
                if not enc:
                    continue
                try:
                    out.append(text.decode(enc, errors="replace"))
                    break
                except LookupError:
                    continue
            else:
                out.append(text.decode("utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def _parse_date(date_header: str | None) -> str | None:
    if not date_header:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(date_header)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed is None:
        return None
    return parsed.isoformat()


_BULK_PRECEDENCE_RE = re.compile(r"^\s*(bulk|list|junk)\s*$", re.IGNORECASE)


def parse_headers(raw: bytes) -> dict:
    """BODY.PEEK[HEADER] bytes -> the fields mixins/mail.py's
    upsert_mail_message() stores. has_attachments is deliberately not
    guessed from headers here — it's set precisely once the full body is
    fetched (see parse_full_message), not approximated from Content-Type.

    has_list_unsubscribe/is_bulk_precedence (П7) feed
    core/mail_triage.py's "bulk_signal" — read once here, off the same
    header bytes every other field already comes from, so scoring a
    message costs no extra network round trip."""
    msg = email.message_from_bytes(raw, policy=email.policy.compat32)
    from_name, from_addr = email.utils.parseaddr(msg.get("From", ""))
    to_list = [addr for _, addr in email.utils.getaddresses([msg.get("To", "")]) if addr]
    return {
        "subject": decode_mime_header(msg.get("Subject", "")),
        "sender_name": decode_mime_header(from_name),
        "sender_address": from_addr or None,
        "to_addresses": json.dumps(to_list, ensure_ascii=False),
        "date": _parse_date(msg.get("Date")),
        "message_id": (msg.get("Message-ID") or "").strip() or None,
        "in_reply_to": (msg.get("In-Reply-To") or "").strip() or None,
        "refs": (msg.get("References") or "").strip() or None,
        "has_list_unsubscribe": msg.get("List-Unsubscribe") is not None,
        "is_bulk_precedence": bool(_BULK_PRECEDENCE_RE.match(msg.get("Precedence", ""))),
    }


# ---- full-body parsing (fetched on demand, П1 builds the code, the
# reading screen that triggers it is П2) -----------------------------
class _TextExtractor(HTMLParser):
    """Just enough to give an HTML-only message something searchable in
    body_text — real rendering (with П-3's «удалённые картинки не
    загружаются» rule) is the reading screen's job, not this parser's."""

    def __init__(self):
        super().__init__()
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def _html_to_text(html: str) -> str:
    extractor = _TextExtractor()
    try:
        extractor.feed(html)
    except Exception:
        return html
    return extractor.text()


def _safe_filename(name: str) -> str:
    """Strip any directory component a hostile or malformed filename
    might carry (e.g. an attachment named "../../../evil.exe") — both
    separators, since the sender's OS isn't known."""
    stripped = posixpath.basename(ntpath.basename((name or "").strip()))
    return stripped or "attachment"


def parse_full_message(raw: bytes, mail_dir: Path) -> dict:
    """RFC822 bytes -> {"body_text", "body_html_path", "attachments":
    [{"filename", "content_type", "size_bytes", "path"}]}. mail_dir is
    the already-resolved per-message directory (Paths.mail_message_dir) —
    created lazily, only if there's an HTML part or an attachment to
    actually write, so a plain-text-only message never touches disk."""
    msg = email.message_from_bytes(raw, policy=email.policy.compat32)
    body_text: str | None = None
    html_bytes: bytes | None = None
    attachments: list[dict] = []
    created = False

    def ensure_dir() -> Path:
        nonlocal created
        if not created:
            mail_dir.mkdir(parents=True, exist_ok=True)
            created = True
        return mail_dir

    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        filename = part.get_filename()
        if filename:
            filename = decode_mime_header(filename)

        if content_type == "text/plain" and not filename and body_text is None:
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            try:
                body_text = payload.decode(charset, errors="replace")
            except LookupError:
                body_text = payload.decode("utf-8", errors="replace")
            continue

        if content_type == "text/html" and not filename and html_bytes is None:
            html_bytes = part.get_payload(decode=True) or b""
            continue

        if filename:
            payload = part.get_payload(decode=True) or b""
            dest = ensure_dir() / _safe_filename(filename)
            dest.write_bytes(payload)
            attachments.append({
                "filename": filename, "content_type": content_type,
                "size_bytes": len(payload), "path": str(dest),
            })

    body_html_path = None
    if html_bytes is not None:
        dest = ensure_dir() / "body.html"
        dest.write_bytes(html_bytes)
        body_html_path = str(dest)
        if body_text is None:
            body_text = _html_to_text(html_bytes.decode("utf-8", errors="replace"))

    return {"body_text": body_text, "body_html_path": body_html_path, "attachments": attachments}


# ---- raw IMAP response parsing ----------------------------------------
_UID_RE = re.compile(rb"UID (\d+)")
_STATUS_RE = re.compile(rb"UIDVALIDITY (\d+)")
_FOLDER_NAME_RE = re.compile(r'"([^"]*)"\s*$')


def _first(data) -> bytes:
    return data[0] if data else b""


def _quote_search_term(term: str) -> bytes:
    escaped = term.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'.encode("utf-8")


def _parse_fetch_pairs(data) -> list[tuple[int, bytes]]:
    """imaplib hands FETCH results back as a list mixing (meta, payload)
    tuples with plain closing-paren bytes — only the tuples carry data,
    and only the ones whose meta line actually names a UID are ours."""
    out = []
    for item in data or ():
        if isinstance(item, tuple) and len(item) == 2:
            meta, payload = item
            m = _UID_RE.search(meta)
            if m:
                out.append((int(m.group(1)), payload))
    return out


def _parse_fetch_pairs_with_flags(data) -> list[tuple[int, bytes, dict]]:
    """Same shape as _parse_fetch_pairs(), plus parse_flags() read off
    the same meta line — used wherever the FETCH request asked for
    FLAGS alongside the header/body (П4: a sync has to pick up
    \\Flagged/\\Answered/\\Seen changes made by another mail client, not
    only push this app's own changes outward)."""
    out = []
    for item in data or ():
        if isinstance(item, tuple) and len(item) == 2:
            meta, payload = item
            m = _UID_RE.search(meta)
            if m:
                out.append((int(m.group(1)), payload, parse_flags(meta)))
    return out


def _parse_status_response(line: bytes) -> int | None:
    m = _STATUS_RE.search(line or b"")
    return int(m.group(1)) if m else None


_LIST_ATTRS_RE = re.compile(r"^\(([^)]*)\)")
# RFC 6154 — the subset of LIST attributes that name a folder's role,
# not its structural shape (\HasChildren, \Marked, …).
_SPECIAL_USE_ATTRS = {"\\Sent", "\\Drafts", "\\Trash", "\\Junk", "\\Archive", "\\All"}


def _parse_list_response_detailed(data) -> list[dict]:
    """Name plus whatever RFC 6154 SPECIAL-USE attribute the server
    volunteered on a *plain* LIST — deliberately not the LIST-EXTENDED
    "RETURN (SPECIAL-USE)" form, which needs a raw, hand-built command
    imaplib has no public method for and not every server implements
    anyway. The providers PLAN.md names (Yandex, Mail.ru, Gmail) already
    include \\Sent/\\Drafts/\\Trash/\\Junk on an ordinary LIST without
    being asked, which covers the common case; a server that only
    reports it under RETURN just leaves special_use NULL — detected by
    what the server actually said, never guessed from the folder's name,
    which is the invariant the checklist actually cares about."""
    out = []
    for line in data or ():
        if line is None:
            continue
        text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else str(line)
        name_m = _FOLDER_NAME_RE.search(text)
        if not name_m:
            continue
        attrs_m = _LIST_ATTRS_RE.match(text.strip())
        attrs = attrs_m.group(1).split() if attrs_m else []
        special_use = next((a[1:] for a in attrs if a in _SPECIAL_USE_ATTRS), None)
        out.append({
            "name": name_m.group(1),
            "special_use": special_use,
            "selectable": "\\Noselect" not in attrs,
        })
    return out


def _parse_list_response(data) -> list[str]:
    """"(\\HasNoChildren) "/" "INBOX"" -> "INBOX". Modified-UTF-7 mailbox
    names (non-ASCII folders on some servers) aren't decoded here."""
    return [f["name"] for f in _parse_list_response_detailed(data)]


_FLAGS_RE = re.compile(rb"FLAGS \(([^)]*)\)")


def parse_flags(meta: bytes) -> dict:
    """The same FETCH meta line _parse_fetch_pairs_with_flags() splits a
    UID out of, read again for its FLAGS() list — \\Answered and a
    message's own \\Seen/\\Flagged state can change from *another* mail
    client (Outlook, webmail), not just from actions taken here, so a
    normal sync has to reconcile them, not only push local state
    outward. $Forwarded isn't an RFC-standard flag (no client is
    required to set or honour it), but it's the de facto keyword every
    major client uses, and IMAP servers accept arbitrary keyword flags
    by design (RFC 3501 §2.3.2) — reading it costs nothing even on a
    server that never sets it."""
    m = _FLAGS_RE.search(meta or b"")
    flags = {f.decode("ascii", "replace") for f in (m.group(1).split() if m else [])}
    return {
        "is_read": "\\Seen" in flags,
        "is_flagged": "\\Flagged" in flags,
        "is_answered": "\\Answered" in flags,
        "is_forwarded": "$Forwarded" in flags,
    }


class ImapClient:
    def __init__(self, host: str, port: int = 993,
                 connection_factory: Callable[[], object] | None = None):
        self.host = host
        self.port = port
        self._connection_factory = connection_factory or (
            lambda: imaplib.IMAP4_SSL(host, port))
        self._conn = None

    def connect(self, username: str, password: str) -> None:
        self._conn = self._connection_factory()
        typ, data = self._conn.login(username, password)
        if typ != "OK":
            raise ImapError(f"не удалось войти: {_first(data).decode('utf-8', 'replace')}")

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    def list_folders(self) -> list[str]:
        typ, data = self._conn.list()
        if typ != "OK":
            raise ImapError("не удалось получить список папок")
        return _parse_list_response(data)

    def list_folders_detailed(self) -> list[dict]:
        """Name, SPECIAL-USE role, and whether the folder can even be
        selected (a \\Noselect node is a pure hierarchy separator, e.g.
        Gmail's "[Gmail]") — everything П4's folder tree and
        auto-detected Sent/Drafts/Trash/Junk needs, from one LIST."""
        typ, data = self._conn.list()
        if typ != "OK":
            raise ImapError("не удалось получить список папок")
        return _parse_list_response_detailed(data)

    def create_folder(self, name: str) -> None:
        typ, data = self._conn.create(name)
        if typ != "OK":
            raise ImapError(f"не удалось создать папку {name!r}: {_first(data).decode('utf-8', 'replace')}")

    def rename_folder(self, old_name: str, new_name: str) -> None:
        typ, data = self._conn.rename(old_name, new_name)
        if typ != "OK":
            raise ImapError(
                f"не удалось переименовать папку {old_name!r} в {new_name!r}: "
                f"{_first(data).decode('utf-8', 'replace')}")

    def delete_folder(self, name: str) -> None:
        typ, data = self._conn.delete(name)
        if typ != "OK":
            raise ImapError(f"не удалось удалить папку {name!r}: {_first(data).decode('utf-8', 'replace')}")

    def subscribe_folder(self, name: str) -> None:
        typ, data = self._conn.subscribe(name)
        if typ != "OK":
            raise ImapError(f"не удалось подписаться на папку {name!r}: {_first(data).decode('utf-8', 'replace')}")

    def unsubscribe_folder(self, name: str) -> None:
        typ, data = self._conn.unsubscribe(name)
        if typ != "OK":
            raise ImapError(f"не удалось отписаться от папки {name!r}: {_first(data).decode('utf-8', 'replace')}")

    def capabilities(self) -> set[str]:
        typ, data = self._conn.capability()
        if typ != "OK":
            return set()
        raw = b" ".join(d for d in data if d)
        return {c.decode("ascii", "replace").upper() for c in raw.split()}

    def supports_idle(self) -> bool:
        return "IDLE" in self.capabilities()

    def folder_uidvalidity(self, folder: str) -> int | None:
        """STATUS, not SELECT — doesn't require the folder to be opened
        and never marks anything read."""
        typ, data = self._conn.status(folder, "(UIDVALIDITY)")
        if typ != "OK":
            raise ImapError(f"не удалось получить статус папки {folder!r}")
        return _parse_status_response(_first(data))

    def fetch_new_headers(self, folder: str, since_uid: int) -> list[tuple[int, bytes, dict]]:
        """UID FETCH <since_uid + 1>:* — BODY.PEEK[HEADER], so nothing
        here ever sets \\Seen. FLAGS rides along on the same FETCH (П4):
        a message can already carry \\Flagged/\\Answered/\\Seen the first
        time this app ever sees it — arrived that way, or set by another
        client before this app connected — so the initial sync has to
        read them, not assume every new message starts blank. Returns
        [] when there's nothing newer.

        RFC 3501 §9 treats a range's two endpoints as interchangeable
        ("2:4" and "4:2" are the same range) — so once the mailbox is
        fully caught up (since_uid is already the highest UID that
        exists), "<since_uid+1>:*" is a range whose low end doesn't
        exist and whose high end (`*`) resolves to since_uid itself;
        several real servers respond to that by returning the message
        at since_uid rather than an empty result. Without the filter
        below, that already-seen message would look "new" again on
        every single tick — re-triggering a triage notification and
        resetting the П9 "мы не ответили" reminder indefinitely, for as
        long as no genuinely new mail arrives."""
        typ, _ = self._conn.select(folder, readonly=True)
        if typ != "OK":
            raise ImapError(f"не удалось открыть папку {folder!r}")
        typ, data = self._conn.uid(
            "fetch", f"{since_uid + 1}:*", "(UID FLAGS BODY.PEEK[HEADER])")
        if typ != "OK":
            raise ImapError(f"не удалось получить письма из {folder!r}")
        return [pair for pair in _parse_fetch_pairs_with_flags(data) if pair[0] > since_uid]

    def fetch_full_message(self, folder: str, uid: int) -> bytes:
        """The complete RFC822 body for one UID — «тело по требованию»."""
        typ, _ = self._conn.select(folder, readonly=True)
        if typ != "OK":
            raise ImapError(f"не удалось открыть папку {folder!r}")
        typ, data = self._conn.uid("fetch", str(uid), "(UID BODY.PEEK[])")
        if typ != "OK":
            raise ImapError(f"не удалось получить письмо {uid} из {folder!r}")
        pairs = _parse_fetch_pairs(data)
        if not pairs:
            raise ImapError(f"письмо {uid} не найдено в {folder!r}")
        return pairs[0][1]

    def fetch_headers_for_uids(self, folder: str, uids: list[int]) -> list[tuple[int, bytes, dict]]:
        """Headers (+ FLAGS, see fetch_new_headers) for a specific,
        already-known set of UIDs — used to pull in the headers of
        search_uids() hits that aren't in the database yet. Same
        BODY.PEEK[HEADER] as fetch_new_headers, so it never marks
        anything \\Seen either."""
        if not uids:
            return []
        typ, _ = self._conn.select(folder, readonly=True)
        if typ != "OK":
            raise ImapError(f"не удалось открыть папку {folder!r}")
        seq = ",".join(str(u) for u in uids)
        typ, data = self._conn.uid("fetch", seq, "(UID FLAGS BODY.PEEK[HEADER])")
        if typ != "OK":
            raise ImapError(f"не удалось получить письма из {folder!r}")
        return _parse_fetch_pairs_with_flags(data)

    def search_uids(self, folder: str, query: str) -> list[int]:
        """Server-side UID SEARCH over subject and body — reaches mail
        this app hasn't synced yet, unlike the local FTS5 index. The
        search term is sent as a UTF-8-encoded quoted string under
        CHARSET UTF-8, as *bytes* (not str): imaplib's own command
        encoder defaults to ASCII and would raise on a Cyrillic term
        passed as a plain str, since it has no idea a CHARSET override is
        in play three arguments earlier — encoding it ourselves sidesteps
        that entirely rather than fighting imaplib's literal-continuation
        machinery for a second occurrence of the same term."""
        typ, _ = self._conn.select(folder, readonly=True)
        if typ != "OK":
            raise ImapError(f"не удалось открыть папку {folder!r}")
        term = _quote_search_term(query)
        typ, data = self._conn.uid(
            "search", "CHARSET", "UTF-8", "OR", "SUBJECT", term, "BODY", term)
        if typ != "OK":
            raise ImapError(f"не удалось выполнить поиск в {folder!r}")
        raw = _first(data)
        return [int(x) for x in raw.split()] if raw else []

    def store_seen(self, folder: str, uids: list[int]) -> None:
        """\\Seen specifically — kept as its own method since every other
        fetch in this client uses BODY.PEEK specifically to avoid this
        as a side effect, so this remains the one deliberate exception,
        callable on its own without pulling in store_flag()'s more
        general (and slightly more expensive to read) signature."""
        self.store_flag(folder, uids, "\\Seen", add=True)

    def store_flag(self, folder: str, uids: list[int], flag: str, add: bool) -> None:
        """General \\Seen/\\Flagged/\\Answered/$Forwarded push (П4) —
        add=False sends -FLAGS instead of +FLAGS, so this also covers
        "unflag"/"mark unread". Needs the folder opened read-write
        (readonly=False), unlike most other methods here."""
        if not uids:
            return
        typ, _ = self._conn.select(folder, readonly=False)
        if typ != "OK":
            raise ImapError(f"не удалось открыть папку {folder!r} для записи")
        seq = ",".join(str(u) for u in uids)
        op = "+FLAGS" if add else "-FLAGS"
        typ, _ = self._conn.uid("store", seq, op, f"({flag})")
        if typ != "OK":
            raise ImapError(f"не удалось изменить флаг {flag!r} в {folder!r}")

    # ---- move/copy/delete (П4) -------------------------------------------
    def move_message(self, folder: str, uid: int, dest_folder: str) -> None:
        """UID MOVE (RFC 6851) if the server accepts it; COPY + \\Deleted
        + EXPUNGE otherwise — the same "same physical message" outcome
        either way, just not atomic on a server that lacks MOVE."""
        typ, _ = self._conn.select(folder, readonly=False)
        if typ != "OK":
            raise ImapError(f"не удалось открыть папку {folder!r}")
        typ, _ = self._conn.uid("move", str(uid), dest_folder)
        if typ == "OK":
            return
        typ, data = self._conn.uid("copy", str(uid), dest_folder)
        if typ != "OK":
            raise ImapError(
                f"не удалось переместить письмо {uid} из {folder!r} в {dest_folder!r}: "
                f"{_first(data).decode('utf-8', 'replace')}")
        self._delete_and_expunge(str(uid))

    def copy_message(self, folder: str, uid: int, dest_folder: str) -> None:
        typ, _ = self._conn.select(folder, readonly=True)
        if typ != "OK":
            raise ImapError(f"не удалось открыть папку {folder!r}")
        typ, data = self._conn.uid("copy", str(uid), dest_folder)
        if typ != "OK":
            raise ImapError(
                f"не удалось скопировать письмо {uid} из {folder!r} в {dest_folder!r}: "
                f"{_first(data).decode('utf-8', 'replace')}")

    def append_message(self, folder: str, raw: bytes, flags: list[str] | None = None) -> None:
        """Writes a full RFC822 message straight into a folder — the
        server-side half of a cross-mailbox move (fetch from the source
        connection, append through the destination mailbox's own
        connection; a single IMAP session has no command that moves a
        message between two different accounts)."""
        flag_str = "(" + " ".join(flags) + ")" if flags else None
        typ, data = self._conn.append(folder, flag_str, None, raw)
        if typ != "OK":
            raise ImapError(
                f"не удалось дописать письмо в {folder!r}: {_first(data).decode('utf-8', 'replace')}")

    def permanently_delete(self, folder: str, uid: int) -> None:
        """\\Deleted + expunge — the *only* place a message actually
        disappears rather than moving to Trash; MailService only calls
        this for a message already sitting in a \\Trash-special-use
        folder (see the П-4-adjacent invariant in mail_service.py's
        docstring: nothing here ever deletes on its own initiative)."""
        typ, _ = self._conn.select(folder, readonly=False)
        if typ != "OK":
            raise ImapError(f"не удалось открыть папку {folder!r}")
        self._delete_and_expunge(str(uid))

    def _delete_and_expunge(self, uid_seq: str) -> None:
        typ, _ = self._conn.uid("store", uid_seq, "+FLAGS", "(\\Deleted)")
        if typ != "OK":
            raise ImapError(f"не удалось пометить {uid_seq} на удаление")
        # UID EXPUNGE (RFC 4315/UIDPLUS) touches only this UID; a server
        # without UIDPLUS rejects it, and plain EXPUNGE — which removes
        # every \Deleted message in the folder, not just this one — is
        # the correct, always-available fallback.
        typ, _ = self._conn.uid("expunge", uid_seq)
        if typ == "OK":
            return
        typ, _ = self._conn.expunge()
        if typ != "OK":
            raise ImapError(f"не удалось зачистить папку после удаления {uid_seq}")

    # ---- IDLE (П4) ---------------------------------------------------
    # imaplib has no public IDLE support at all (RFC 2177 postdates it
    # and was never added) — this is the same private-surface workaround
    # every IDLE-capable Python IMAP tool in the wild uses: send the raw
    # command via _new_tag()/send(), then read untagged lines directly
    # off the socket with a timeout so shutdown and periodic re-issuing
    # both stay possible. A future CPython imaplib rewrite could change
    # these internals; this is accepted, documented risk, same category
    # as attachment_view.py's introspected QtPdf calls (П3).
    _IDLE_READ_TIMEOUT = 20  # seconds between "is anyone waiting to stop me" checks
    _IDLE_REFRESH_SECONDS = 25 * 60  # RFC 2177: re-issue before ~29 min

    def idle(self, folder: str, on_event: Callable[[], None], stop_event) -> None:
        """Blocks the calling thread until stop_event.is_set() or the
        connection breaks. Calls on_event() with no arguments whenever
        the server reports untagged activity (new/removed message,
        flag change by another client) — the caller decides what that
        activity means and does the real resync; this method only ever
        says "something happened, go look," never what."""
        typ, _ = self._conn.select(folder, readonly=True)
        if typ != "OK":
            raise ImapError(f"не удалось открыть папку {folder!r} для IDLE")
        self._conn.sock.settimeout(self._IDLE_READ_TIMEOUT)
        while not stop_event.is_set():
            tag = self._conn._new_tag()
            self._conn.send(tag + b" IDLE\r\n")
            line = self._conn.readline()
            if not line.startswith(b"+"):
                raise ImapError(f"сервер отклонил IDLE: {line!r}")
            started = time.monotonic()
            try:
                while (not stop_event.is_set()
                       and time.monotonic() - started < self._IDLE_REFRESH_SECONDS):
                    try:
                        line = self._conn.readline()
                    except (socket.timeout, TimeoutError):
                        continue
                    if not line:
                        raise ImapError("соединение закрыто сервером во время IDLE")
                    if line.startswith(b"*"):
                        on_event()
            finally:
                self._conn.send(b"DONE\r\n")
                self._drain_idle_done(tag)

    def _drain_idle_done(self, tag: bytes) -> None:
        """Reads up to the tagged completion for the IDLE command just
        ended, so a stray line doesn't linger in the buffer ahead of the
        next command. Best-effort: a server slow to answer DONE doesn't
        get to hang shutdown — a short timeout just gives up and moves
        on, since the next select()/uid() call will simply see whatever
        arrives first anyway."""
        self._conn.sock.settimeout(10)
        while True:
            try:
                line = self._conn.readline()
            except (socket.timeout, TimeoutError):
                return
            if not line or line.startswith(tag):
                return
