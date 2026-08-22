"""П5: everything about composing an outgoing message that doesn't need
sqlite, Qt, or a network connection — subject prefixing, reply/forward
quoting, References-chain building, the "text says attached but nothing
is" heuristic, and the actual MIME bytes. Same contract as core/
mail_thread.py and core/mail_attachment_text.py: pure functions, tested
without a fake server.

MailService (services/mail_service.py) is the only caller that turns
build_mime_message()'s bytes into an actual network send — nothing here
sends anything, on principle: this module has no import of smtplib or
of ImapClient, and PLAN.md's П-1 ("никакой автоматической отправки")
depends on that staying true.
"""
from __future__ import annotations

import datetime as dt
import re
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

ATTACHMENT_WARN_BYTES = 20 * 1024 * 1024
MANY_RECIPIENTS_THRESHOLD = 5

_RE_PREFIX_RE = re.compile(r"^\s*(?:re|ответ)\s*:", re.IGNORECASE)
_FWD_PREFIX_RE = re.compile(r"^\s*(?:fwd?|пересл)\s*:", re.IGNORECASE)


def reply_subject(subject: str | None) -> str:
    subject = (subject or "").strip()
    if not subject:
        return "Re:"
    if _RE_PREFIX_RE.match(subject):
        return subject
    return f"Re: {subject}"


def forward_subject(subject: str | None) -> str:
    subject = (subject or "").strip()
    if not subject:
        return "Fwd:"
    if _FWD_PREFIX_RE.match(subject):
        return subject
    return f"Fwd: {subject}"


_MESSAGE_ID_RE = re.compile(r"<[^<>]+>")


def build_references(original_refs: str | None, original_message_id: str | None) -> str | None:
    """The outgoing References header for a reply — every id the
    original already carried, plus the original's own id appended if
    it isn't already the last one (mirrors core/mail_thread.py's
    reference_ids(), just building the chain forward instead of reading
    it backward). None if there's nothing to carry — a reply to a
    message with no References and no Message-ID of its own has nothing
    to thread onto, which shouldn't normally happen for a synced
    message but isn't this function's job to assume away."""
    ids = _MESSAGE_ID_RE.findall(original_refs or "")
    last = (original_message_id or "").strip()
    if last and (not ids or ids[-1] != last):
        ids.append(last)
    return " ".join(ids) if ids else None


def _format_quote_date(date_iso: str | None) -> str:
    if not date_iso:
        return ""
    try:
        parsed = dt.datetime.fromisoformat(date_iso)
    except ValueError:
        return date_iso
    return parsed.strftime("%d.%m.%Y, %H:%M")


def quote_header(sender_name: str | None, sender_address: str | None, date_iso: str | None) -> str:
    who = sender_name or sender_address or "отправитель"
    when = _format_quote_date(date_iso)
    if when:
        return f"{when}, {who} писал(а):"
    return f"{who} писал(а):"


def quote_body(original_text: str | None, header: str) -> str:
    """header + the original text, every line prefixed "> " — the
    "принятый вид" (checklist's own words) every mainstream mail client
    uses for a reply's trailing quote."""
    lines = (original_text or "").splitlines() or [""]
    quoted = "\n".join(f"> {line}" if line else ">" for line in lines)
    return f"{header}\n{quoted}"


def forward_block(sender_name: str | None, sender_address: str | None, date_iso: str | None,
                   to_addresses: list[str] | None, subject: str | None, original_text: str | None) -> str:
    """Not quote-prefixed, per convention — a forward shows the original
    as its own clearly delimited block, not as something the forwarder
    is replying inside of."""
    who = sender_name or sender_address or "—"
    to_line = ", ".join(to_addresses or [])
    header = (
        "---------- Пересылаемое сообщение ----------\n"
        f"От: {who}\n"
        f"Дата: {_format_quote_date(date_iso)}\n"
        f"Тема: {subject or '(без темы)'}\n"
        f"Кому: {to_line}\n"
    )
    return f"{header}\n{original_text or ''}"


_ATTACHMENT_MENTION_RE = re.compile(
    r"\b(?:во\s+вложени|вложени[ея]|прилага[юе]|приложен|attach(?:ed|ment))",
    re.IGNORECASE,
)


def mentions_attachment(body_text: str) -> bool:
    """A cheap heuristic, not a parser — «вложение», «прилагаю»,
    «приложен(ный/о)», "attached"/"attachment" in the body, in any
    case. False positives (a sentence merely mentioning the word) are
    fine: this only ever powers a dismissible warning on the mandatory
    pre-send screen, never a hard block."""
    return bool(_ATTACHMENT_MENTION_RE.search(body_text or ""))


def build_mime_message(
    from_address: str,
    from_name: str | None,
    to_addresses: list[str],
    cc_addresses: list[str],
    subject: str,
    body_text: str,
    in_reply_to: str | None = None,
    references: str | None = None,
    attachments: list[tuple[str, str]] | None = None,
    domain_for_message_id: str | None = None,
) -> bytes:
    """to_addresses/cc_addresses are plain address strings, already
    resolved — this function doesn't validate or parse them, that's the
    compose screen's job before it ever gets here. attachments is
    [(filename, path_on_disk), ...]. Returns full RFC822 bytes, ready
    for SmtpClient.send() and, unchanged, for IMAP APPEND into Sent —
    one build, both destinations, so the copy in Sent is byte-identical
    to what actually left the network, not a re-serialized approximation."""
    msg = EmailMessage()
    msg["From"] = formataddr((from_name or "", from_address))
    msg["To"] = ", ".join(to_addresses)
    if cc_addresses:
        msg["Cc"] = ", ".join(cc_addresses)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=domain_for_message_id)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body_text or "")
    for filename, path in attachments or []:
        data = Path(path).read_bytes()
        msg.add_attachment(data, maintype="application", subtype="octet-stream", filename=filename)
    return msg.as_bytes()


def total_recipients(to_addresses: list[str], cc_addresses: list[str]) -> int:
    return len({a.strip().lower() for a in (to_addresses or []) if a.strip()}
                | {a.strip().lower() for a in (cc_addresses or []) if a.strip()})


def total_attachment_size(sizes_bytes: list[int]) -> int:
    return sum(s for s in sizes_bytes if s)
