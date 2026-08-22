"""Mail thread assembly: deciding which conversation a newly-synced
message belongs to. No sqlite, no Qt — same contract as core/lead.py.

Two independent signals, tried in order by the caller (db/mixins/mail.py
and services/mail_service.py do the actual lookups; this module only
decides):

1. References/In-Reply-To — exact. If a header names a message-id this
   app has already stored and threaded, the new message continues that
   thread, full stop. reference_ids() below only does the header parsing;
   resolving an id to a thread is a database lookup, not this module's
   job.
2. Normalized subject + participant overlap + a time window — a fallback
   for the (very common) case where a reply lost its References header,
   or the very first message of a thread. find_subject_fallback_thread()
   is the one real decision this module makes: which of several
   same-subject threads, if any, this message actually continues.
"""
from __future__ import annotations

import datetime as dt
import re

# Russian and English reply/forward prefixes, stripped in one pass so
# "Re: Re: Fwd: тема" and "тема" normalize to the same string — a mail
# client that doesn't collapse repeated prefixes itself is common, not an
# edge case. Bracketed tags ([EXT], [SPAM], a mailing-list [tag]) are
# stripped too: a security gateway prepending [EXT] to every external
# email would otherwise split one conversation into two threads.
_PREFIX_RE = re.compile(
    r"^\s*(?:(?:re|fwd?|ответ|пересл)\s*:\s*|\[[^\]]*\]\s*)+", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


def normalize_subject(subject: str | None) -> str:
    text = _PREFIX_RE.sub("", subject or "")
    return _WS_RE.sub(" ", text).strip().lower()


_MESSAGE_ID_RE = re.compile(r"<[^<>]+>")


def reference_ids(refs: str | None, in_reply_to: str | None) -> list[str]:
    """Every message-id this message points back to, oldest first (the
    order References already lists them in) with In-Reply-To appended if
    it isn't already the last entry — some clients set only one of the
    two headers, and when both are present In-Reply-To is normally just
    the last item of References restated, so this rarely adds a second
    entry in practice."""
    ids = _MESSAGE_ID_RE.findall(refs or "")
    last = (in_reply_to or "").strip()
    if last and last not in ids:
        ids.append(last)
    return ids


# How far apart two same-subject messages can be and still be considered
# one conversation. A year-old "Заявка" and a new one with the same title
# are two different conversations that happen to share a subject line;
# 30 days is generous for a real back-and-forth without merging unrelated
# ones.
SUBJECT_FALLBACK_WINDOW_DAYS = 30


def _participants(message: dict) -> set[str]:
    addrs = set()
    sender = (message.get("sender_address") or "").strip().lower()
    if sender:
        addrs.add(sender)
    for addr in message.get("to_addresses") or ():
        addr = (addr or "").strip().lower()
        if addr:
            addrs.add(addr)
    return addrs


def _parse_date(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def _within_window(a: str | None, b: str | None, window_days: int) -> bool:
    """No date on either side isn't evidence against a match — it just
    means the time check can't rule anything out, so the subject+
    participant match alone decides."""
    da, db_ = _parse_date(a), _parse_date(b)
    if da is None or db_ is None:
        return True
    # Naive vs. aware datetimes can't be subtracted — compare the parts
    # that are always safe (mail dates almost always carry a UTC offset,
    # but a header that doesn't shouldn't crash the match).
    if (da.tzinfo is None) != (db_.tzinfo is None):
        da, db_ = da.replace(tzinfo=None), db_.replace(tzinfo=None)
    return abs((da - db_).days) <= window_days


def find_subject_fallback_thread(
    message: dict, candidate_threads: list[dict],
    window_days: int = SUBJECT_FALLBACK_WINDOW_DAYS,
) -> int | None:
    """message: {"subject", "sender_address", "to_addresses": list[str],
    "date"} — one already header-parsed message, not yet threaded.

    candidate_threads: threads that already share this message's
    normalized subject (the caller filters by subject_norm before calling
    this — matching every thread in a mailbox here would be pure waste) —
    each {"id", "participants": set[str], "last_date": str | None}.

    Returns the id of the thread this message should join, or None to
    start a new one. When several candidates qualify, the most recently
    active one wins — the natural reading of "continue this conversation"
    when more than one same-subject thread technically fits.
    """
    subject_norm = normalize_subject(message.get("subject"))
    if not subject_norm:
        return None
    participants = _participants(message)
    if not participants:
        return None

    matches = [
        c for c in candidate_threads
        if c["participants"] & participants
        and _within_window(message.get("date"), c.get("last_date"), window_days)
    ]
    if not matches:
        return None
    matches.sort(key=lambda c: c.get("last_date") or "", reverse=True)
    return matches[0]["id"]
