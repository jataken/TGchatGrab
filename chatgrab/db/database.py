"""Thin, thread-safe SQLite access layer. One connection, one file, guarded
by a lock so it can be safely called both from the asyncio/Qt main thread
and from executor threads (bulk export, backups, VACUUM).

Р1/Р2 (see PLAN.md): the 165 methods that used to all live in this one
file are now split by domain into db/mixins/*.py, composed back together
below. `Database` still has exactly the same public surface it always
did — `db.method(...)` means the same thing it meant before, for every
caller in the app — this file just stopped being where every method body
was written. The low-level layer (connection, lock, execute/query/
transaction) stays here: every mixin depends on it, so it can't be a
mixin itself without creating an import cycle.
"""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from . import schema
from .mixins.accounts import AccountsMixin
from .mixins.bots import BotsMixin
from .mixins.chats import ChatsMixin
from .mixins.crm import CrmMixin
from .mixins.directions import DirectionsMixin
from .mixins.export import ExportMixin
from .mixins.funnels import FunnelsMixin
from .mixins.ignore_rules import IgnoreRulesMixin
from .mixins.leads import LeadsMixin
from .mixins.mail import MailMixin
from .mixins.maintenance import MaintenanceMixin
from .mixins.outbox import OutboxMixin
from .mixins.productivity import ProductivityMixin
from .mixins.reports import ReportsMixin
from .mixins.retention import RetentionMixin
from .mixins.scenario_sessions import ScenarioSessionsMixin
from .mixins.scenarios import ScenariosMixin
from .mixins.search import SearchMixin
from .mixins.settings_kv import SettingsMixin
from .mixins.templates import TemplatesMixin
from .mixins.watch import WatchMixin
from .timeutil import now_iso  # noqa: F401 — re-exported: see module docstring

_logger = logging.getLogger("chatgrab")


class Database(
    SettingsMixin, ChatsMixin, IgnoreRulesMixin, ExportMixin, SearchMixin,
    DirectionsMixin, AccountsMixin, WatchMixin, RetentionMixin, ProductivityMixin,
    MaintenanceMixin, BotsMixin, LeadsMixin, ReportsMixin, OutboxMixin, CrmMixin,
    TemplatesMixin, ScenariosMixin, ScenarioSessionsMixin, MailMixin, FunnelsMixin,
):
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
