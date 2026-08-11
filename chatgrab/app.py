"""Application bootstrap: config, database, Telegram service, collector,
main window — wired together and run on a shared Qt/asyncio event loop."""
from __future__ import annotations

import asyncio
import sys

import qasync
from PySide6.QtWidgets import QApplication

from . import APP_NAME
from .config import AppConfig
from .db.database import Database
from .paths import PATHS
from .services.backup_service import BackupService
from .services.export_service import ExportService
from .services.ignore_service import IgnoreService
from .telegram.collector import Collector
from .telegram.service import TelegramService
from .ui.context import AppContext
from .ui.main_window import MainWindow
from .ui.theme import build_qss


def run() -> int:
    PATHS.ensure()
    config = AppConfig.load(PATHS)

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyleSheet(build_qss())

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    db = Database(PATHS.db_path)
    tg = TelegramService(config)
    collector = Collector(db, tg, config, PATHS)
    export_service = ExportService(db, PATHS)
    ignore_service = IgnoreService(db)
    backup_service = BackupService(db, PATHS)

    ctx = AppContext(
        config=config, paths=PATHS, db=db, tg=tg, collector=collector,
        export_service=export_service, ignore_service=ignore_service,
        backup_service=backup_service,
    )

    window = MainWindow(ctx)
    window.show()

    with loop:
        loop.create_task(backup_service.run_periodic())
        loop.run_forever()
    return 0
