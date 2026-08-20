"""П10: «Ретеншн для почты... переиспользует retention_service целиком»
— reused for real: cutoff_for()'s calendar-month arithmetic (imported,
not reimplemented) and the same non-negotiable shape as
RetentionService.archive_and_prune() — nothing is ever deleted without
being written to a JSONL archive first, and pruning is a button with a
count on it, never a background job.

A second, parallel class rather than parameterizing RetentionService
itself: that class's own methods (messages_older_than/select_older_than/
delete_older_than in db/mixins/retention.py) are hardcoded SQL against
the Telegram `messages` table — reusing the *class* wholesale would mean
either forcing mail through Telegram's schema or teaching that class
two unrelated table shapes behind one flag, more indirection than a
second small class with the same two settings/preview/prune methods.

Two independent knobs, not one, per the checklist's own "отдельный срок
для вложений": mail_retention_months prunes whole messages (and
everything attached to them); mail_attachment_retention_months only
strips the attachment files off messages that are otherwise kept — the
body text and the paper trail stay, just the (usually much bigger)
attached files go.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

from ..db.database import Database
from ..paths import Paths
from .retention_service import cutoff_for

_logger = logging.getLogger("chatgrab")

SETTING_MONTHS = "mail_retention_months"
SETTING_ATTACHMENT_MONTHS = "mail_attachment_retention_months"
DEFAULT_MONTHS = 0  # 0 = хранить всё, тот же смысл, что и у Telegram-версии


class MailRetentionService:
    def __init__(self, db: Database, paths: Paths):
        self.db = db
        self.paths = paths

    # ---- messages ------------------------------------------------------
    @property
    def months(self) -> int:
        return int(self.db.get_setting(SETTING_MONTHS, DEFAULT_MONTHS) or 0)

    def set_months(self, months: int) -> None:
        self.db.set_setting(SETTING_MONTHS, max(0, int(months)))

    def preview(self, months: int | None = None) -> dict:
        months = self.months if months is None else months
        if months <= 0:
            return {"months": 0, "cutoff": None, "messages": 0}
        cutoff = cutoff_for(months)
        return {"months": months, "cutoff": cutoff,
                "messages": self.db.count_mail_messages_older_than(cutoff)}

    def archive_and_prune(self, months: int | None = None, archive: bool = True) -> dict:
        """Same write-before-delete order as RetentionService's own
        method: the JSONL file is written and closed before a single row
        is deleted, so a crash mid-run leaves the database untouched
        rather than half-pruned with nothing to show for it. Attachment
        *files* aren't embedded in the archive (same choice as Telegram
        media — a path/filename/size record, not the bytes); pruning
        their rows and files is prune_attachments()'s separate job,
        deliberately not folded into this one call."""
        months = self.months if months is None else months
        if months <= 0:
            return {"archived": 0, "deleted": 0, "path": None}
        cutoff = cutoff_for(months)
        rows = self.db.select_mail_messages_older_than(cutoff)
        if not rows:
            return {"archived": 0, "deleted": 0, "path": None}

        path = None
        if archive:
            self.paths.archives_dir.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            path = self.paths.archives_dir / f"mail-archive-до-{cutoff[:10]}-{stamp}.jsonl"
            with path.open("w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(dict(row), ensure_ascii=False) + "\n")
            _logger.info("почтовый архив написан: %s (%d записей)", path, len(rows))

        deleted, attachment_paths = self.db.delete_mail_messages_older_than(cutoff)
        for p in attachment_paths:
            self._unlink_quietly(p)
        return {"archived": len(rows) if archive else 0, "deleted": deleted,
                "path": path, "cutoff": cutoff}

    # ---- attachments only ------------------------------------------------
    @property
    def attachment_months(self) -> int:
        return int(self.db.get_setting(SETTING_ATTACHMENT_MONTHS, DEFAULT_MONTHS) or 0)

    def set_attachment_months(self, months: int) -> None:
        self.db.set_setting(SETTING_ATTACHMENT_MONTHS, max(0, int(months)))

    def preview_attachments(self, months: int | None = None) -> dict:
        months = self.attachment_months if months is None else months
        if months <= 0:
            return {"months": 0, "cutoff": None, "count": 0, "bytes": 0}
        cutoff = cutoff_for(months)
        rows = self.db.mail_attachments_older_than(cutoff)
        return {"months": months, "cutoff": cutoff, "count": len(rows),
                "bytes": sum(r["size_bytes"] or 0 for r in rows)}

    def prune_attachments(self, months: int | None = None) -> dict:
        """Strips attachment *files* off messages older than cutoff —
        the message, its subject/body/thread, stays exactly where it
        was; only the (usually much larger) attached files and their
        mail_attachment rows go. No archive step here: the message
        itself, which is what archive_and_prune() preserves, is
        untouched by this — there's nothing new being destroyed that
        the *message* archive wouldn't already cover if it's ever
        pruned too."""
        months = self.attachment_months if months is None else months
        if months <= 0:
            return {"deleted": 0, "bytes_freed": 0}
        cutoff = cutoff_for(months)
        rows = self.db.mail_attachments_older_than(cutoff)
        if not rows:
            return {"deleted": 0, "bytes_freed": 0}
        freed = 0
        for row in rows:
            if row["path"] and self._unlink_quietly(row["path"]):
                freed += row["size_bytes"] or 0
        self.db.delete_mail_attachment_rows([r["id"] for r in rows])
        return {"deleted": len(rows), "bytes_freed": freed}

    @staticmethod
    def _unlink_quietly(path_str: str) -> bool:
        try:
            path = Path(path_str)
            if path.exists():
                path.unlink()
                return True
        except OSError:
            _logger.warning("не удалось удалить файл вложения при ретеншне: %s", path_str)
        return False
