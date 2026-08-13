"""Трей: сворачивание вместо выхода, уведомления по одному разу на проблему,
автозапуск не ломается вне Windows."""
import os, sys, asyncio
from pathlib import Path
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PySide6.QtWidgets import QApplication
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
from chatgrab.ui.context import AppContext
from chatgrab.ui.main_window import MainWindow
from chatgrab.ui import tray as tray_mod

base = "/tmp/cgtray"; os.system(f"rm -rf {base}")
paths = Paths(Path(base)); paths.ensure()
config = AppConfig.load(paths); db = Database(paths.db_path)
tg = TelegramService(config); sec = SecurityService(config, paths)
ctx = AppContext(config=config, paths=paths, db=db, tg=tg,
    collector=Collector(db, tg, config, paths),
    export_service=ExportService(db, paths), ignore_service=IgnoreService(db),
    backup_service=BackupService(db, paths), security=sec,
    bot_manager=BotManager(db, tg, sec))

db.add_chat(1, "Биржа", "b", "all", None)
bot_id = ctx.bot_manager.create_bot("Бот", "userbot", None, "custom", None)

win = MainWindow(ctx)

print("== автозапуск не падает вне Windows ==")
print("  поддерживается:", tray_mod.autostart_supported())
print("  включён:", tray_mod.autostart_enabled())
assert tray_mod.set_autostart(True) == tray_mod.autostart_supported()
assert tray_mod.autostart_enabled() in (True, False)
print("  вызовы безопасны на этой платформе")

# трей в offscreen недоступен — подменяем на счётчик, логика та же
notes = []
class FakeTray:
    def setToolTip(self, t): self.tip = t
    def showMessage(self, title, text, icon, ms): notes.append((title, text))
win.tray.tray = FakeTray()
class FakeAction:
    def setText(self, t): pass
win.tray.status_action = FakeAction()

print("\n== уведомление о проблеме приходит один раз ==")
db.set_chat_field(1, last_error="Чат стал недоступен")
win.tray.refresh(); win.tray.refresh(); win.tray.refresh()
chat_notes = [n for n in notes if "Чат недоступен" in n[0]]
print("  уведомлений о чате:", len(chat_notes))
assert len(chat_notes) == 1, f"продублировалось: {len(chat_notes)}"

print("\n== проблема ушла и вернулась — уведомляем снова ==")
db.set_chat_field(1, last_error=None)
win.tray.refresh()
db.set_chat_field(1, last_error="Снова недоступен")
win.tray.refresh()
chat_notes = [n for n in notes if "Чат недоступен" in n[0]]
print("  уведомлений о чате:", len(chat_notes))
assert len(chat_notes) == 2

print("\n== упавший бот тоже уведомляет один раз ==")
notes.clear()
db.set_bot_field(bot_id, status="error", last_error="Не задан менеджер")
win.tray.refresh(); win.tray.refresh()
bot_notes = [n for n in notes if "Бот остановился" in n[0]]
print("  уведомлений о боте:", len(bot_notes))
assert len(bot_notes) == 1

print("\n== подсказка в трее описывает состояние ==")
win.tray.refresh()
print("  ", win.tray.tray.tip)
assert "Слушаем" in win.tray.tray.tip

print("\n== закрытие окна прячет в трей, а не выходит ==")
from PySide6.QtGui import QCloseEvent
win.show(); app.processEvents()
ev = QCloseEvent(); win.closeEvent(ev)
print("  окно видимо:", win.isVisible(), "| событие принято:", ev.isAccepted())
assert not ev.isAccepted(), "приложение бы вышло"
assert not win.isVisible(), "окно должно быть скрыто"
hint = [n for n in notes if "продолжает работать" in n[0]]
assert hint, "первое сворачивание должно объяснить, куда делось окно"
print("  подсказка показана:", hint[0][0])

print("\n== подсказка показывается только один раз ==")
notes.clear()
win.show(); ev = QCloseEvent(); win.closeEvent(ev)
assert not [n for n in notes if "продолжает работать" in n[0]], "подсказка повторилась"
print("  повторно не показана")

print("\n== настройка выключена — окно закрывается по-настоящему ==")
db.set_setting("tray_minimize_on_close", False)
win.show(); ev = QCloseEvent(); win.closeEvent(ev)
print("  событие принято:", ev.isAccepted())
assert ev.isAccepted()

print("\n== «Выйти» из меню закрывает несмотря на настройку ==")
db.set_setting("tray_minimize_on_close", True)
win.show(); win.allow_close = True
ev = QCloseEvent(); win.closeEvent(ev)
assert ev.isAccepted()
print("  ok")

print("\nТЕСТ ПРОЙДЕН: трей и уведомления работают")
