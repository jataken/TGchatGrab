"""Everything a screen needs, bundled together so widgets don't have to
thread six constructor arguments through the whole tree."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..bots.manager import BotManager
from ..config import AppConfig
from ..db.database import Database
from ..paths import Paths
from ..security import SecurityService
from ..services.backup_service import BackupService
from ..services.bitrix_sync_service import BitrixSyncService
from ..services.export_schedule_service import ExportScheduleService
from ..services.export_service import ExportService
from ..services.ignore_service import IgnoreService
from ..services.lead_reminder_service import LeadReminderService
from ..services.retention_service import RetentionService
from ..services.watch_service import WatchService
from ..telegram.accounts import AccountRegistry
from ..telegram.collector import Collector
from ..telegram.service import TelegramService
from .bot_selection import BotSelection


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
    security: SecurityService
    bot_manager: BotManager
    watch_service: WatchService
    retention_service: RetentionService
    export_schedule_service: ExportScheduleService
    lead_reminder_service: LeadReminderService
    bitrix_sync_service: BitrixSyncService
    accounts: AccountRegistry | None = None
    # Which bot the «Боты» block's screens are currently editing.
    bot_selection: BotSelection = field(default_factory=BotSelection)
