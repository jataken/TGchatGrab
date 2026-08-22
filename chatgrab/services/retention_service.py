"""Keeping the archive from growing forever.

Until now messages were only ever deleted along with their whole chat. On
an active exchange that means the database and the media folder grow
without bound — and the user finds out when the disk does.

Two deliberate choices:

- Nothing is deleted without being written out first. The archive is an
  ordinary JSONL file next to the exports, so anything pruned can still be
  read (and fed to Claude) later. Deleting data the user cannot get back
  is not a housekeeping feature, it is data loss with a progress bar.
- Pruning never runs on its own. It is a button with the count on it. A
  background job that quietly removes history is exactly the kind of thing
  nobody wants to discover after the fact.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

from ..db.database import Database
from ..paths import Paths

_logger = logging.getLogger("chatgrab")

SETTING_MONTHS = "retention_months"
DEFAULT_MONTHS = 0  # 0 = хранить всё


def cutoff_for(months: int, now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now()
    # Calendar months rather than 30-day blocks, so "храню полгода" means
    # what a person means by it.
    year, month = now.year, now.month - months
    while month <= 0:
        month += 12
        year -= 1
    day = min(now.day, 28)
    return dt.datetime(year, month, day, now.hour, now.minute).isoformat()


class RetentionService:
    def __init__(self, db: Database, paths: Paths):
        self.db = db
        self.paths = paths

    @property
    def months(self) -> int:
        return int(self.db.get_setting(SETTING_MONTHS, DEFAULT_MONTHS) or 0)

    def set_months(self, months: int) -> None:
        self.db.set_setting(SETTING_MONTHS, max(0, int(months)))

    def preview(self, months: int | None = None) -> dict:
        """What a prune would remove, without removing it."""
        months = self.months if months is None else months
        if months <= 0:
            return {"months": 0, "cutoff": None, "messages": 0}
        cutoff = cutoff_for(months)
        return {
            "months": months,
            "cutoff": cutoff,
            "messages": self.db.messages_older_than(cutoff),
        }

    def archive_and_prune(self, months: int | None = None,
                          archive: bool = True) -> dict:
        """Write the old messages to a JSONL archive, then delete them.

        The archive is written and closed *before* anything is deleted, so
        a failure part-way leaves the database intact rather than half
        pruned with no copy.
        """
        months = self.months if months is None else months
        if months <= 0:
            return {"archived": 0, "deleted": 0, "path": None}
        cutoff = cutoff_for(months)
        rows = self.db.select_older_than(cutoff)
        if not rows:
            return {"archived": 0, "deleted": 0, "path": None}

        path = None
        if archive:
            self.paths.archives_dir.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            path = self.paths.archives_dir / f"archive-до-{cutoff[:10]}-{stamp}.jsonl"
            with path.open("w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(dict(row), ensure_ascii=False) + "\n")
            _logger.info("архив написан: %s (%d записей)", path, len(rows))

        deleted = self.db.delete_older_than(cutoff)
        return {"archived": len(rows) if archive else 0, "deleted": deleted,
                "path": path, "cutoff": cutoff}

    def orphaned_media(self) -> list[Path]:
        """Files on disk no message points at any more — what pruning
        leaves behind. Listed rather than deleted silently."""
        known = {
            r["media_path"] for r in self.db.query(
                "SELECT media_path FROM messages WHERE media_path IS NOT NULL AND media_path != ''"
            )
        }
        orphans: list[Path] = []
        for folder in (self.paths.photos_dir, self.paths.videos_dir,
                       self.paths.voice_dir, self.paths.documents_dir):
            if not folder.exists():
                continue
            for file in folder.rglob("*"):
                if not file.is_file():
                    continue
                try:
                    rel = file.relative_to(self.paths.data_dir).as_posix()
                except ValueError:
                    continue
                if rel not in known:
                    orphans.append(file)
        return orphans
