"""Application bootstrap: config, database, Telegram service, collector,
main window — wired together and run on a shared Qt/asyncio event loop."""
from __future__ import annotations

import asyncio
import sys

import qasync
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QDialog

from . import APP_NAME
from . import safety_net
from .bots.crypto import register_bot_token_rotation
from .bots.manager import BotManager
from .config import AppConfig
from .db.database import Database
from .paths import PATHS, resource_path
from .security import SecurityService
from .services.backup_service import BackupService
from .services.export_service import ExportService
from .services.ignore_service import IgnoreService
from .telegram.collector import Collector
from .telegram.service import TelegramService
from .ui.context import AppContext
from .ui.main_window import MainWindow
from .ui.theme import build_qss
from .ui.unlock_dialog import UnlockDialog


def run() -> int:
    PATHS.ensure()
    safety_net.install(PATHS)
    config = AppConfig.load(PATHS)

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyleSheet(build_qss())
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
    tg = TelegramService(config)
    collector = Collector(db, tg, config, PATHS)
    export_service = ExportService(db, PATHS)
    ignore_service = IgnoreService(db)
    backup_service = BackupService(db, PATHS)
    bot_manager = BotManager(db, tg, security)
    register_bot_token_rotation(db, security)

    ctx = AppContext(
        config=config, paths=PATHS, db=db, tg=tg, collector=collector,
        export_service=export_service, ignore_service=ignore_service,
        backup_service=backup_service, security=security, bot_manager=bot_manager,
    )

    window = MainWindow(ctx)
    window.show()

    with loop:
        loop.create_task(backup_service.run_periodic())
        loop.run_forever()
    return 0
