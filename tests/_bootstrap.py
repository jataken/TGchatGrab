"""Р7: the ~6 lines nearly every test file repeats — a wiped temp data
directory, Paths, and a fresh Database — as one function instead of a
copy per file. Plain functions, not a pytest fixture: the project
deliberately doesn't pull in pytest (invariant 8), and every test here
still runs as a standalone script, imported by nothing but itself.

Three entry points, not one, even though most callers only need the
second: fresh_paths exists because one test (log rotation) never touches
the database at all — routing it through fresh_db would open a Database
it never uses, a small but real behavior change from what that test did
before. fresh_env's AppConfig/SecurityService construction only matters
to the handful of files that exercise something master-password- or
integration-credential-related — building both unconditionally for every
other caller would just be two more unused names to explain away at each
of those call sites.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from chatgrab.config import AppConfig
from chatgrab.db.database import Database
from chatgrab.paths import Paths
from chatgrab.security import SecurityService


def fresh_paths(name: str) -> Paths:
    """A wiped temp data directory + Paths, nothing else — for the rare
    test that never opens the database."""
    base = Path(tempfile.gettempdir()) / name
    shutil.rmtree(base, ignore_errors=True)
    paths = Paths(base)
    paths.ensure()
    return paths


def fresh_db(name: str) -> tuple[Paths, Database]:
    """fresh_paths(), plus a fresh Database on top — the common case."""
    paths = fresh_paths(name)
    db = Database(paths.db_path)
    return paths, db


def fresh_env(name: str) -> tuple[Paths, Database, AppConfig, SecurityService]:
    """fresh_db(), plus the AppConfig/SecurityService pair the tests that
    touch master-password protection or an integration credential need."""
    paths, db = fresh_db(name)
    config = AppConfig.load(paths)
    security = SecurityService(config, paths)
    return paths, db, config, security
