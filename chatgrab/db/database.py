"""Thin, thread-safe SQLite access layer. One connection, one file, guarded
by a lock so it can be safely called both from the asyncio/Qt main thread
and from executor threads (bulk export, backups, VACUUM)."""
from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from . import schema
from .dedup import fingerprint
from ..core import lead as lead_domain

_logger = logging.getLogger("chatgrab")


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
        schema.migrate(self._conn, on_backup=self._backup_before_migration)

    def _backup_before_migration(self, conn: sqlite3.Connection, migration_id: str) -> None:
        """Copy the database before schema.migrate() changes anything.

        Uses the same online-backup API as backup_to() below, on the raw
        connection migrate() hands back — not through BackupService,
        which needs an already-migrated Database and would be circular
        this early. Runs once, right before the first migration a given
        database hasn't seen yet; a fresh install has nothing to protect
        (see migrations._has_existing_schema), so it never fires there.
        """
        backup_dir = self.path.parent / "backups" / "pre_migration"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        dest_path = backup_dir / f"chatgrab_before_{migration_id}_{stamp}.db"
        dest = sqlite3.connect(str(dest_path))
        try:
            conn.backup(dest)
        finally:
            dest.close()
        _logger.info("резервная копия перед миграцией %s: %s -> %s",
                     migration_id, self.path, dest_path)

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
        # Recomputed on edit too: an edited message is a different text and
        # so a different repeat-group than the one it was posted as.
        m["text_hash"] = fingerprint(m.get("text") or "")
        cols = ["chat_id", "message_id", "chat_title", "date", "edited_date",
                "sender_id", "sender_username", "sender_display_name", "text",
                "reply_to_message_id", "forwarded_from", "media_type",
                "media_caption", "media_path", "views", "link", "char_len",
                "is_reply", "is_forward", "is_hidden", "text_hash"]
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

    def repeat_summary(self, chat_ids: list[int] | None = None) -> dict[str, int]:
        """How many stored messages are reposts of text already collected —
        i.e. how much «только уникальные» would leave out."""
        scope, params = "", []
        if chat_ids:
            scope = " AND chat_id IN (" + ",".join("?" for _ in chat_ids) + ")"
            params = list(chat_ids)
        row = self.query_one(
            f"""
            SELECT count(*) AS groups, coalesce(sum(n - 1), 0) AS repeats FROM (
                SELECT count(*) AS n FROM messages
                WHERE text_hash IS NOT NULL{scope}
                GROUP BY chat_id, text_hash HAVING n > 1)
            """,
            params,
        )
        return {
            "repeats": row["repeats"] if row else 0,
            "groups": row["groups"] if row else 0,
        }

    def gap_summary(self, chat_id: int) -> dict[str, int]:
        """How many holes there are in the collected id sequence, and how
        many messages they add up to.

        Computed in SQL rather than by pulling every id into Python the way
        find_gaps() does, so this can run on every screen refresh for a chat
        with tens of thousands of messages.

        A gap is not proof of loss: Telegram's per-chat ids also cover
        service messages and anything since deleted, and those holes are
        permanent. It marks a chat worth re-checking, not a defect."""
        row = self.query_one(
            """
            SELECT count(*) AS gaps, coalesce(sum(diff - 1), 0) AS missing FROM (
                SELECT message_id - lag(message_id) OVER (ORDER BY message_id) AS diff
                FROM messages WHERE chat_id = ?
            ) WHERE diff > 1
            """,
            (chat_id,),
        )
        return {"gaps": row["gaps"] if row else 0, "missing": row["missing"] if row else 0}

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
                       min_id_by_chat: dict[int, int] | None = None,
                       unique_only: bool = False,
                       _columns: str = "m.*") -> list[sqlite3.Row]:
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
        if unique_only:
            # Keep the first posting of each repeated text within its chat
            # and drop the later reposts. Messages without a fingerprint
            # (short ones, media-only) always pass — see db/dedup.py for
            # why those are not treated as repeats.
            clauses.append(
                "(m.text_hash IS NULL OR m.message_id = ("
                "  SELECT min(e.message_id) FROM messages e"
                "  WHERE e.chat_id = m.chat_id AND e.text_hash = m.text_hash))"
            )
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        # Pure chronological order — not grouped by chat first — so a
        # merged multi-chat export reads as one continuous timeline
        # (old to new) instead of one chat's whole history, then the
        # next chat's from its own beginning. Per-chat file splitting
        # groups these afterward without disturbing the relative order,
        # so single-chat output stays date-sorted too.
        return self.query(
            f"SELECT {_columns} {from_sql}{where} ORDER BY m.date ASC", params
        )

    def export_select_meta(self, **kwargs) -> list[sqlite3.Row]:
        """The same selection as export_select, but only the columns needed
        to plan an export: which chat, when, how long, and whether media is
        attached.

        The estimate on the export screen re-runs on every toggle, and
        pulling full message text each time made that cost grow with the
        size of the archive. `char_len` is stored per row precisely so the
        token estimate never has to read the text itself."""
        return self.export_select(_columns="m.chat_id, m.date, m.char_len, m.media_path", **kwargs)

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

    # ---- scheduled exports ---------------------------------------------
    def add_export_schedule(self, preset_name: str, every_hours: int, at_hour: int) -> int:
        cur = self.execute(
            "INSERT INTO export_schedule(preset_name, every_hours, at_hour, enabled, created_at) "
            "VALUES (?, ?, ?, 1, ?)",
            (preset_name, every_hours, at_hour, now_iso()),
        )
        return cur.lastrowid

    def list_export_schedules(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM export_schedule ORDER BY created_at")

    def set_export_schedule(self, schedule_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE export_schedule SET {cols} WHERE id = ?",
                     (*fields.values(), schedule_id))

    def delete_export_schedule(self, schedule_id: int) -> None:
        self.execute("DELETE FROM export_schedule WHERE id = ?", (schedule_id,))

    def delete_preset(self, name: str) -> None:
        self.execute("DELETE FROM export_preset WHERE name = ?", (name,))

    # ---- stats cache -------------------------------------------------
    # ---- saved searches ----------------------------------------------
    def save_search_preset(self, name: str, params: dict[str, Any]) -> None:
        self.execute(
            "INSERT INTO search_preset(name, params, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET params = excluded.params",
            (name, json.dumps(params, ensure_ascii=False), now_iso()),
        )

    def list_search_presets(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM search_preset ORDER BY name")

    def delete_search_preset(self, name: str) -> None:
        self.execute("DELETE FROM search_preset WHERE name = ?", (name,))

    # ---- directions -----------------------------------------------------
    # A flat catalogue — see db/schema.py's _DDL_DIRECTION comment for why
    # this deliberately isn't a line-item hierarchy.
    def add_direction(self, name: str, keywords: list[str] | None = None,
                      stop_words: list[str] | None = None, price_file: str | None = None,
                      note: str | None = None) -> int:
        order_index = (self.query_one(
            "SELECT COALESCE(MAX(order_index), -1) + 1 AS n FROM direction")["n"])
        cur = self.execute(
            "INSERT INTO direction(name, keywords, stop_words, price_file, note, "
            "enabled, order_index, created_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
            (name, json.dumps(keywords or [], ensure_ascii=False),
             json.dumps(stop_words or [], ensure_ascii=False), price_file, note,
             order_index, now_iso()),
        )
        return cur.lastrowid

    def list_directions(self, enabled_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM direction"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY order_index, id"
        return self.query(sql)

    def get_direction(self, direction_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM direction WHERE id = ?", (direction_id,))

    def update_direction(self, direction_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields = dict(fields)
        for list_field in ("keywords", "stop_words"):
            if list_field in fields and isinstance(fields[list_field], list):
                fields[list_field] = json.dumps(fields[list_field], ensure_ascii=False)
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE direction SET {cols} WHERE id = ?",
                     (*fields.values(), direction_id))

    def delete_direction(self, direction_id: int) -> None:
        self.execute("DELETE FROM direction WHERE id = ?", (direction_id,))

    def reorder_directions(self, ordered_ids: list[int]) -> None:
        """Rewrite order_index to match the given sequence — used by the
        ↑/↓ buttons on the screen, one full rewrite rather than a swap so
        it can't drift out of sync with what's actually on screen."""
        with self._lock:
            for index, direction_id in enumerate(ordered_ids):
                self._conn.execute(
                    "UPDATE direction SET order_index = ? WHERE id = ?", (index, direction_id))
            self._conn.commit()

    def export_directions(self) -> dict:
        """Plain JSON-able dict — id is left out on purpose: importing this
        elsewhere (or back into a rebuilt database) should create fresh
        rows, not collide with whatever ids happen to already exist."""
        directions = []
        for row in self.list_directions():
            directions.append({
                "name": row["name"],
                "keywords": json.loads(row["keywords"]),
                "stop_words": json.loads(row["stop_words"]),
                "price_file": row["price_file"],
                "note": row["note"],
                "enabled": bool(row["enabled"]),
            })
        return {"directions": directions}

    def import_directions(self, data: dict, replace: bool = False) -> int:
        """Load a previously exported catalogue. Malformed entries are
        skipped rather than aborting the whole import — one bad row in a
        hand-edited file shouldn't cost the rest. Returns how many were
        actually added.

        replace=True clears the existing catalogue first — the "start
        over from this file" path; the default just appends, for merging
        two machines' lists or restoring after an accidental delete.
        """
        directions = data.get("directions") if isinstance(data, dict) else None
        if not isinstance(directions, list):
            return 0
        if replace:
            self.execute("DELETE FROM direction")
        added = 0
        for entry in directions:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            keywords = entry.get("keywords")
            stop_words = entry.get("stop_words")
            self.add_direction(
                name.strip(),
                keywords=keywords if isinstance(keywords, list) else [],
                stop_words=stop_words if isinstance(stop_words, list) else [],
                price_file=entry.get("price_file") if isinstance(entry.get("price_file"), str) else None,
                note=entry.get("note") if isinstance(entry.get("note"), str) else None,
            )
            added += 1
        return added

    # ---- Telegram accounts ---------------------------------------------
    def add_account(self, name: str, session_file: str, phone: str | None = None,
                    make_default: bool = False) -> int:
        cur = self.execute(
            "INSERT INTO account(name, phone, session_file, enabled, is_default, created_at) "
            "VALUES (?, ?, ?, 1, 0, ?)",
            (name, phone, session_file, now_iso()),
        )
        account_id = cur.lastrowid
        if make_default or not self.query_one("SELECT count(*) AS c FROM account WHERE is_default = 1")["c"]:
            self.set_default_account(account_id)
        return account_id

    def list_accounts(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM account ORDER BY is_default DESC, id")

    def get_account(self, account_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM account WHERE id = ?", (account_id,))

    def default_account(self) -> sqlite3.Row | None:
        """The flagged one if there is one, otherwise the oldest — never
        None while any account exists, so callers have one branch fewer."""
        return self.query_one(
            "SELECT * FROM account ORDER BY is_default DESC, id LIMIT 1")

    def set_default_account(self, account_id: int) -> None:
        # One flag, flipped in a single statement pair — a half-applied
        # change here would leave the app with no account to fall back on.
        self.execute("UPDATE account SET is_default = 0")
        self.execute("UPDATE account SET is_default = 1 WHERE id = ?", (account_id,))

    def set_account_field(self, account_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE account SET {cols} WHERE id = ?",
                     (*fields.values(), account_id))

    def delete_account(self, account_id: int) -> None:
        """Chats and bots that pointed at it fall back to «основной»
        rather than becoming unreachable rows nothing can collect."""
        self.execute("UPDATE chats SET account_id = NULL WHERE account_id = ?", (account_id,))
        self.execute("UPDATE bots SET account_id = NULL WHERE account_id = ?", (account_id,))
        was_default = bool((self.get_account(account_id) or {"is_default": 0})["is_default"])
        self.execute("DELETE FROM account WHERE id = ?", (account_id,))
        if was_default:
            row = self.query_one("SELECT id FROM account ORDER BY id LIMIT 1")
            if row:
                self.set_default_account(row["id"])

    def account_usage(self, account_id: int) -> dict:
        chats = self.query_one(
            "SELECT count(*) AS c FROM chats WHERE account_id = ?", (account_id,))["c"]
        bots = self.query_one(
            "SELECT count(*) AS c FROM bots WHERE account_id = ?", (account_id,))["c"]
        return {"chats": chats, "bots": bots}

    # ---- watch list ---------------------------------------------------
    def add_watch_rule(self, phrase: str, chat_id: int | None = None, notify: bool = True) -> int:
        cur = self.execute(
            "INSERT INTO watch_rule(phrase, chat_id, enabled, notify, created_at) "
            "VALUES (?, ?, 1, ?, ?)",
            (phrase.strip(), chat_id, 1 if notify else 0, now_iso()),
        )
        return cur.lastrowid

    def list_watch_rules(self, enabled_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM watch_rule"
        if enabled_only:
            sql += " WHERE enabled = 1"
        return self.query(sql + " ORDER BY created_at")

    def set_watch_rule(self, rule_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE watch_rule SET {cols} WHERE id = ?", (*fields.values(), rule_id))

    def delete_watch_rule(self, rule_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM watch_hit WHERE rule_id = ?", (rule_id,))
            self._conn.execute("DELETE FROM watch_rule WHERE id = ?", (rule_id,))
            self._conn.commit()

    def add_watch_hit(self, rule_id: int, chat_id: int, message_id: int) -> bool:
        """Records a match. Returns False when this message already matched
        this rule — re-scanning history must not resurrect old alerts."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO watch_hit(rule_id, chat_id, message_id, matched_at) "
                "VALUES (?, ?, ?, ?)",
                (rule_id, chat_id, message_id, now_iso()),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def list_watch_hits(self, unseen_only: bool = False, limit: int = 200) -> list[sqlite3.Row]:
        sql = ("SELECT h.*, r.phrase, m.text, m.date, m.sender_id, m.sender_display_name, "
               "       m.sender_username, m.link, m.media_path, c.title AS chat_title "
               "FROM watch_hit h "
               "JOIN watch_rule r ON r.id = h.rule_id "
               "LEFT JOIN messages m ON m.chat_id = h.chat_id AND m.message_id = h.message_id "
               "LEFT JOIN chats c ON c.chat_id = h.chat_id")
        if unseen_only:
            sql += " WHERE h.seen = 0"
        return self.query(sql + " ORDER BY h.matched_at DESC LIMIT ?", (limit,))

    def unseen_watch_count(self) -> int:
        row = self.query_one("SELECT count(*) AS c FROM watch_hit WHERE seen = 0")
        return row["c"] if row else 0

    def mark_watch_hits_seen(self, hit_ids: list[int] | None = None) -> None:
        if hit_ids:
            placeholders = ",".join("?" for _ in hit_ids)
            self.execute(f"UPDATE watch_hit SET seen = 1 WHERE id IN ({placeholders})", hit_ids)
        else:
            self.execute("UPDATE watch_hit SET seen = 1 WHERE seen = 0")

    # ---- retention -----------------------------------------------------
    def chat_storage(self) -> list[dict]:
        """Per-chat volume: how many messages, how far back they go, and how
        many carry a downloaded file. Feeds both the retention screen and
        the source-productivity view."""
        rows = self.query(
            """
            SELECT c.chat_id, c.title,
                   count(m.id) AS messages,
                   sum(CASE WHEN m.media_path IS NOT NULL AND m.media_path != '' THEN 1 ELSE 0 END) AS media,
                   min(m.date) AS oldest, max(m.date) AS newest,
                   coalesce(sum(m.char_len), 0) AS chars
            FROM chats c LEFT JOIN messages m ON m.chat_id = c.chat_id
            GROUP BY c.chat_id, c.title ORDER BY messages DESC
            """
        )
        return [dict(r) for r in rows]

    def messages_older_than(self, cutoff_iso: str, chat_id: int | None = None) -> int:
        sql = "SELECT count(*) AS c FROM messages WHERE date < ?"
        params: list[Any] = [cutoff_iso]
        if chat_id is not None:
            sql += " AND chat_id = ?"
            params.append(chat_id)
        return self.query_one(sql, params)["c"]

    def select_older_than(self, cutoff_iso: str) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM messages WHERE date < ? ORDER BY date", (cutoff_iso,))

    def delete_older_than(self, cutoff_iso: str) -> int:
        """Deletes and reports how many rows went. FTS and watch hits are
        cleaned alongside so nothing points at a message that is gone."""
        with self._lock:
            cur = self._conn.execute("SELECT count(*) FROM messages WHERE date < ?", (cutoff_iso,))
            n = cur.fetchone()[0]
            self._conn.execute(
                "DELETE FROM watch_hit WHERE (chat_id, message_id) IN "
                "(SELECT chat_id, message_id FROM messages WHERE date < ?)",
                (cutoff_iso,),
            )
            self._conn.execute("DELETE FROM messages WHERE date < ?", (cutoff_iso,))
            self._conn.commit()
            return n

    # ---- source productivity -------------------------------------------
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

    def add_lead(self, contact_id: int | None, bot_id: int | None, content: dict, status: str = "new",
                 manager: str | None = None, *, tg_user_id: int | None = None,
                 username: str | None = None, display_name: str | None = None,
                 phone: str | None = None, email: str | None = None,
                 source_chat_id: int | None = None,
                 source_type: str = lead_domain.SOURCE_TYPE_BOT,
                 direction_id: int | None = None, product: str | None = None,
                 volume: str | None = None, unit: str | None = None,
                 deadline: str | None = None, city: str | None = None,
                 delivery: str | None = None,
                 event_source: str = lead_domain.EVENT_SOURCE_RULE) -> int:
        """Creates a lead and its opening lead_events row in one go — a
        lead with no history is exactly the "silent database row" the
        card (see ui/screens/bots/lead_card.py) exists to stop being.

        Signature grew rather than gaining a second create_lead(): every
        existing caller (rules_engine's save_lead/run_scenario actions)
        still passes just contact_id/bot_id/content/status, and every new
        field here is optional with a sensible default — a bot-sourced
        lead just doesn't set the ones a human fills in on the card later.

        contact_id/bot_id are None for a lead that never touched a bot —
        created by hand, or from a plain collected message or watch hit
        (С3). Migration 009 made both columns nullable for exactly this.
        """
        with self._lock:
            now = now_iso()
            cur = self._conn.execute(
                """INSERT INTO bot_leads(
                       contact_id, bot_id, status, manager, created_at, updated_at, content,
                       tg_user_id, username, display_name, phone, email,
                       source_chat_id, source_type, direction_id, product, volume, unit,
                       deadline, city, delivery, owner
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (contact_id, bot_id, status, manager, now, now, json.dumps(content, ensure_ascii=False),
                 tg_user_id, username, display_name, phone, email,
                 source_chat_id, source_type, direction_id, product, volume, unit,
                 deadline, city, delivery, lead_domain.DEFAULT_OWNER),
            )
            lead_id = cur.lastrowid
            self._conn.execute(
                "INSERT INTO lead_events(lead_id, kind, source, created_at) "
                "VALUES (?, ?, ?, ?)",
                (lead_id, lead_domain.EVENT_KIND_CREATED, event_source, now),
            )
            self._conn.commit()
            return lead_id

    def get_lead(self, lead_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM bot_leads WHERE id = ?", (lead_id,))

    def list_leads(self, bot_id: int | None = None, status: str | None = None, *,
                    direction_id: int | None = None, source_type: str | None = None,
                    since: str | None = None, until: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM bot_leads"
        clauses, params = [], []
        if bot_id is not None:
            clauses.append("bot_id = ?")
            params.append(bot_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if direction_id is not None:
            clauses.append("direction_id = ?")
            params.append(direction_id)
        if source_type is not None:
            clauses.append("source_type = ?")
            params.append(source_type)
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("created_at <= ?")
            params.append(until)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        # id вторым ключом: время пишется с точностью до секунды, и две
        # заявки, пришедшие в одну секунду, иначе меняются местами между
        # обновлениями списка.
        sql += " ORDER BY created_at DESC, id DESC"
        return self.query(sql, params)

    def leads_status_counts(self, bot_id: int | None = None) -> dict[str, int]:
        """Per-status totals for the funnel row on the leads screen —
        pre-seeded with 0 for every status so the UI never has to guard
        against a missing key."""
        sql = "SELECT status, count(*) AS c FROM bot_leads"
        params: list[Any] = []
        if bot_id is not None:
            sql += " WHERE bot_id = ?"
            params.append(bot_id)
        sql += " GROUP BY status"
        counts = {r["status"]: r["c"] for r in self.query(sql, params)}
        return {s: counts.get(s, 0) for s in lead_domain.ALL_STATUSES}

    def due_lead_reminders(self, now_iso_str: str) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM bot_leads WHERE next_action_at IS NOT NULL AND next_action_at <= ? "
            "ORDER BY next_action_at",
            (now_iso_str,),
        )

    def fire_lead_reminder(self, lead_id: int) -> None:
        """Records the reminder in the lead's history and clears the
        field that made it due — the clearing itself is what keeps a
        reminder from firing twice, including across a restart, since
        due_lead_reminders only ever looks at that same field."""
        lead = self.get_lead(lead_id)
        if lead is None:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE bot_leads SET next_action_at = NULL, next_action_text = NULL, "
                "updated_at = ? WHERE id = ?",
                (now_iso(), lead_id),
            )
            self._conn.execute(
                "INSERT INTO lead_events(lead_id, kind, text, source, created_at) VALUES (?, ?, ?, ?, ?)",
                (lead_id, lead_domain.EVENT_KIND_REMINDER, lead["next_action_text"],
                 lead_domain.EVENT_SOURCE_RULE, now_iso()),
            )
            self._conn.commit()

    def set_lead_field(self, lead_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields = dict(fields)
        if "content" in fields and isinstance(fields["content"], dict):
            fields["content"] = json.dumps(fields["content"], ensure_ascii=False)
        if "attachments" in fields and isinstance(fields["attachments"], list):
            fields["attachments"] = json.dumps(fields["attachments"], ensure_ascii=False)
        fields.setdefault("updated_at", now_iso())
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE bot_leads SET {cols} WHERE id = ?", (*fields.values(), lead_id))

    def set_lead_status(self, lead_id: int, new_status: str, *, reject_reason: str | None = None,
                        source: str = lead_domain.EVENT_SOURCE_MANUAL, text: str | None = None) -> None:
        """The one validated path for changing a lead's stage — enforces
        core.lead's single hard rule (LOST needs a reason) and always logs
        the move, which is what makes set_lead_field unsuitable for this
        one field: a status written through it would change the funnel
        with no trace in the history the card shows.

        Raises ValueError on an invalid move rather than silently
        refusing, so a caller (the card, a future scenario action) finds
        out immediately instead of the change quietly not happening.
        """
        error = lead_domain.validate_transition(new_status, reject_reason)
        if error:
            raise ValueError(error)
        lead = self.get_lead(lead_id)
        if lead is None:
            raise ValueError(f"Заявка {lead_id} не найдена.")
        old_status = lead["status"]
        with self._lock:
            fields = {"status": new_status, "updated_at": now_iso()}
            if new_status == lead_domain.LOST:
                fields["reject_reason"] = reject_reason.strip()
            cols = ", ".join(f"{k} = ?" for k in fields)
            self._conn.execute(f"UPDATE bot_leads SET {cols} WHERE id = ?", (*fields.values(), lead_id))
            self._conn.execute(
                "INSERT INTO lead_events(lead_id, kind, from_status, to_status, text, source, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (lead_id, lead_domain.EVENT_KIND_STATUS, old_status, new_status, text, source, now_iso()),
            )
            self._conn.commit()

    def add_lead_note(self, lead_id: int, text: str,
                      source: str = lead_domain.EVENT_SOURCE_MANUAL) -> None:
        text = text.strip()
        if not text:
            return
        self.execute(
            "INSERT INTO lead_events(lead_id, kind, text, source, created_at) VALUES (?, ?, ?, ?, ?)",
            (lead_id, lead_domain.EVENT_KIND_NOTE, text, source, now_iso()),
        )

    def list_lead_events(self, lead_id: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM lead_events WHERE lead_id = ? ORDER BY created_at, id", (lead_id,))

    def lead_correspondence(self, telegram_id: int, limit: int = 200) -> list[sqlite3.Row]:
        """What this contact has actually said, across every tracked
        chat — the "переписка" on the card. A plain filter on sender_id,
        not an FTS query: there's no search phrase here, just "everything
        from this person," and messages already carries that column."""
        return self.query(
            "SELECT * FROM messages WHERE sender_id = ? AND is_hidden = 0 "
            "ORDER BY date DESC LIMIT ?",
            (telegram_id, limit),
        )

    def log_activity(self, contact_id: int | None, bot_id: int | None, chat_id: int | None,
                      message_id: int | None, chat_type: str | None, kind: str = "message") -> None:
        self.execute(
            """INSERT INTO bot_activity_log(contact_id, bot_id, chat_id, message_id, timestamp, chat_type, kind)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (contact_id, bot_id, chat_id, message_id, now_iso(), chat_type, kind),
        )

    def contacts_silent_since(self, bot_id: int, cutoff_iso: str) -> list[sqlite3.Row]:
        """Contacts this bot has actually interacted with whose last
        activity predates the cutoff — the candidate set for an
        inactivity trigger. Scoped by bot so one bot's reminders don't
        reach into another bot's audience."""
        return self.query(
            """
            SELECT c.* FROM bot_contacts c
            WHERE c.last_active < ?
              AND EXISTS (SELECT 1 FROM bot_activity_log a
                          WHERE a.contact_id = c.id AND a.bot_id = ?)
            ORDER BY c.last_active
            """,
            (cutoff_iso, bot_id),
        )

    def has_activity_since(self, contact_id: int, kind: str, since_iso: str) -> bool:
        row = self.query_one(
            "SELECT 1 FROM bot_activity_log WHERE contact_id = ? AND kind = ? "
            "AND timestamp >= ? LIMIT 1",
            (contact_id, kind, since_iso),
        )
        return row is not None

    def has_trigger_activity_since(self, bot_id: int, kind: str, since_iso: str) -> bool:
        row = self.query_one(
            "SELECT 1 FROM bot_activity_log WHERE bot_id = ? AND kind = ? "
            "AND timestamp >= ? LIMIT 1",
            (bot_id, kind, since_iso),
        )
        return row is not None

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
        """Three coarse buckets for analytics_tab.py's summary row — the
        funnel's own five-stage detail (С8) doesn't fit that layout, so
        qualified/quote_sent/negotiation collapse into "в работе" and
        won/lost collapse into "закрыты", same grouping analytics_tab and
        today.py already use elsewhere."""
        sql = "SELECT status, count(*) AS c FROM bot_leads"
        params: list[Any] = []
        if bot_id is not None:
            sql += " WHERE bot_id = ?"
            params.append(bot_id)
        sql += " GROUP BY status"
        counts = {r["status"]: r["c"] for r in self.query(sql, params)}
        in_progress = sum(counts.get(s, 0) for s in
                           (lead_domain.QUALIFIED, lead_domain.QUOTE_SENT, lead_domain.NEGOTIATION))
        closed = sum(counts.get(s, 0) for s in (lead_domain.WON, lead_domain.LOST))
        return {"new": counts.get(lead_domain.NEW, 0), "in_progress": in_progress, "closed": closed}

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

    def template_usage(self, template_id: int) -> dict[str, int]:
        """What would break if this template were deleted: actions that
        send it, and scenarios that use it as their closing message.
        Referenced from JSON config, so there is no foreign key to lean on
        — this is the only way to warn before the fact instead of showing
        a broken reference afterwards."""
        actions = self.query(
            "SELECT id FROM bot_actions WHERE json_extract(config, '$.template_id') = ?",
            (template_id,),
        )
        scenarios = self.query(
            "SELECT id FROM bot_scenarios WHERE done_template_id = ?", (template_id,)
        )
        return {"actions": len(actions), "scenarios": len(scenarios)}

    def scenario_usage(self, scenario_id: int) -> dict[str, int]:
        """Actions that launch this scenario, plus how many contacts are
        part-way through it right now — losing those mid-dialog is the
        part a user is least likely to expect."""
        actions = self.query(
            "SELECT id FROM bot_actions WHERE json_extract(config, '$.scenario_id') = ?",
            (scenario_id,),
        )
        active = self.query(
            "SELECT id FROM bot_scenario_sessions WHERE scenario_id = ? AND status = 'active'",
            (scenario_id,),
        )
        return {"actions": len(actions), "active_dialogs": len(active)}

    def delete_template(self, template_id: int) -> None:
        self.execute("DELETE FROM bot_templates WHERE id = ?", (template_id,))

    # ---- bot scenarios -----------------------------------------------------
    def add_scenario(self, bot_id: int, name: str, steps: list[dict]) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO bot_scenarios(bot_id, name, steps, created_at) VALUES (?, ?, ?, ?)",
                (bot_id, name, json.dumps(_with_step_ids(steps), ensure_ascii=False), now_iso()),
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
            fields["steps"] = json.dumps(_with_step_ids(fields["steps"]), ensure_ascii=False)
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE bot_scenarios SET {cols} WHERE id = ?", (*fields.values(), scenario_id))

    def delete_scenario(self, scenario_id: int) -> None:
        self.execute("DELETE FROM bot_scenarios WHERE id = ?", (scenario_id,))

    # ---- bot scenario sessions (FSM state) --------------------------------
    def last_finished_session(self, bot_id: int, contact_telegram_id: int) -> sqlite3.Row | None:
        """The most recently completed run for this contact — used to find
        which scenario they just finished, since the active session row is
        already marked done by the time the confirmation is sent."""
        return self.query_one(
            "SELECT * FROM bot_scenario_sessions WHERE bot_id = ? AND contact_telegram_id = ? "
            "AND status = 'done' ORDER BY updated_at DESC, id DESC LIMIT 1",
            (bot_id, contact_telegram_id),
        )

    def scenario_funnel(self, scenario_id: int) -> list[dict]:
        """How far contacts got through a scenario: for each step, how many
        runs reached it and how many stopped there.

        Reads the accumulated session history — which only became possible
        once schema v3 stopped collapsing every contact to one row per
        status. `step_index` is where a run stopped, so a run that reached
        step N passed through every step before it."""
        scenario = self.get_scenario(scenario_id)
        if not scenario:
            return []
        steps = json.loads(scenario["steps"])
        if not steps:
            return []
        rows = self.query(
            "SELECT step_index, status FROM bot_scenario_sessions WHERE scenario_id = ?",
            (scenario_id,),
        )
        total = len(rows)
        funnel = []
        for i, step in enumerate(steps):
            reached = sum(1 for r in rows if r["step_index"] >= i or r["status"] == "done")
            dropped = sum(
                1 for r in rows
                if r["status"] in ("abandoned", "active") and r["step_index"] == i
            )
            funnel.append({
                "index": i,
                "question": step.get("question", ""),
                "field": step.get("field", ""),
                "reached": reached,
                "dropped": dropped,
                "share": (reached / total) if total else 0.0,
            })
        return funnel

    def get_active_scenario_session(self, bot_id: int, contact_telegram_id: int) -> sqlite3.Row | None:
        return self.query_one(
            "SELECT * FROM bot_scenario_sessions WHERE bot_id = ? AND contact_telegram_id = ? AND status = 'active'",
            (bot_id, contact_telegram_id),
        )

    def start_scenario_session(self, bot_id: int, scenario_id: int, contact_telegram_id: int,
                                step_id: str | None = None) -> int:
        with self._lock:
            now = now_iso()
            self._conn.execute(
                "UPDATE bot_scenario_sessions SET status = 'abandoned', updated_at = ? "
                "WHERE bot_id = ? AND contact_telegram_id = ? AND status = 'active'",
                (now, bot_id, contact_telegram_id),
            )
            cur = self._conn.execute(
                """INSERT INTO bot_scenario_sessions(bot_id, scenario_id, contact_telegram_id,
                       step_index, step_id, answers, status, started_at, updated_at)
                   VALUES (?, ?, ?, 0, ?, '{}', 'active', ?, ?)""",
                (bot_id, scenario_id, contact_telegram_id, step_id, now, now),
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


def _with_step_ids(steps: list[dict]) -> list[dict]:
    """Give every scenario step a stable `id`, assigned once and preserved
    across edits.

    The engine walks steps by position and doesn't read this yet. It exists
    so that when branching lands, a jump can name its destination by id
    instead of by index — otherwise inserting a step in the middle of a
    live scenario would silently repoint every existing branch, and fixing
    that later would mean migrating real customer data. Cheap now,
    expensive to retrofit.
    """
    used = {s["id"] for s in steps if isinstance(s, dict) and s.get("id")}
    out = []
    for step in steps:
        step = dict(step)
        if not step.get("id"):
            n = len(used) + 1
            while f"s{n}" in used:
                n += 1
            step["id"] = f"s{n}"
            used.add(step["id"])
        out.append(step)
    return out


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
