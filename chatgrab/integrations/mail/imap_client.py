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


def parse_headers(raw: bytes) -> dict:
    """BODY.PEEK[HEADER] bytes -> the fields mixins/mail.py's
    upsert_mail_message() stores. has_attachments is deliberately not
    guessed from headers here — it's set precisely once the full body is
    fetched (see parse_full_message), not approximated from Content-Type."""
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


def _parse_status_response(line: bytes) -> int | None:
    m = _STATUS_RE.search(line or b"")
    return int(m.group(1)) if m else None


def _parse_list_response(data) -> list[str]:
    """"(\\HasNoChildren) "/" "INBOX"" -> "INBOX". Modified-UTF-7 mailbox
    names (non-ASCII folders on some servers) aren't decoded here — real
    folder management is П4's job; this session only needs INBOX, which
    is always plain ASCII."""
    names = []
    for line in data or ():
        if line is None:
            continue
        text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else str(line)
        m = _FOLDER_NAME_RE.search(text)
        if m:
            names.append(m.group(1))
    return names


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

    def folder_uidvalidity(self, folder: str) -> int | None:
        """STATUS, not SELECT — doesn't require the folder to be opened
        and never marks anything read."""
        typ, data = self._conn.status(folder, "(UIDVALIDITY)")
        if typ != "OK":
            raise ImapError(f"не удалось получить статус папки {folder!r}")
        return _parse_status_response(_first(data))

    def fetch_new_headers(self, folder: str, since_uid: int) -> list[tuple[int, bytes]]:
        """UID FETCH <since_uid + 1>:* — BODY.PEEK[HEADER], so nothing
        here ever sets \\Seen. Returns [] when there's nothing newer."""
        typ, _ = self._conn.select(folder, readonly=True)
        if typ != "OK":
            raise ImapError(f"не удалось открыть папку {folder!r}")
        typ, data = self._conn.uid(
            "fetch", f"{since_uid + 1}:*", "(UID BODY.PEEK[HEADER])")
        if typ != "OK":
            raise ImapError(f"не удалось получить письма из {folder!r}")
        return _parse_fetch_pairs(data)

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
