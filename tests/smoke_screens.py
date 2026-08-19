"""Каждый экран открывается без ошибок на посевных данных.

Ловит то, что не поймает ни один модульный тест: опечатку в имени
виджета, обращение к удалённому полю, забытый импорт — всё, что падает
только при реальном построении интерфейса.
"""
import asyncio, os, sys, traceback
import os
import tempfile
import shutil
from pathlib import Path
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication(sys.argv)
from chatgrab.ui.theme import apply_theme
apply_theme(app)
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
from chatgrab.services.lead_reminder_service import LeadReminderService
from chatgrab.services.mail_service import MailService
from chatgrab.services.bitrix_sync_service import BitrixSyncService
from chatgrab.ui.context import AppContext
from chatgrab.ui.main_window import MainWindow, NAV_BY_BLOCK, COMMON_ITEMS

base = os.path.join(tempfile.gettempdir(), "cgsmoke"); shutil.rmtree(base, ignore_errors=True)
paths = Paths(Path(base)); paths.ensure()
config = AppConfig.load(paths); db = Database(paths.db_path)
tg = TelegramService(config); sec = SecurityService(config, paths)
ctx = AppContext(config=config, paths=paths, db=db, tg=tg,
    collector=Collector(db, tg, config, paths),
    export_service=ExportService(db, paths), ignore_service=IgnoreService(db),
    backup_service=BackupService(db, paths), security=sec,
    bot_manager=BotManager(db, tg, sec),
    watch_service=WatchService(db), retention_service=RetentionService(db, paths),
    export_schedule_service=ExportScheduleService(db, ExportService(db, paths)),
    lead_reminder_service=LeadReminderService(db),
    bitrix_sync_service=BitrixSyncService(db, sec),
    mail_service=MailService(db, paths, sec))

# посевные данные: разные состояния чатов, бот с ошибкой, заявки, сценарий
for cid, title, uname in [(1001, "Косметическое сырьё · Биржа", "cosmo"),
                          (1002, "Упаковка B2B", "pack"),
                          (1003, "Флаконы", "flacon")]:
    db.add_chat(cid, title, uname, "all", None)
db.set_chat_field(1002, status="loading", approx_total=30000)
db.set_chat_field(1003, enabled=0, status="off")
for i in range(1, 31):
    db.upsert_message({"chat_id": 1001, "message_id": i, "chat_title": "Биржа",
        "date": f"2026-08-{(i % 28) + 1:02d}T10:00:00", "edited_date": None,
        "sender_id": i, "sender_username": f"u{i}", "sender_display_name": f"U{i}",
        "text": "Ищем поставщика глицерина 99,5%, два тонны в месяц, регулярно.",
        "reply_to_message_id": None, "forwarded_from": None,
        "media_type": "photo" if i % 3 == 0 else None, "media_caption": None,
        "media_path": f"photos/1001/{i}.jpg" if i % 3 == 0 else None,
        "views": None, "link": "", "is_hidden": 0, "char_len": 0,
        "is_reply": 0, "is_forward": 0})
    db.rebuild_stat_cache(1001)

bot_ok = ctx.bot_manager.create_bot("Заявки из чатов", "userbot", None, "b2b", "@lead")
bot_bad = ctx.bot_manager.create_bot("Дежурный", "userbot", None, "custom", None)
db.set_bot_field(bot_ok, status="running")
db.set_bot_field(bot_bad, status="error", last_error="Не задан менеджер")
contact = db.upsert_contact(555, "irina", "Ирина")
db.add_lead(contact, bot_ok, {"company": "Аврора", "budget": "300 тыс"})
trig = db.add_trigger(bot_ok, "chat_message", {"chat_id": 1001, "keywords": ["куплю"]})
db.add_action(trig, "save_lead", {}, 0)

win = MainWindow(ctx)
win.resize(1320, 880)
win.show()

keys = [k for _, items in NAV_BY_BLOCK.items() for k, _ in items] + [k for k, _ in COMMON_ITEMS]
failed = []
for key in keys:
    try:
        win.navigate(key)
        app.processEvents()
        app.processEvents()
        print(f"OK:   {key}")
    except Exception:
        failed.append(key)
        print(f"FAIL: {key}")
        traceback.print_exc()

if failed:
    print("\nЭКРАНЫ С ОШИБКАМИ:", ", ".join(failed))
    sys.exit(1)
print(f"\nВСЕ {len(keys)} ЭКРАНОВ ОТКРЫВАЮТСЯ")
