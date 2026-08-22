"""С8: the one bit of arithmetic behind every conversion number the
funnel/source report shows — kept here, not duplicated between the
report screen's table and export_service's workbook writer, so both
always agree on what a percentage means.
"""
from __future__ import annotations


def conversion(total: int, won: int, lost: int) -> dict:
    """total/won/lost -> the row a report table or sheet actually shows.
    in_progress is whatever's left, not a separate count — a lead is
    exactly one of won/lost/still-open at any moment, so there's nothing
    to query for it beyond the other two. conversion_pct is against
    total, not against decided (won+lost): an open lead is still part of
    "how many of what came in turned into a sale so far," which is the
    number this report exists to answer.
    """
    in_progress = max(0, total - won - lost)
    conversion_pct = round(won / total * 100, 1) if total else 0.0
    return {
        "total": total, "won": won, "lost": lost,
        "in_progress": in_progress, "conversion_pct": conversion_pct,
    }
