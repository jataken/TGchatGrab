"""Is a source worth collecting? Pairs raw volume (chat_storage(), from
mixins/retention.py — mixed into the same Database instance) with how
often the chat actually produced something — a watch hit or a bot rule
firing. Also owns the per-day activity-bar cache the chats screen renders
as a small sparkline."""
from __future__ import annotations

import datetime as dt


class ProductivityMixin:
    def chat_productivity(self, days: int = 30) -> list[dict]:
        """Is a source worth collecting? Volume alone does not say — this
        pairs it with how often the chat produced something the user
        actually asked to be told about (watch hits) or that a bot rule
        turned into a lead."""
        since = (dt.datetime.now() - dt.timedelta(days=days)).isoformat()
        out = []
        for row in self.chat_storage():
            cid = row["chat_id"]
            recent = self.query_one(
                "SELECT count(*) AS c FROM messages WHERE chat_id = ? AND date >= ?",
                (cid, since),
            )["c"]
            hits = self.query_one(
                "SELECT count(*) AS c FROM watch_hit WHERE chat_id = ? AND matched_at >= ?",
                (cid, since),
            )["c"]
            fired = self.query_one(
                "SELECT count(*) AS c FROM bot_activity_log "
                "WHERE chat_id = ? AND kind = 'trigger_fired' AND timestamp >= ?",
                (cid, since),
            )["c"]
            out.append({
                **row,
                "recent": recent,
                "per_day": round(recent / max(1, days), 1),
                "watch_hits": hits,
                "triggers": fired,
            })
        return out

    def rebuild_stat_cache(self, chat_id: int, days: int = 16) -> None:
        since = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
        rows = self.query(
            """SELECT date(date) AS day, count(*) AS c FROM messages
               WHERE chat_id = ? AND date(date) >= ? GROUP BY date(date)""",
            (chat_id, since),
        )
        with self._lock:
            self._conn.execute("DELETE FROM chat_stat_cache WHERE chat_id = ?", (chat_id,))
            self._conn.executemany(
                "INSERT INTO chat_stat_cache(chat_id, day, count) VALUES (?, ?, ?)",
                [(chat_id, r["day"], r["c"]) for r in rows],
            )
            self._conn.commit()

    def activity_bars(self, chat_id: int, days: int = 16) -> list[int]:
        rows = self.query(
            "SELECT day, count FROM chat_stat_cache WHERE chat_id = ? ORDER BY day", (chat_id,)
        )
        by_day = {r["day"]: r["count"] for r in rows}
        today = dt.date.today()
        return [by_day.get((today - dt.timedelta(days=i)).isoformat(), 0)
                for i in range(days - 1, -1, -1)]
