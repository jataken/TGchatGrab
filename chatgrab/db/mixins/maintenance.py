"""Housekeeping on the database file itself — VACUUM, an online backup
copy, WAL checkpointing, and closing the connection."""
from __future__ import annotations

import sqlite3
from pathlib import Path


class MaintenanceMixin:
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
