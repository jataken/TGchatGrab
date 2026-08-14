"""Диагностическая запись фиксирует переходы, нажатия и скрытые ошибки."""
import os, sys, asyncio, logging
from pathlib import Path
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PySide6.QtWidgets import QApplication, QPushButton
app = QApplication.instance() or QApplication(sys.argv)
loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)

from chatgrab.paths import Paths
from chatgrab.config import AppConfig
from chatgrab.db.database import Database
from chatgrab.telegram.service import TelegramService
from chatgrab.telegram.collector import Collector
from chatgrab.services.export_service import ExportService
from chatgrab.services.ignore_service import IgnoreService
from chatgrab.services.backup_service import BackupService
from chatgrab.security import SecurityService
from chatgrab.bots.manager import BotManager
from chatgrab.services.watch_service import WatchService
from chatgrab.services.retention_service import RetentionService
from chatgrab.services.export_schedule_service import ExportScheduleService
from chatgrab.ui.context import AppContext
from chatgrab.ui.main_window import MainWindow
from chatgrab import diagnostics

base = "/tmp/cgdiag"; os.system(f"rm -rf {base}")
paths = Paths(Path(base)); paths.ensure()
config = AppConfig.load(paths); db = Database(paths.db_path)
tg = TelegramService(config); sec = SecurityService(config, paths)
ctx = AppContext(config=config, paths=paths, db=db, tg=tg,
    collector=Collector(db, tg, config, paths),
    export_service=ExportService(db, paths), ignore_service=IgnoreService(db),
    backup_service=BackupService(db, paths), security=sec,
    bot_manager=BotManager(db, tg, sec),
    watch_service=WatchService(db), retention_service=RetentionService(db, paths),
    export_schedule_service=ExportScheduleService(db, ExportService(db, paths)))

print("== выключена по умолчанию ==")
assert db.get_setting(diagnostics.SETTING_KEY, False) is False
assert diagnostics.install(paths, False) is None
assert diagnostics.current() is None
# вызовы при выключенной записи не должны падать
diagnostics.event("тест", "ничего"); diagnostics.screen("Экран")
diagnostics.failure("нигде", RuntimeError("не должно записаться"))
print("  вызовы безопасны, файлов нет")
assert not (paths.data_dir / "diagnostics").exists() or \
       not list((paths.data_dir / "diagnostics").glob("session-*.log"))

print("\n== включаем ==")
session = diagnostics.install(paths, True)
assert session is not None and session.active
print("  файл:", session.path.name)

win = MainWindow(ctx)
win.show()

print("\n== переходы по экранам пишутся ==")
for key in ["today", "chats", "export", "bots", "settings"]:
    win.navigate(key); app.processEvents()

print("== нажатия кнопок пишутся ==")
btn = QPushButton("Тестовая кнопка")
btn.show()
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QMouseEvent
for ev_type in (QMouseEvent.Type.MouseButtonPress, QMouseEvent.Type.MouseButtonRelease):
    app.sendEvent(btn, QMouseEvent(ev_type, QPoint(5, 5), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
app.processEvents()

print("== скрытые предупреждения попадают в запись ==")
logging.getLogger("chatgrab").warning("тихое предупреждение из недр приложения")
ctx.collector.log_event.emit({"time": "12:00:00", "chat": "Биржа",
                              "text": "Telegram попросил подождать 42 с", "tone": "warn"})
app.processEvents()

print("== пойманное исключение фиксируется ==")
diagnostics.failure("проверка", ValueError("так ломается незаметно"))

session.stop()
text = session.path.read_text(encoding="utf-8")
print(f"\n== содержимое ({len(text.splitlines())} строк) ==")
for line in text.splitlines()[:4]:
    print("   ", line)
print("    …")

checks = {
    "заголовок с версией": "окружение:" in text,
    "переход на экран": "открыт «Источники»" in text or "открыт «Сегодня»" in text,
    "нажатие кнопки": "Тестовая кнопка" in text,
    "тихое предупреждение": "тихое предупреждение" in text,
    "событие сбора": "Telegram попросил подождать" in text,
    "пойманное исключение": "так ломается незаметно" in text,
    "трассировка": "ValueError" in text,
    "остановка записи": "запись остановлена" in text,
}
print()
for name, ok in checks.items():
    print(f"  {'✓' if ok else '✗'} {name}")
assert all(checks.values()), [k for k, v in checks.items() if not v]

print("\n== после остановки запись не ведётся ==")
before = session.path.stat().st_size
diagnostics.event("после", "не должно попасть")
assert session.path.stat().st_size == before

print("\n== секреты не попадают в файл ==")
for secret in [config.api_hash or "нет-хеша", "worker.session"]:
    if secret and secret != "нет-хеша":
        assert secret not in text, f"в записи оказался секрет: {secret}"
print("  ok")

print("\nТЕСТ ПРОЙДЕН: диагностическая запись работает")
