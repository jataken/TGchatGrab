"""Thin, thread-safe SQLite access layer. One connection, one file, guarded
by a lock so it can be safely called both from the asyncio/Qt main thread
and from executor threads (bulk export, backups, VACUUM)."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from . import schema


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=OFF;")
        schema.migrate(self._conn)

    # ---- low level -------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def executemany(self, sql: str, seq: Iterable[Iterable[Any]]) -> None:
        with self._lock:
            self._conn.executemany(sql, seq)
            self._conn.commit()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def transaction(self):
        return _Transaction(self)

    # ---- settings (generic JSON key/value) --------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value FROM app_settings WHERE key = ?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return default

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO app_settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    # ---- chats --------------------------------------------------------
    def add_chat(self, chat_id: int, title: str, username: str | None,
                 depth_mode: str = "all", depth_from_date: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO chats(chat_id, title, username, is_tracked, enabled,
                       depth_mode, depth_from_date, status, created_at, queue_order)
                   VALUES (?, ?, ?, 1, 1, ?, ?, 'queued', ?,
                       (SELECT COALESCE(MAX(queue_order), 0) + 1 FROM chats))
                   ON CONFLICT(chat_id) DO UPDATE SET
                       is_tracked = 1, title = excluded.title, username = excluded.username""",
                (chat_id, title, username, depth_mode, depth_from_date, now_iso()),
            )
            self._conn.commit()

    def list_chats(self, tracked_only: bool = True) -> list[sqlite3.Row]:
        sql = "SELECT * FROM chats"
        if tracked_only:
            sql += " WHERE is_tracked = 1"
        sql += " ORDER BY queue_order"
        return self.query(sql)

    def get_chat(self, chat_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM chats WHERE chat_id = ?", (chat_id,))

    def set_chat_field(self, chat_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(
            f"UPDATE chats SET {cols} WHERE chat_id = ?",
            (*fields.values(), chat_id),
        )

    def untrack_chat(self, chat_id: int) -> None:
        """Remove from the list but keep collected messages/photos."""
        self.set_chat_field(chat_id, is_tracked=0, enabled=0, status="off")

    def delete_chat_and_data(self, chat_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
            self._conn.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
            self._conn.execute("DELETE FROM chat_stat_cache WHERE chat_id = ?", (chat_id,))
            self._conn.execute("DELETE FROM ignore_rule WHERE chat_id = ?", (chat_id,))
            self._conn.commit()

    # ---- messages -------------------------------------------------
    def upsert_message(self, m: dict[str, Any]) -> bool:
        """Insert or update-on-edit a message, keyed on (chat_id, message_id).
        Returns True if a new row was inserted, False if an existing one was
        updated (edit)."""
        existing = self.query_one(
            "SELECT id FROM messages WHERE chat_id = ? AND message_id = ?",
            (m["chat_id"], m["message_id"]),
        )
        m = dict(m)
        m["char_len"] = len(m.get("text") or "")
        m["is_reply"] = 1 if m.get("reply_to_message_id") else 0
        m["is_forward"] = 1 if m.get("forwarded_from") else 0
        m["is_hidden"] = 1 if m.get("is_hidden") else 0
        cols = ["chat_id", "message_id", "chat_title", "date", "edited_date",
                "sender_id", "sender_username", "sender_display_name", "text",
                "reply_to_message_id", "forwarded_from", "media_type",
                "media_caption", "media_path", "views", "link", "char_len",
                "is_reply", "is_forward", "is_hidden"]
        values = [m.get(c) for c in cols]
        with self._lock:
            if existing:
                set_clause = ", ".join(f"{c} = ?" for c in cols)
                self._conn.execute(
                    f"UPDATE messages SET {set_clause} WHERE id = ?",
                    (*values, existing["id"]),
                )
                self._conn.commit()
                return False
            placeholders = ", ".join("?" for _ in cols)
            self._conn.execute(
                f"INSERT INTO messages({', '.join(cols)}) VALUES ({placeholders})",
                values,
            )
            self._conn.commit()
            return True

    def message_count(self, chat_id: int | None = None, include_hidden: bool = True) -> int:
        sql = "SELECT count(*) AS c FROM messages"
        clauses, params = [], []
        if chat_id is not None:
            clauses.append("chat_id = ?")
            params.append(chat_id)
        if not include_hidden:
            clauses.append("is_hidden = 0")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return self.query_one(sql, params)["c"]

    def media_count(self, chat_id: int | None = None, media_type: str | None = None) -> int:
        sql = "SELECT count(*) AS c FROM messages WHERE media_path IS NOT NULL AND media_path != ''"
        params: list[Any] = []
        if chat_id is not None:
            sql += " AND chat_id = ?"
            params.append(chat_id)
        if media_type is not None:
            sql += " AND media_type = ?"
            params.append(media_type)
        return self.query_one(sql, params)["c"]

    def last_message_date(self, chat_id: int) -> str | None:
        row = self.query_one(
            "SELECT max(date) AS d FROM messages WHERE chat_id = ?", (chat_id,)
        )
        return row["d"] if row else None

    def existing_message_ids(self, chat_id: int) -> set[int]:
        rows = self.query("SELECT message_id FROM messages WHERE chat_id = ?", (chat_id,))
        return {r["message_id"] for r in rows}

    def min_max_message_id(self, chat_id: int) -> tuple[int | None, int | None]:
        row = self.query_one(
            "SELECT min(message_id) AS mn, max(message_id) AS mx FROM messages WHERE chat_id = ?",
            (chat_id,),
        )
        return (row["mn"], row["mx"]) if row else (None, None)

    def find_gaps(self, chat_id: int) -> list[tuple[int, int]]:
        """Missing message_id ranges between the min and max ids we hold."""
        rows = self.query(
            "SELECT message_id FROM messages WHERE chat_id = ? ORDER BY message_id",
            (chat_id,),
        )
        ids = [r["message_id"] for r in rows]
        gaps: list[tuple[int, int]] = []
        for a, b in zip(ids, ids[1:]):
            if b - a > 1:
                gaps.append((a + 1, b - 1))
        return gaps

    # ---- authors ----------------------------------------------------
    def authors_for_chat(self, chat_id: int) -> list[sqlite3.Row]:
        return self.query(
            """SELECT sender_id, sender_username, sender_display_name,
                      count(*) AS n, min(date) AS first, max(date) AS last
               FROM messages WHERE chat_id = ? AND sender_id IS NOT NULL
               GROUP BY sender_id ORDER BY n DESC""",
            (chat_id,),
        )

    # ---- ignore rules -------------------------------------------------
    def add_ignore_rule(self, rule_type: str, value: str, scope: str, chat_id: int | None) -> None:
        self.execute(
            "INSERT INTO ignore_rule(rule_type, value, scope, chat_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (rule_type, value, scope, chat_id, now_iso()),
        )

    def delete_ignore_rule(self, rule_id: int) -> None:
        self.execute("DELETE FROM ignore_rule WHERE id = ?", (rule_id,))

    def list_ignore_rules(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM ignore_rule ORDER BY created_at DESC")

    def set_message_hidden(self, chat_id: int, message_id: int, hidden: bool) -> None:
        self.execute(
            "UPDATE messages SET is_hidden = ? WHERE chat_id = ? AND message_id = ?",
            (1 if hidden else 0, chat_id, message_id),
        )

    def apply_ignore_rules(self, rules: list[sqlite3.Row]) -> int:
        """Mark existing messages matching author/stopword rules as hidden.
        Never deletes — only sets is_hidden, so it can be undone."""
        total = 0
        with self._lock:
            for r in rules:
                if r["rule_type"] == "author":
                    sql = "UPDATE messages SET is_hidden = 1 WHERE is_hidden = 0 AND (sender_username = ? OR sender_display_name = ?)"
                    params: list[Any] = [r["value"], r["value"]]
                else:
                    sql = "UPDATE messages SET is_hidden = 1 WHERE is_hidden = 0 AND text LIKE ?"
                    params = [f"%{r['value']}%"]
                if r["scope"] == "chat" and r["chat_id"] is not None:
                    sql += " AND chat_id = ?"
                    params.append(r["chat_id"])
                cur = self._conn.execute(sql, params)
                total += cur.rowcount
            self._conn.commit()
        return total

    # ---- export selection ---------------------------------------------
    def export_select(self, chat_ids: list[int], date_from: str | None = None,
                       date_to: str | None = None, include_hidden: bool = False,
                       query: str = "", author: str = "", photos_only: bool = False,
                       forwards_only: bool = False, replies_only: bool = False,
                       min_id_by_chat: dict[int, int] | None = None) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[Any] = []
        from_sql = "FROM messages m"
        if query.strip():
            from_sql = "FROM messages_fts f JOIN messages m ON m.id = f.rowid"
            clauses.append("messages_fts MATCH ?")
            params.append(_fts_query(query))
        if chat_ids:
            if min_id_by_chat is not None:
                or_parts = []
                for cid in chat_ids:
                    if cid in min_id_by_chat:
                        or_parts.append("(m.chat_id = ? AND m.message_id > ?)")
                        params += [cid, min_id_by_chat[cid]]
                    else:
                        or_parts.append("(m.chat_id = ?)")
                        params.append(cid)
                clauses.append("(" + " OR ".join(or_parts) + ")")
            else:
                placeholders = ",".join("?" for _ in chat_ids)
                clauses.append(f"m.chat_id IN ({placeholders})")
                params += list(chat_ids)
        else:
            return []
        if date_from:
            clauses.append("m.date >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("m.date <= ?")
            params.append(date_to)
        if author.strip():
            clauses.append("(m.sender_display_name LIKE ? OR m.sender_username LIKE ?)")
            like = f"%{author.strip()}%"
            params += [like, like]
        if photos_only:
            clauses.append("m.media_type = 'photo' AND m.media_path IS NOT NULL AND m.media_path != ''")
        if forwards_only:
            clauses.append("m.is_forward = 1")
        if replies_only:
            clauses.append("m.is_reply = 1")
        if not include_hidden:
            clauses.append("m.is_hidden = 0")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        # Pure chronological order — not grouped by chat first — so a
        # merged multi-chat export reads as one continuous timeline
        # (old to new) instead of one chat's whole history, then the
        # next chat's from its own beginning. Per-chat file splitting
        # groups these afterward without disturbing the relative order,
        # so single-chat output stays date-sorted too.
        return self.query(
            f"SELECT m.* {from_sql}{where} ORDER BY m.date ASC", params
        )

    # ---- export log / presets ----------------------------------------
    def add_export_log(self, **fields: Any) -> int:
        cols = list(fields.keys())
        placeholders = ", ".join("?" for _ in cols)
        with self._lock:
            cur = self._conn.execute(
                f"INSERT INTO export_log({', '.join(cols)}) VALUES ({placeholders})",
                list(fields.values()),
            )
            self._conn.commit()
            return cur.lastrowid

    def list_export_log(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM export_log ORDER BY id DESC LIMIT ?", (limit,))

    def last_export_max_id(self, chat_id: int) -> int | None:
        rows = self.query(
            "SELECT max_message_id_by_chat FROM export_log ORDER BY id DESC LIMIT 50"
        )
        for row in rows:
            data = json.loads(row["max_message_id_by_chat"])
            if str(chat_id) in data:
                return data[str(chat_id)]
        return None

    def incremental_baseline(self, chat_ids: list[int]) -> dict[int, int]:
        """Highest message_id already exported per chat, across all past
        exports — the cutoff for "только новое с прошлой выгрузки"."""
        wanted = set(chat_ids)
        best: dict[int, int] = {}
        for row in self.query("SELECT max_message_id_by_chat FROM export_log"):
            data = json.loads(row["max_message_id_by_chat"])
            for k, v in data.items():
                cid = int(k)
                if cid in wanted:
                    best[cid] = max(best.get(cid, 0), v)
        return best

    def save_preset(self, name: str, params: dict[str, Any]) -> None:
        self.execute(
            "INSERT INTO export_preset(name, params, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET params = excluded.params",
            (name, json.dumps(params, ensure_ascii=False), now_iso()),
        )

    def list_presets(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM export_preset ORDER BY created_at DESC")

    def delete_preset(self, name: str) -> None:
        self.execute("DELETE FROM export_preset WHERE name = ?", (name,))

    # ---- stats cache -------------------------------------------------
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

    # ---- search (FTS5) -------------------------------------------------
    def search_messages(self, query: str = "", chat_id: int | None = None,
                         author: str = "", date_from: str | None = None,
                         date_to: str | None = None, photos_only: bool = False,
                         forwards_only: bool = False, replies_only: bool = False,
                         include_hidden: bool = False, sort_desc: bool = True,
                         page: int = 0, page_size: int = 100) -> tuple[list[sqlite3.Row], int]:
        clauses: list[str] = []
        params: list[Any] = []
        from_sql = "FROM messages m"
        if query.strip():
            from_sql = "FROM messages_fts f JOIN messages m ON m.id = f.rowid"
            clauses.append("messages_fts MATCH ?")
            params.append(_fts_query(query))
        if chat_id:
            clauses.append("m.chat_id = ?")
            params.append(chat_id)
        if author.strip():
            clauses.append("(m.sender_display_name LIKE ? OR m.sender_username LIKE ?)")
            like = f"%{author.strip()}%"
            params += [like, like]
        if date_from:
            clauses.append("m.date >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("m.date <= ?")
            params.append(date_to)
        if photos_only:
            clauses.append("m.media_type = 'photo' AND m.media_path IS NOT NULL AND m.media_path != ''")
        if forwards_only:
            clauses.append("m.is_forward = 1")
        if replies_only:
            clauses.append("m.is_reply = 1")
        if not include_hidden:
            clauses.append("m.is_hidden = 0")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        total = self.query_one(f"SELECT count(*) AS c {from_sql}{where}", params)["c"]
        order = "DESC" if sort_desc else "ASC"
        rows = self.query(
            f"SELECT m.* {from_sql}{where} ORDER BY m.date {order} LIMIT ? OFFSET ?",
            [*params, page_size, page * page_size],
        )
        return rows, total

    # ---- maintenance -------------------------------------------------
    def vacuum(self) -> None:
        with self._lock:
            self._conn.commit()
            self._conn.execute("VACUUM;")

    def backup_to(self, dest_path: Path) -> None:
        """Consistent online copy via SQLite's own backup API — safe to run
        while the app keeps writing to the live database."""
        with self._lock:
            dest = sqlite3.connect(str(dest_path))
            try:
                self._conn.backup(dest)
            finally:
                dest.close()

    def checkpoint(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(FULL);")

    def file_size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- bots -----------------------------------------------------------
    def add_bot(self, name: str, type_: str, token_encrypted: str | None,
                preset: str = "custom", manager_chat_id: str | None = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO bots(name, type, token_encrypted, preset, manager_chat_id,
                       status, created_at)
                   VALUES (?, ?, ?, ?, ?, 'stopped', ?)""",
                (name, type_, token_encrypted, preset, manager_chat_id, now_iso()),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_bot(self, bot_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM bots WHERE id = ?", (bot_id,))

    def list_bots(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM bots ORDER BY created_at")

    def set_bot_field(self, bot_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE bots SET {cols} WHERE id = ?", (*fields.values(), bot_id))

    def delete_bot(self, bot_id: int) -> None:
        """Removes the bot's own config (triggers/actions/scenarios/
        templates/scenario sessions). Leads, contacts and activity history
        are kept — they're shared records of real conversations, not bot
        configuration, so they outlive the bot that created them."""
        with self._lock:
            trigger_ids = [r["id"] for r in self._conn.execute(
                "SELECT id FROM bot_triggers WHERE bot_id = ?", (bot_id,)).fetchall()]
            for tid in trigger_ids:
                self._conn.execute("DELETE FROM bot_actions WHERE trigger_id = ?", (tid,))
            self._conn.execute("DELETE FROM bot_triggers WHERE bot_id = ?", (bot_id,))
            self._conn.execute("DELETE FROM bot_scenarios WHERE bot_id = ?", (bot_id,))
            self._conn.execute("DELETE FROM bot_scenario_sessions WHERE bot_id = ?", (bot_id,))
            self._conn.execute("DELETE FROM bot_templates WHERE bot_id = ?", (bot_id,))
            self._conn.execute("DELETE FROM bots WHERE id = ?", (bot_id,))
            self._conn.commit()

    # ---- bot triggers / actions ------------------------------------------
    def add_trigger(self, bot_id: int, type_: str, config: dict, enabled: bool = True) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO bot_triggers(bot_id, type, config, enabled, created_at) VALUES (?, ?, ?, ?, ?)",
                (bot_id, type_, json.dumps(config, ensure_ascii=False), 1 if enabled else 0, now_iso()),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_trigger(self, trigger_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM bot_triggers WHERE id = ?", (trigger_id,))

    def list_triggers(self, bot_id: int, type_: str | None = None) -> list[sqlite3.Row]:
        if type_ is not None:
            return self.query(
                "SELECT * FROM bot_triggers WHERE bot_id = ? AND type = ? AND enabled = 1 ORDER BY id",
                (bot_id, type_),
            )
        return self.query("SELECT * FROM bot_triggers WHERE bot_id = ? ORDER BY id", (bot_id,))

    def set_trigger_field(self, trigger_id: int, **fields: Any) -> None:
        if not fields:
            return
        if "config" in fields and isinstance(fields["config"], dict):
            fields["config"] = json.dumps(fields["config"], ensure_ascii=False)
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE bot_triggers SET {cols} WHERE id = ?", (*fields.values(), trigger_id))

    def delete_trigger(self, trigger_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM bot_actions WHERE trigger_id = ?", (trigger_id,))
            self._conn.execute("DELETE FROM bot_triggers WHERE id = ?", (trigger_id,))
            self._conn.commit()

    def add_action(self, trigger_id: int, type_: str, config: dict, order_index: int = 0) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO bot_actions(trigger_id, type, config, order_index) VALUES (?, ?, ?, ?)",
                (trigger_id, type_, json.dumps(config, ensure_ascii=False), order_index),
            )
            self._conn.commit()
            return cur.lastrowid

    def list_actions(self, trigger_id: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM bot_actions WHERE trigger_id = ? ORDER BY order_index, id", (trigger_id,)
        )

    def set_action_field(self, action_id: int, **fields: Any) -> None:
        if not fields:
            return
        if "config" in fields and isinstance(fields["config"], dict):
            fields["config"] = json.dumps(fields["config"], ensure_ascii=False)
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE bot_actions SET {cols} WHERE id = ?", (*fields.values(), action_id))

    def delete_action(self, action_id: int) -> None:
        self.execute("DELETE FROM bot_actions WHERE id = ?", (action_id,))

    # ---- bot contacts / leads / activity ---------------------------------
    def upsert_contact(self, telegram_id: int, username: str | None = None,
                        source: str = "organic") -> int:
        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM bot_contacts WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
            now = now_iso()
            if existing:
                self._conn.execute(
                    "UPDATE bot_contacts SET username = COALESCE(?, username), last_active = ? WHERE id = ?",
                    (username, now, existing["id"]),
                )
                self._conn.commit()
                return existing["id"]
            cur = self._conn.execute(
                """INSERT INTO bot_contacts(telegram_id, username, first_seen, last_active, source)
                   VALUES (?, ?, ?, ?, ?)""",
                (telegram_id, username, now, now, source),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_contact(self, contact_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM bot_contacts WHERE id = ?", (contact_id,))

    def get_contact_by_telegram_id(self, telegram_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM bot_contacts WHERE telegram_id = ?", (telegram_id,))

    def list_contacts(self, limit: int = 500) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM bot_contacts ORDER BY last_active DESC LIMIT ?", (limit,))

    def set_contact_tags(self, contact_id: int, tags: list[str]) -> None:
        self.execute(
            "UPDATE bot_contacts SET tags = ? WHERE id = ?",
            (json.dumps(tags, ensure_ascii=False), contact_id),
        )

    def add_lead(self, contact_id: int, bot_id: int, content: dict, status: str = "new",
                 manager: str | None = None) -> int:
        with self._lock:
            now = now_iso()
            cur = self._conn.execute(
                """INSERT INTO bot_leads(contact_id, bot_id, status, manager, created_at, updated_at, content)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (contact_id, bot_id, status, manager, now, now, json.dumps(content, ensure_ascii=False)),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_lead(self, lead_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM bot_leads WHERE id = ?", (lead_id,))

    def list_leads(self, bot_id: int | None = None, status: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM bot_leads"
        clauses, params = [], []
        if bot_id is not None:
            clauses.append("bot_id = ?")
            params.append(bot_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC"
        return self.query(sql, params)

    def set_lead_field(self, lead_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields = dict(fields)
        if "content" in fields and isinstance(fields["content"], dict):
            fields["content"] = json.dumps(fields["content"], ensure_ascii=False)
        fields.setdefault("updated_at", now_iso())
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE bot_leads SET {cols} WHERE id = ?", (*fields.values(), lead_id))

    def log_activity(self, contact_id: int | None, bot_id: int | None, chat_id: int | None,
                      message_id: int | None, chat_type: str | None, kind: str = "message") -> None:
        self.execute(
            """INSERT INTO bot_activity_log(contact_id, bot_id, chat_id, message_id, timestamp, chat_type, kind)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (contact_id, bot_id, chat_id, message_id, now_iso(), chat_type, kind),
        )

    def activity_for_contact(self, contact_id: int, limit: int = 200) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM bot_activity_log WHERE contact_id = ? ORDER BY timestamp DESC LIMIT ?",
            (contact_id, limit),
        )

    def contact_ranking(self, limit: int = 50, half_life_days: float = 14.0) -> list[dict]:
        """Recency+frequency activity score per contact: each activity_log
        row contributes exp(-age_days / half_life_days) — a message from
        today counts close to 1, one from half_life_days ago counts 0.5,
        older ones fade further, so someone active a lot three months ago
        doesn't outrank someone active daily this week.

        Bounded to the last 10 half-lives — beyond that a row's own
        contribution is under 0.005, negligible to the score, so there's
        no reason to keep pulling the *entire* activity_log into Python on
        every refresh as it grows over months of use."""
        import math
        now = dt.datetime.now().astimezone()
        cutoff = (now - dt.timedelta(days=half_life_days * 10)).isoformat()
        rows = self.query(
            """SELECT contact_id, timestamp FROM bot_activity_log
               WHERE contact_id IS NOT NULL AND timestamp >= ? ORDER BY contact_id""",
            (cutoff,),
        )
        scores: dict[int, float] = {}
        counts: dict[int, int] = {}
        for r in rows:
            try:
                ts = dt.datetime.fromisoformat(r["timestamp"])
            except ValueError:
                continue
            age_days = max(0.0, (now - ts).total_seconds() / 86400)
            scores[r["contact_id"]] = scores.get(r["contact_id"], 0.0) + math.exp(-age_days / half_life_days)
            counts[r["contact_id"]] = counts.get(r["contact_id"], 0) + 1
        out = []
        for contact_id, score in scores.items():
            contact = self.get_contact(contact_id)
            if not contact:
                continue
            out.append({
                "contact_id": contact_id, "telegram_id": contact["telegram_id"],
                "username": contact["username"], "score": round(score, 3),
                "activity_count": counts[contact_id], "last_active": contact["last_active"],
            })
        out.sort(key=lambda r: r["score"], reverse=True)
        return out[:limit]

    def leads_funnel(self, bot_id: int | None = None) -> dict[str, int]:
        sql = "SELECT status, count(*) AS c FROM bot_leads"
        params: list[Any] = []
        if bot_id is not None:
            sql += " WHERE bot_id = ?"
            params.append(bot_id)
        sql += " GROUP BY status"
        counts = {r["status"]: r["c"] for r in self.query(sql, params)}
        return {"new": counts.get("new", 0), "in_progress": counts.get("in_progress", 0),
                "closed": counts.get("closed", 0)}

    # ---- bot templates ---------------------------------------------------
    def add_template(self, bot_id: int | None, name: str, text: str, variables: list[str]) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO bot_templates(bot_id, name, text, variables, created_at) VALUES (?, ?, ?, ?, ?)",
                (bot_id, name, text, json.dumps(variables, ensure_ascii=False), now_iso()),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_template(self, template_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM bot_templates WHERE id = ?", (template_id,))

    def list_templates(self, bot_id: int | None = None) -> list[sqlite3.Row]:
        if bot_id is not None:
            return self.query(
                "SELECT * FROM bot_templates WHERE bot_id = ? ORDER BY created_at", (bot_id,)
            )
        return self.query("SELECT * FROM bot_templates ORDER BY created_at")

    def update_template(self, template_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields = dict(fields)
        if "variables" in fields and isinstance(fields["variables"], list):
            fields["variables"] = json.dumps(fields["variables"], ensure_ascii=False)
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE bot_templates SET {cols} WHERE id = ?", (*fields.values(), template_id))

    def delete_template(self, template_id: int) -> None:
        self.execute("DELETE FROM bot_templates WHERE id = ?", (template_id,))

    # ---- bot scenarios -----------------------------------------------------
    def add_scenario(self, bot_id: int, name: str, steps: list[dict]) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO bot_scenarios(bot_id, name, steps, created_at) VALUES (?, ?, ?, ?)",
                (bot_id, name, json.dumps(steps, ensure_ascii=False), now_iso()),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_scenario(self, scenario_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM bot_scenarios WHERE id = ?", (scenario_id,))

    def list_scenarios(self, bot_id: int) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM bot_scenarios WHERE bot_id = ? ORDER BY created_at", (bot_id,))

    def update_scenario(self, scenario_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields = dict(fields)
        if "steps" in fields and isinstance(fields["steps"], list):
            fields["steps"] = json.dumps(fields["steps"], ensure_ascii=False)
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE bot_scenarios SET {cols} WHERE id = ?", (*fields.values(), scenario_id))

    def delete_scenario(self, scenario_id: int) -> None:
        self.execute("DELETE FROM bot_scenarios WHERE id = ?", (scenario_id,))

    # ---- bot scenario sessions (FSM state) --------------------------------
    def get_active_scenario_session(self, bot_id: int, contact_telegram_id: int) -> sqlite3.Row | None:
        return self.query_one(
            "SELECT * FROM bot_scenario_sessions WHERE bot_id = ? AND contact_telegram_id = ? AND status = 'active'",
            (bot_id, contact_telegram_id),
        )

    def start_scenario_session(self, bot_id: int, scenario_id: int, contact_telegram_id: int) -> int:
        with self._lock:
            now = now_iso()
            self._conn.execute(
                "UPDATE bot_scenario_sessions SET status = 'abandoned', updated_at = ? "
                "WHERE bot_id = ? AND contact_telegram_id = ? AND status = 'active'",
                (now, bot_id, contact_telegram_id),
            )
            cur = self._conn.execute(
                """INSERT INTO bot_scenario_sessions(bot_id, scenario_id, contact_telegram_id,
                       step_index, answers, status, started_at, updated_at)
                   VALUES (?, ?, ?, 0, '{}', 'active', ?, ?)""",
                (bot_id, scenario_id, contact_telegram_id, now, now),
            )
            self._conn.commit()
            return cur.lastrowid

    def update_scenario_session(self, session_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields = dict(fields)
        if "answers" in fields and isinstance(fields["answers"], dict):
            fields["answers"] = json.dumps(fields["answers"], ensure_ascii=False)
        fields.setdefault("updated_at", now_iso())
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE bot_scenario_sessions SET {cols} WHERE id = ?", (*fields.values(), session_id))


def _fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 MATCH query: quote each token as a
    phrase so punctuation in the source text can't break the query syntax,
    AND them together."""
    tokens = [t for t in text.strip().split() if t]
    escaped = [f'"{t.replace(chr(34), chr(34) * 2)}"' for t in tokens]
    return " AND ".join(escaped) if escaped else '""'


class _Transaction:
    def __init__(self, db: Database):
        self.db = db

    def __enter__(self):
        self.db._lock.acquire()
        return self.db._conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.db._conn.commit()
            else:
                self.db._conn.rollback()
        finally:
            self.db._lock.release()
        return False
