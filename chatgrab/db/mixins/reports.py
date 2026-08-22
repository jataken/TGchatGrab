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
        total/won/lost. С10: won/lost come from each lead's own
        funnel_stage.kind, not a hardcoded status string, so a lead from
        any funnel (a future mail funnel included, П9) counts correctly
        here — this is the "отчёт сравнивает воронки с разными этапами"
        half of С10's acceptance criterion."""
        sql = (
            "SELECT bl.source_chat_id AS chat_id, c.title AS chat_title, "
            "count(*) AS total, "
            "sum(CASE WHEN fs.kind = 'won' THEN 1 ELSE 0 END) AS won, "
            "sum(CASE WHEN fs.kind = 'lost' THEN 1 ELSE 0 END) AS lost "
            "FROM bot_leads bl LEFT JOIN chats c ON c.chat_id = bl.source_chat_id "
            "LEFT JOIN funnel_stage fs ON fs.funnel_id = bl.funnel_id AND fs.code = bl.status"
        )
        params: list[Any] = []
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
            "sum(CASE WHEN fs.kind = 'won' THEN 1 ELSE 0 END) AS won, "
            "sum(CASE WHEN fs.kind = 'lost' THEN 1 ELSE 0 END) AS lost "
            "FROM bot_leads bl LEFT JOIN direction d ON d.id = bl.direction_id "
            "LEFT JOIN funnel_stage fs ON fs.funnel_id = bl.funnel_id AND fs.code = bl.status"
        )
        params: list[Any] = []
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

    def leads_report_by_channel(self, date_from: str | None = None,
                                 date_to: str | None = None) -> list[sqlite3.Row]:
        """П10's «сводный отчёт по каналам» — same shape and same
        funnel_stage.kind-based won/lost as leads_report_by_source/
        by_direction, grouped by origin_channel instead: the immutable
        first-touch attribution С10 introduced (telegram/email so far),
        not funnel_id — a lead manually transferred to a different
        funnel (С10's transfer_lead_funnel()) came in on the same
        channel it always did, and that's what "how many clients come
        through which channel" is asking about."""
        sql = (
            "SELECT bl.origin_channel AS channel, "
            "count(*) AS total, "
            "sum(CASE WHEN fs.kind = 'won' THEN 1 ELSE 0 END) AS won, "
            "sum(CASE WHEN fs.kind = 'lost' THEN 1 ELSE 0 END) AS lost "
            "FROM bot_leads bl "
            "LEFT JOIN funnel_stage fs ON fs.funnel_id = bl.funnel_id AND fs.code = bl.status"
        )
        params: list[Any] = []
        clauses = []
        if date_from is not None:
            clauses.append("bl.created_at >= ?")
            params.append(date_from)
        if date_to is not None:
            clauses.append("bl.created_at <= ?")
            params.append(date_to)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " GROUP BY bl.origin_channel ORDER BY total DESC"
        return self.query(sql, params)

    def leads_report_by_channel_and_direction(self, date_from: str | None = None,
                                               date_to: str | None = None) -> list[sqlite3.Row]:
        """The «канал × направление» cut — "упаковка идёt с биржи, сырьё
        — почтой" is a cross-tab, not two separate one-dimensional
        reports, so it gets its own query rather than asking the report
        screen to zip leads_report_by_channel/by_direction together
        (that would silently assume the two group independently, which
        a cross-tab specifically shouldn't)."""
        sql = (
            "SELECT bl.origin_channel AS channel, bl.direction_id AS direction_id, "
            "d.name AS direction_name, count(*) AS total "
            "FROM bot_leads bl LEFT JOIN direction d ON d.id = bl.direction_id"
        )
        params: list[Any] = []
        clauses = []
        if date_from is not None:
            clauses.append("bl.created_at >= ?")
            params.append(date_from)
        if date_to is not None:
            clauses.append("bl.created_at <= ?")
            params.append(date_to)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " GROUP BY bl.origin_channel, bl.direction_id ORDER BY channel, total DESC"
        return self.query(sql, params)

    def avg_days_to_quote(self, date_from: str | None = None,
                           date_to: str | None = None) -> float | None:
        """Mean days from a lead's created_at to the first time it reached
        quote_sent — the earliest matching lead_events row, in case a
        lead bounced back to an earlier stage and through quote_sent
        again later. None (not 0) when nothing in range ever reached that
        stage: there's no average of an empty set.

        С10 note: deliberately still hardcoded to the default funnel's
        own QUOTE_SENT code, unlike the two reports above — "days to
        quote" is a business metric specific to that funnel's own stage
        naming (there's no `kind` equivalent to "a quote was sent" the
        way there's one for won/lost), so generalizing it would mean
        making "which stage counts as the quote milestone" itself
        configurable per funnel, which isn't part of this session's
        checklist. Leads on a different funnel (a future mail funnel,
        П9) simply never match here, same as before С10 existed."""
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

    def avg_days_to_win_by_channel(self, date_from: str | None = None,
                                    date_to: str | None = None) -> dict[str, float]:
        """The «средний срок» half of the channel report — unlike
        avg_days_to_quote (deliberately still hardcoded to one funnel's
        own quote_sent code, see that method's docstring), "reached a
        won-kind stage" *is* a kind-level concept, so this one genuinely
        generalizes across funnels instead of picking one funnel's own
        stage name: a lead's first (earliest, in case of a re-open)
        lead_events row whose to_status resolves to a kind='won' stage
        *in the funnel that lead is currently in*. Same "resolved
        against the lead's current funnel" approximation lead_card.py's
        own history tab already accepts for a status predating a later
        funnel transfer — see that screen's comment."""
        sql = (
            "SELECT bl.origin_channel AS channel, bl.created_at AS created_at, "
            "MIN(le.created_at) AS won_at "
            "FROM bot_leads bl "
            "JOIN lead_events le ON le.lead_id = bl.id AND le.kind IN ('status', 'funnel') "
            "JOIN funnel_stage fs ON fs.funnel_id = bl.funnel_id AND fs.code = le.to_status "
            "WHERE fs.kind = 'won'"
        )
        params: list[Any] = []
        if date_from is not None:
            sql += " AND bl.created_at >= ?"
            params.append(date_from)
        if date_to is not None:
            sql += " AND bl.created_at <= ?"
            params.append(date_to)
        sql += " GROUP BY bl.id"
        rows = self.query(sql, params)
        by_channel: dict[str, list[float]] = {}
        for r in rows:
            try:
                created = dt.datetime.fromisoformat(r["created_at"])
                won = dt.datetime.fromisoformat(r["won_at"])
            except (ValueError, TypeError):
                continue
            by_channel.setdefault(r["channel"] or "", []).append((won - created).total_seconds() / 86400)
        return {channel: sum(days) / len(days) for channel, days in by_channel.items() if days}

    def reject_reasons_report(self, date_from: str | None = None,
                               date_to: str | None = None) -> list[sqlite3.Row]:
        sql = (
            "SELECT bl.reject_reason AS reject_reason, count(*) AS c FROM bot_leads bl "
            "JOIN funnel_stage fs ON fs.funnel_id = bl.funnel_id AND fs.code = bl.status "
            "WHERE fs.kind = 'lost'"
        )
        params: list[Any] = []
        if date_from is not None:
            sql += " AND bl.created_at >= ?"
            params.append(date_from)
        if date_to is not None:
            sql += " AND bl.created_at <= ?"
            params.append(date_to)
        sql += " GROUP BY bl.reject_reason ORDER BY c DESC"
        return self.query(sql, params)

    # ---- П10: скорость ответа по почте ---------------------------------
    def _mail_thread_response_times(self, date_from: str | None, date_to: str | None) -> str:
        """Shared subquery both mail_response_time_by_mailbox/by_direction
        build on: per thread, the first incoming message's date and the
        first outgoing one after it — "time from when the client first
        wrote to when we first answered," not every back-and-forth in
        the thread (a later reply-to-a-reply isn't counted a second
        time). A thread we started ourselves (no incoming before our
        first outgoing) never gets a row here — there's no "response
        time" to something we sent first. Returns SQL text, not rows;
        both callers wrap it with their own GROUP BY."""
        clauses = ["first_in IS NOT NULL", "first_out IS NOT NULL", "first_out > first_in"]
        if date_from is not None:
            clauses.append("first_in >= :date_from")
        if date_to is not None:
            clauses.append("first_in <= :date_to")
        return (
            "SELECT thread_id, mailbox_id, lead_id, first_in, first_out FROM ("
            "  SELECT t.id AS thread_id, t.mailbox_id AS mailbox_id, t.lead_id AS lead_id, "
            "  MIN(CASE WHEN m.is_outgoing = 0 THEN m.date END) AS first_in, "
            "  MIN(CASE WHEN m.is_outgoing = 1 THEN m.date END) AS first_out "
            "  FROM mail_thread t JOIN mail_message m ON m.thread_id = t.id "
            "  GROUP BY t.id"
            ") WHERE " + " AND ".join(clauses)
        )

    def mail_response_time_by_mailbox(self, date_from: str | None = None,
                                       date_to: str | None = None) -> list[sqlite3.Row]:
        sql = (
            "SELECT sub.mailbox_id AS mailbox_id, mb.address AS mailbox_address, "
            "AVG((julianday(sub.first_out) - julianday(sub.first_in)) * 24) AS avg_hours, "
            "MAX((julianday(sub.first_out) - julianday(sub.first_in)) * 24) AS worst_hours, "
            "count(*) AS n "
            f"FROM ({self._mail_thread_response_times(date_from, date_to)}) sub "
            "JOIN mailbox mb ON mb.id = sub.mailbox_id "
            "GROUP BY sub.mailbox_id ORDER BY avg_hours DESC"
        )
        params = {"date_from": date_from, "date_to": date_to}
        return self.query(sql, params)

    def mail_response_time_by_direction(self, date_from: str | None = None,
                                         date_to: str | None = None) -> list[sqlite3.Row]:
        """Only threads linked to a lead with a direction set count here
        — an unlinked thread has no direction to attribute the response
        time to, same "simply doesn't appear in this cut" shape as the
        NULL-direction bucket the Telegram-side reports already show
        explicitly (this one only shows rows that resolved to a real
        direction, since "response speed for no direction" isn't a
        question this report is trying to answer)."""
        sql = (
            "SELECT bl.direction_id AS direction_id, d.name AS direction_name, "
            "AVG((julianday(sub.first_out) - julianday(sub.first_in)) * 24) AS avg_hours, "
            "MAX((julianday(sub.first_out) - julianday(sub.first_in)) * 24) AS worst_hours, "
            "count(*) AS n "
            f"FROM ({self._mail_thread_response_times(date_from, date_to)}) sub "
            "JOIN bot_leads bl ON bl.id = sub.lead_id "
            "JOIN direction d ON d.id = bl.direction_id "
            "GROUP BY bl.direction_id ORDER BY avg_hours DESC"
        )
        params = {"date_from": date_from, "date_to": date_to}
        return self.query(sql, params)
