"""П10: whether one filter's conditions match a message — pure, over
already-fetched fields, same discipline as mail_triage.py/mail_thread.py
(no sqlite, no Qt, no network).

A filter's conditions are ANDed (matches() returns False on the first
one that doesn't hold); OR across filters is just having more than one
filter, no logic needed for that here. What actions to take when a
filter matches (label/move/mark-read/no-notify, never delete — see
db/schema.py's own docstring on mail_filter) is the caller's job
(MailService._apply_mail_filters()), not this module's — a condition
match is the only thing that needs to stay this side of the DB.
"""
from __future__ import annotations

_TEXT_FIELDS = {
    "sender": "sender_address",
    "domain": "sender_address",
    "subject": "subject",
    "body": "body_text",
}


def _domain_of(address: str | None) -> str:
    address = (address or "").strip().lower()
    return address.rsplit("@", 1)[-1] if "@" in address else ""


def _text_value(field: str, fields: dict) -> str:
    if field == "domain":
        return _domain_of(fields.get("sender_address"))
    return (fields.get(_TEXT_FIELDS.get(field, field)) or "").strip().lower()


def _matches_one(cond: dict, fields: dict) -> bool:
    field = cond.get("field")
    op = cond.get("op", "contains")
    value = cond.get("value", "")

    if field == "has_attachment":
        # "found nothing yet" (header-stage, body not fetched) reads as
        # False rather than an error — same as every other body-dependent
        # signal in this app (mail_triage's own attachment-text checks).
        return bool(fields.get("has_attachments")) == _truthy(value)

    if field == "size_over_kb":
        try:
            threshold = float(value)
        except (TypeError, ValueError):
            return False
        return (fields.get("total_attachment_bytes") or 0) >= threshold * 1024

    if field == "mailbox":
        try:
            return fields.get("mailbox_id") == int(value)
        except (TypeError, ValueError):
            return False

    if field in _TEXT_FIELDS:
        haystack = _text_value(field, fields)
        needle = (value or "").strip().lower()
        if not needle:
            return False
        if op == "equals":
            return haystack == needle
        if op == "starts_with":
            return haystack.startswith(needle)
        return needle in haystack  # "contains" — the default op

    return False


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "да", "yes")


def matches(conditions: list[dict], fields: dict) -> bool:
    """fields — whatever the caller already has for this message:
    subject, body_text, sender_address, has_attachments,
    total_attachment_bytes, mailbox_id. Not every key
    needs to be present — a condition whose field isn't in fields simply
    can't match (has_attachment/size_over_kb read as False, text fields
    as an empty haystack), the same "not there yet" shape mail_triage.py
    already uses for header-stage-only scoring.

    A filter with zero conditions never matches anything — an empty
    filter isn't "match everything," it's a filter nobody finished
    configuring yet."""
    if not conditions:
        return False
    return all(_matches_one(cond, fields) for cond in conditions)
