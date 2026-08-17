"""С8's funnel/source report queries — see PLAN.md's С8 journal entry for
why all four are scoped by bot_leads.created_at rather than by when an
event inside the range happened."""
from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any

from ...core import lead as lead_domain


class ReportsMixin:
    # All four scoped by bot_leads.created_at, not by when an event inside
    # the range happened — a lead created just before date_from that later
    # got a status change inside the range would otherwise show up in the
    # "срок до КП"/"причины отказов" numbers but not in "конверсия по
    # источникам", two different populations under one report. Scoping all
    # four the same way keeps them describing the same set of leads.
    def leads_report_by_source(self, date_from: str | None = None,
                                date_to: str | None = None) -> list[sqlite3.Row]:
        """One row per source chat, plus a NULL bucket (source_chat_id
        IS NULL — bot-triggered or manually created leads never had one)
        — see core/lead_report.conversion() for what a caller does with
        total/won/lost."""
        sql = (
            "SELECT bl.source_chat_id AS chat_id, c.title AS chat_title, "
            "count(*) AS total, "
            "sum(CASE WHEN bl.status = ? THEN 1 ELSE 0 END) AS won, "
            "sum(CASE WHEN bl.status = ? THEN 1 ELSE 0 END) AS lost "
            "FROM bot_leads bl LEFT JOIN chats c ON c.chat_id = bl.source_chat_id"
        )
        params: list[Any] = [lead_domain.WON, lead_domain.LOST]
        clauses = []
        if date_from is not None:
            clauses.append("bl.created_at >= ?")
            params.append(date_from)
        if date_to is not None:
            clauses.append("bl.created_at <= ?")
            params.append(date_to)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " GROUP BY bl.source_chat_id ORDER BY total DESC"
        return self.query(sql, params)

    def leads_report_by_direction(self, date_from: str | None = None,
                                   date_to: str | None = None) -> list[sqlite3.Row]:
        """Same shape as leads_report_by_source, grouped by direction_id
        instead — including its own NULL bucket for leads with no
        direction set."""
        sql = (
            "SELECT bl.direction_id AS direction_id, d.name AS direction_name, "
            "count(*) AS total, "
            "sum(CASE WHEN bl.status = ? THEN 1 ELSE 0 END) AS won, "
            "sum(CASE WHEN bl.status = ? THEN 1 ELSE 0 END) AS lost "
            "FROM bot_leads bl LEFT JOIN direction d ON d.id = bl.direction_id"
        )
        params: list[Any] = [lead_domain.WON, lead_domain.LOST]
        clauses = []
        if date_from is not None:
            clauses.append("bl.created_at >= ?")
            params.append(date_from)
        if date_to is not None:
            clauses.append("bl.created_at <= ?")
            params.append(date_to)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " GROUP BY bl.direction_id ORDER BY total DESC"
        return self.query(sql, params)

    def avg_days_to_quote(self, date_from: str | None = None,
                           date_to: str | None = None) -> float | None:
        """Mean days from a lead's created_at to the first time it reached
        quote_sent — the earliest matching lead_events row, in case a
        lead bounced back to an earlier stage and through quote_sent
        again later. None (not 0) when nothing in range ever reached that
        stage: there's no average of an empty set."""
        sql = (
            "SELECT bl.created_at AS created_at, MIN(le.created_at) AS quoted_at "
            "FROM bot_leads bl JOIN lead_events le ON le.lead_id = bl.id "
            "WHERE le.kind = ? AND le.to_status = ?"
        )
        params: list[Any] = [lead_domain.EVENT_KIND_STATUS, lead_domain.QUOTE_SENT]
        if date_from is not None:
            sql += " AND bl.created_at >= ?"
            params.append(date_from)
        if date_to is not None:
            sql += " AND bl.created_at <= ?"
            params.append(date_to)
        sql += " GROUP BY bl.id"
        rows = self.query(sql, params)
        diffs = []
        for r in rows:
            try:
                created = dt.datetime.fromisoformat(r["created_at"])
                quoted = dt.datetime.fromisoformat(r["quoted_at"])
            except (ValueError, TypeError):
                continue
            diffs.append((quoted - created).total_seconds() / 86400)
        return sum(diffs) / len(diffs) if diffs else None

    def reject_reasons_report(self, date_from: str | None = None,
                               date_to: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT reject_reason, count(*) AS c FROM bot_leads WHERE status = ?"
        params: list[Any] = [lead_domain.LOST]
        if date_from is not None:
            sql += " AND created_at >= ?"
            params.append(date_from)
        if date_to is not None:
            sql += " AND created_at <= ?"
            params.append(date_to)
        sql += " GROUP BY reject_reason ORDER BY c DESC"
        return self.query(sql, params)
