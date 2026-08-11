"""Scheduled database backups (keep N most recent copies), VACUUM, and
small filesystem helpers for the Настройки screen."""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

from ..db.database import Database
from ..paths import Paths

DEFAULT_BACKUP_SETTINGS = {"enabled": True, "interval_hours": 24, "keep": 5}


class BackupService:
    def __init__(self, db: Database, paths: Paths):
        self.db = db
        self.paths = paths

    def settings(self) -> dict:
        return self.db.get_setting("backup", DEFAULT_BACKUP_SETTINGS)

    def save_settings(self, enabled: bool, interval_hours: int, keep: int) -> None:
        self.db.set_setting("backup", {"enabled": enabled, "interval_hours": interval_hours, "keep": keep})

    def backup_dir(self) -> Path:
        d = Path(self.db.get_setting("backup_dir", str(self.paths.backups_dir)))
        d.mkdir(parents=True, exist_ok=True)
        return d

    def run_backup_now(self) -> Path:
        d = self.backup_dir()
        stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        dest = d / f"chatgrab_{stamp}.db"
        self.db.backup_to(dest)
        self._trim_old(d)
        return dest

    def _trim_old(self, d: Path) -> None:
        keep = self.settings().get("keep", 5)
        backups = sorted(d.glob("chatgrab_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in backups[keep:]:
            try:
                old.unlink()
            except OSError:
                pass

    def vacuum(self) -> tuple[int, int]:
        before = self.db.file_size()
        self.db.vacuum()
        after = self.db.file_size()
        return before, after

    async def run_periodic(self) -> None:
        while True:
            settings = self.settings()
            interval = max(1, int(settings.get("interval_hours", 24))) * 3600
            await asyncio.sleep(interval)
            if self.settings().get("enabled", True):
                try:
                    self.run_backup_now()
                except Exception:
                    pass


def open_in_explorer(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # noqa: S606 — user-triggered, opening a local folder
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])
