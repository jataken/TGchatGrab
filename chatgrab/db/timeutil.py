"""Р1: now_iso() used to live at the top of database.py, but every mixin
under db/mixins/ needs it too, and mixins are imported *by* database.py to
build the Database class — importing it back from there would be
circular. One leaf module both sides can depend on instead.

database.py still re-exports this under the same name (`from .timeutil
import now_iso`), so every existing `from ..db.database import Database,
now_iso` elsewhere in the app keeps working unchanged.
"""
from __future__ import annotations

import datetime as dt


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")
