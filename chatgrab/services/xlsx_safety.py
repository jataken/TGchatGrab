"""Excel formula-injection guard, shared by every openpyxl writer.

Message text, sender names, chat titles and scenario answers all come
from Telegram — any account can set them to something like
`=cmd|'/c calc'!A1` or `=HYPERLINK("http://evil","click")`. openpyxl
auto-detects a leading "=" and stores the cell as a real *formula*
(data_type='f'), which Excel then evaluates on open — arbitrary,
attacker-controlled formula execution (including legacy DDE payloads)
via a normal export. Verified directly against the installed openpyxl:
a string cell set to "=1+1" comes back with data_type == 'f'; one set
to "+1+1", "-1+1" or "@SUM(1,1)" comes back as plain data_type == 's'
— those three are a real risk for CSV (Excel's heuristic type-guessing
on import), but not for a genuine .xlsx cell, so they're intentionally
*not* touched here: prefixing them would only cosmetically alter
otherwise-legitimate content (a username, a phone number, a price
delta) without closing any actual gap in this file format. Tab and CR
are kept as a cheap, low-collateral extra guard against control-
character tricks in some downstream consumers.
"""
from __future__ import annotations

_TRIGGER_CHARS = ("=", "\t", "\r")


def excel_safe(value):
    """Prefix a leading formula-trigger character with an apostrophe so
    openpyxl stores it as plain text, never as a formula. Non-string
    values (numbers, None) pass through untouched — only text can carry
    a formula payload."""
    if isinstance(value, str) and value.startswith(_TRIGGER_CHARS):
        return "'" + value
    return value
