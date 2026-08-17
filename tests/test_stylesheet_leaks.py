"""Стили контейнеров не должны протекать на дочерние виджеты.

Qt применяет таблицу стилей без селектора не только к самому виджету, но и
ко всем его детям. Из-за этого фон контейнера перекрывал заливку кнопок
внутри него, и выбранная вкладка выглядела как невыбранная. Ошибка тихая:
ничего не падает, просто состояние перестаёт быть видно.
"""
import os, sys, asyncio, collections
from pathlib import Path
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PySide6.QtWidgets import QApplication, QLabel, QAbstractButton
app = QApplication.instance() or QApplication(sys.argv)
from chatgrab.ui.theme import BASE_STYLE, apply_theme
apply_theme(app)
loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)

from _bootstrap import fresh_env
from chatgrab.telegram.service import TelegramService
from chatgrab.telegram.collector import Collector
from chatgrab.services.export_service import ExportService
from chatgrab.services.ignore_service import IgnoreService
from chatgrab.services.backup_service import BackupService
from chatgrab.bots.manager import BotManager
from chatgrab.services.watch_service import WatchService
from chatgrab.services.retention_service import RetentionService
from chatgrab.services.export_schedule_service import ExportScheduleService
from chatgrab.services.lead_reminder_service import LeadReminderService
from chatgrab.services.bitrix_sync_service import BitrixSyncService
from chatgrab.ui.context import AppContext
from chatgrab.ui.main_window import MainWindow, NAV_BY_BLOCK, COMMON_ITEMS

paths, db, config, sec = fresh_env("cgleak")
tg = TelegramService(config)
ctx = AppContext(config=config, paths=paths, db=db, tg=tg,
    collector=Collector(db, tg, config, paths),
    export_service=ExportService(db, paths), ignore_service=IgnoreService(db),
    backup_service=BackupService(db, paths), security=sec,
    bot_manager=BotManager(db, tg, sec),
    watch_service=WatchService(db), retention_service=RetentionService(db, paths),
    export_schedule_service=ExportScheduleService(db, ExportService(db, paths)),
    lead_reminder_service=LeadReminderService(db),
    bitrix_sync_service=BitrixSyncService(db, sec))
db.add_chat(1, "Биржа", "b", "all", None)
ctx.bot_manager.create_bot("Бот", "userbot", None, "custom", None)

win = MainWindow(ctx); win.resize(1320, 880); win.show()
for key in [k for _, items in NAV_BY_BLOCK.items() for k, _ in items] + [k for k, _ in COMMON_ITEMS]:
    win.navigate(key); app.processEvents()
app.processEvents()

print("== стиль виджетов закреплён ==")
# Без этого весь тест ниже проверяет чужую платформу: на Windows 11 Qt 6.7
# по умолчанию берёт стиль «windows11», который рисует QPushButton сам и
# выбрасывает `:checked { background: ... }`. Заливка выглядела правильной
# в каждом снимке на Linux (там Fusion по умолчанию) и не работала у
# пользователя. Проверяем и то, что приложение закрепляет стиль само.
src = (Path(__file__).resolve().parent.parent / "chatgrab" / "app.py").read_text(encoding="utf-8")
assert "apply_theme(app)" in src, "app.py должен применять тему через apply_theme"
base_style = app.property("chatgrab_base_style")
print(f"  стиль приложения: {base_style} (ожидается {BASE_STYLE.lower()})")
assert base_style == BASE_STYLE.lower(), f"стиль не закреплён: {base_style}"

print("\n== контейнеры со стилем без селектора ==")
RISKY = ("background", "border", "color")
offenders = []
def walk(w, depth=0):
    sheet = (w.styleSheet() or "").strip()
    if sheet and "{" not in sheet:
        kids = [c for c in w.findChildren(object) if hasattr(c, "styleSheet")]
        if kids and any(prop in sheet for prop in RISKY):
            offenders.append((w.__class__.__name__, w.objectName(), len(kids), sheet[:60]))
    for child in w.children():
        if hasattr(child, "styleSheet") and hasattr(child, "children"):
            walk(child, depth + 1)
walk(win)
for cls, name, nkids, sheet in offenders:
    print(f"  ⚠ {cls} «{name}» — {nkids} дочерних — {sheet!r}")
if not offenders:
    print("  не найдено")
assert not offenders, f"стиль контейнера протечёт на детей: {offenders}"

print("\n== выбранное состояние видно на переключателе блоков ==")
img = win.grab().toImage()
def fill(widget):
    tl = widget.mapTo(win, widget.rect().topLeft())
    h = collections.Counter()
    for y in range(tl.y() + 4, tl.y() + widget.height() - 4):
        for x in range(tl.x() + 4, tl.x() + widget.width() - 4):
            h[img.pixelColor(x, y).name()] += 1
    return h.most_common(1)[0][0]

fills = {k: (b.isChecked(), fill(b)) for k, b in win.block_buttons.items()}
for k, (checked, colour) in fills.items():
    print(f"  {k:<8} выбрана={checked}  заливка={colour}")
colours = {c for _, c in fills.values()}
assert len(colours) == 2, f"выбранная и невыбранная вкладки одного цвета: {fills}"

print("\n== то же для пунктов меню и чипов ==")
# именно с открытым «Сегодня»: иначе оба пункта невыбраны и сравнивать нечего
win.navigate("today"); app.processEvents(); app.processEvents()
img = win.grab().toImage()
nav_on = fill(win._nav_buttons["today"])
nav_off = fill(win._nav_buttons["chats"])
print(f"  меню: выбран={nav_on}  невыбран={nav_off}")
assert nav_on != nav_off, "выбранный пункт меню не выделяется"

win.navigate("browse"); app.processEvents(); app.processEvents()
img = win.grab().toImage()
chips = list(win.screens["browse"].chat_chips.values())
if len(chips) > 1:
    on, off = fill(chips[0]), fill(chips[1])
    print(f"  чипы: выбран={on}  невыбран={off}")
    assert on != off, "выбранный чип не выделяется"

print("\nТЕСТ ПРОЙДЕН: заливка состояний видна, стили не протекают")
