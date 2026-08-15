"""Application bootstrap: config, database, Telegram service, collector,
main window — wired together and run on a shared Qt/asyncio event loop."""
from __future__ import annotations

import asyncio
import sys

import qasync
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QDialog

from . import APP_NAME
from . import diagnostics
from . import safety_net
from .bots.crypto import register_bot_token_rotation
from .bots.manager import BotManager
from .config import AppConfig
from .db.database import Database
from .integrations.bitrix import register_bitrix_rotation
from .paths import PATHS, resource_path
from .security import SecurityService
from .services.backup_service import BackupService
from .services.bitrix_sync_service import BitrixSyncService
from .services.export_schedule_service import ExportScheduleService
from .services.export_service import ExportService
from .services.ignore_service import IgnoreService
from .services.lead_reminder_service import LeadReminderService
from .services.retention_service import RetentionService
from .services.watch_service import WatchService
from .telegram.accounts import AccountRegistry
from .telegram.collector import Collector
from .telegram.service import TelegramService
from .ui.context import AppContext
from .ui.main_window import MainWindow
from .ui.theme import apply_theme
from .ui.unlock_dialog import UnlockDialog


def run() -> int:
    PATHS.ensure()
    safety_net.install(PATHS)
    config = AppConfig.load(PATHS)

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    # Fusion, explicitly, on every platform.
    #
    # This is why the checked block tab stayed grey on Windows no matter
    # how the stylesheet was written. Qt 6.7 makes «windows11» the default
    # style on Windows 11, and that style paints QPushButton natively —
    # including its own idea of what a checked button looks like — so the
    # `:checked { background: ... }` rule was simply discarded there. On
    # Linux the default is already Fusion, which honours it, so the fill
    # looked correct in every test render and broken on the real machine.
    #
    # Pinning the style also means the app looks the same everywhere
    # instead of half-inheriting whatever the OS theme decides.
    apply_theme(app)
    icon_path = resource_path("resources", "icon.png")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    security = SecurityService(config, PATHS)
    if security.enabled:
        # Plain synchronous dialog — this runs before Telethon/qasync are
        # touched at all, so no event loop is needed yet. Rejecting it
        # (closed, or gave up) exits the app entirely: without api_hash
        # decrypted there is nothing else it can do.
        if UnlockDialog(security, config, PATHS).exec() != QDialog.Accepted:
            return 0

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    safety_net.install_loop_handler(loop, PATHS)
    app.aboutToQuit.connect(security.lock)

    db = Database(PATHS.db_path)

    # Session trace for hands-on testing — off unless switched on in
    # Настройки. Started here, before any service exists, so the file
    # covers the whole run including startup. TEMPORARY, see TEMPORARY.md.
    diagnostics.install(PATHS, bool(db.get_setting(diagnostics.SETTING_KEY, False)))

    tg = TelegramService(config)
    collector = Collector(db, tg, config, PATHS)
    export_service = ExportService(db, PATHS)
    ignore_service = IgnoreService(db)
    backup_service = BackupService(db, PATHS)
    bot_manager = BotManager(db, tg, security)
    register_bot_token_rotation(db, security)
    register_bitrix_rotation(db, security)

    accounts = AccountRegistry(db, config, PATHS, tg)
    accounts.ensure_primary_row()
    collector.accounts = accounts
    bot_manager.userbot_runner.accounts = accounts

    watch_service = WatchService(db)
    collector.watch_service = watch_service
    retention_service = RetentionService(db, PATHS)
    export_schedule_service = ExportScheduleService(
        db, export_service,
        on_log=lambda text, tone="": collector._log("выгрузка", text, tone),
    )
    lead_reminder_service = LeadReminderService(db)
    bitrix_sync_service = BitrixSyncService(
        db, security, on_log=lambda text, tone="": collector._log("Bitrix24", text, tone),
    )

    ctx = AppContext(
        config=config, paths=PATHS, db=db, tg=tg, collector=collector,
        export_service=export_service, ignore_service=ignore_service,
        backup_service=backup_service, security=security, bot_manager=bot_manager,
        watch_service=watch_service, retention_service=retention_service,
        export_schedule_service=export_schedule_service,
        lead_reminder_service=lead_reminder_service,
        bitrix_sync_service=bitrix_sync_service, accounts=accounts,
    )

    window = MainWindow(ctx)
    window.show()

    try:
        with loop:
            loop.create_task(backup_service.run_periodic())
            export_schedule_service.start()
            lead_reminder_service.start()
            bitrix_sync_service.start()
            loop.run_forever()
    finally:
        session = diagnostics.current()
        if session is not None:
            session.stop()
    return 0
