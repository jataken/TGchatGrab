"""Everything a screen needs, bundled together so widgets don't have to
thread six constructor arguments through the whole tree."""
from __future__ import annotations

from dataclasses import dataclass

from ..config import AppConfig
from ..db.database import Database
from ..paths import Paths
from ..services.backup_service import BackupService
from ..services.export_service import ExportService
from ..services.ignore_service import IgnoreService
from ..telegram.collector import Collector
from ..telegram.service import TelegramService


@dataclass
class AppContext:
    config: AppConfig
    paths: Paths
    db: Database
    tg: TelegramService
    collector: Collector
    export_service: ExportService
    ignore_service: IgnoreService
    backup_service: BackupService
