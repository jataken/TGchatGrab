"""Р6: display-string formatting shared across screens — small pure
functions, not widget construction (that's widgets.py's job), so this
stays its own module rather than growing widgets.py further.
"""
from __future__ import annotations


def short_dt(value: str | None) -> str:
    """An ISO datetime string (e.g. "2026-08-17T16:34:22+00:00") ->
    "2026-08-17 16:34" for display — the app-wide "just show date and
    minute" convention every screen with a timestamp column already used,
    13 times over, each its own copy of the same three operations.

    None or empty -> "" (not "None") — a caller that wants a fallback
    word for the empty case still writes `short_dt(x) or "ещё не
    запускалась"`, exactly as the `(x or "")[...] or "запускалась"`
    copies already did; this just replaces the "..." part.
    """
    if not value:
        return ""
    return str(value)[:16].replace("T", " ")
