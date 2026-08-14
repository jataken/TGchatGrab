"""Выбор бота общий для всего блока «Боты»: нельзя смотреть правила
одного бота и сценарий другого."""
import os, sys, asyncio
import tempfile
import shutil
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
from chatgrab.services.watch_service import WatchService
from chatgrab.services.retention_service import RetentionService
from chatgrab.services.export_schedule_service import ExportScheduleService
from chatgrab.ui.context import AppContext
from chatgrab.ui.main_window import MainWindow, BOT_SCOPED_SCREENS

base = os.path.join(tempfile.gettempdir(), "cgctx"); shutil.rmtree(base, ignore_errors=True)
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

a = ctx.bot_manager.create_bot("Бот А", "userbot", None, "b2b", None)
b = ctx.bot_manager.create_bot("Бот Б", "userbot", None, "b2c", None)
db.add_template(a, "Только у А", "текст А", [])
db.add_template(b, "Только у Б", "текст Б", [])
db.add_trigger(a, "keyword", {"keywords": ["а"]})

win = MainWindow(ctx); win.show()

print("== при открытии выбран первый бот ==")
win.navigate("rules"); app.processEvents()
print("  выбран:", ctx.bot_selection.current, "| у экрана:", win.screens["rules"].rules_tab.selected_bot_id)
assert ctx.bot_selection.current == a
assert win.screens["rules"].rules_tab.selected_bot_id == a

print("\n== переключение бота доходит до всех экранов блока ==")
ctx.bot_selection.set_current(b)
app.processEvents()
for key in ["rules", "scenario", "templates"]:
    win.navigate(key); app.processEvents()
    screen = win.screens[key]
    got = getattr(screen, "selected_bot_id", None)
    if got is None:
        got = getattr(screen, "rules_tab", getattr(screen, "templates_tab", None)).selected_bot_id
    print(f"  {key:10s} -> bot {got}")
    assert got == b, f"{key} остался на другом боте"

print("\n== шаблоны показываются только выбранного бота ==")
win.navigate("templates"); app.processEvents()
tt = win.screens["templates"].templates_tab
names = [tt.tpl_list.item(i).text() for i in range(tt.tpl_list.count())]
print("  список:", names)
assert "Только у Б" in names, names
assert "Только у А" not in names, "виден шаблон чужого бота: " + str(names)

print("\n== селектор виден только на экранах, где он что-то значит ==")
for key, expect in [("rules", True), ("scenario", True), ("templates", True),
                    ("bots", False), ("leads", False), ("botlog", False), ("today", False)]:
    win.navigate(key); app.processEvents()
    vis = win.bot_selector_box.isVisible()
    print(f"  {key:10s} селектор={'виден' if vis else 'скрыт'}")
    assert vis == expect, f"{key}: ожидали {expect}"
    assert (key in BOT_SCOPED_SCREENS) == expect

print("\n== после удаления выбранного бота выбор переходит на оставшегося ==")
win.navigate("rules"); app.processEvents()
ctx.bot_selection.set_current(b)
asyncio.get_event_loop().run_until_complete(ctx.bot_manager.delete_bot(b))
win.navigate("rules"); app.processEvents()
print("  выбран теперь:", ctx.bot_selection.current)
assert ctx.bot_selection.current == a, "выбор остался на удалённом боте"

print("\n== последний бот удалён — экраны не падают ==")
asyncio.get_event_loop().run_until_complete(ctx.bot_manager.delete_bot(a))
for key in ["rules", "scenario", "templates", "bots"]:
    win.navigate(key); app.processEvents()
print("  выбран:", ctx.bot_selection.current)
assert ctx.bot_selection.current is None
assert not win.bot_selector_box.isVisible()

print("\nТЕСТ ПРОЙДЕН: бот — общий контекст блока")
