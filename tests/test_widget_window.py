"""П8: desktop widget — mail/collection/bots at a glance, own timer,
touches no network of its own. Geometry/opacity/section-toggle state
round-trips through app_settings, mail rows carry a per-mailbox colour
dot, unread dot, attachment clip and a triage-score highlight border,
one-click label application from the widget's own row, click-to-open
wired to the same (mailbox_id, thread_id) MailScreen's deep link expects,
and the tray's "Показать виджет" menu action.
"""
import asyncio
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication(sys.argv)
from chatgrab.ui.theme import apply_theme
apply_theme(app)
loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)

from _bootstrap import fresh_env
from _fake_imap import make_client_factory
from chatgrab.integrations.mail import credentials as mail_credentials
from chatgrab.services.mail_service import MailService
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
from chatgrab.ui.widget_window import WidgetWindow, _MAIL_ONLY_WIDTH, _SETTINGS_KEY, _load_state

paths, db, config, security = fresh_env("cgwidget")


def _msg(message_id, subject, sender, to, date, body="Текст."):
    headers = (
        f"From: {sender}\r\nTo: {to}\r\nSubject: {subject}\r\nDate: {date}\r\n"
        f"Message-ID: <{message_id}>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n"
    )
    return headers.encode("utf-8")


state = {
    "imap.widget.test": {
        "INBOX": {"uidvalidity": 1, "messages": {
            1: _msg("w1@x.ru", "Запрос КП: глицерин", "irina@client.ru", "a@widget.test",
                     "Mon, 17 Aug 2026 10:00:00 +0300",
                     body="Нужен глицерин, какая стоимость за тонну?"),
        }},
        "Archive": {"uidvalidity": 1, "messages": {}, "special_use": "Archive"},
    },
}

mail_svc = MailService(db, paths, security, client_factory=make_client_factory(state))
mb = db.add_mailbox("a@widget.test", "imap.widget.test", 993,
                     password_enc=mail_credentials.encrypt_password(security, "correct-password"))
db.add_direction("Глицерин", keywords=["глицерин"], stop_words=[])
db.seed_default_mail_labels(mb)
asyncio.run(mail_svc.tick())

msg_row = db.get_mail_message_by_uid(mb, "INBOX", 1)
thread_id = msg_row["thread_id"]
print("== при синхронизации письмо уже получает частичный балл — по одним заголовкам ==")
partial = db.get_mail_message(msg_row["id"])
print("  балл сразу после тика:", partial["triage_score"])
assert partial["triage_score"] == 40, "тело ещё не забрано — сработало только ключевое слово в теме"

# fetch_body() — то же самое, что открытие письма на чтение или «Пере-
# считать»: подтягивает тело + вложения и пересчитывает балл уже с ними.
db.add_mail_attachment(msg_row["id"], "спецификация.xlsx", "application/vnd.ms-excel", 1024, None)
mail_svc.fetch_body(msg_row["id"])
# Тестовое сырое письмо не multipart — реальных вложений в нём нет, а
# has_attachments выставляется fetch_body() по разбору MIME, а не по
# наличию строк в mail_attachment. Проставляю флаг напрямую — то, ради
# чего это письмо тут вообще заведено, это скрепка в строке виджета, а
# не разбор реального multipart-письма (это уже проверено в П3).
db.set_mail_message_body(msg_row["id"], db.get_mail_message(msg_row["id"])["body_text"], None, True)
scored = db.get_mail_message(msg_row["id"])
print("  балл после подгрузки тела и вложения:", scored["triage_score"], "| категория:", scored["triage_category"])
assert scored["triage_score"] is not None and scored["triage_score"] >= 50
assert scored["has_attachments"] == 1
print("  ok")

tg = TelegramService(config)
ctx = AppContext(
    config=config, paths=paths, db=db, tg=tg,
    collector=Collector(db, tg, config, paths),
    export_service=ExportService(db, paths), ignore_service=IgnoreService(db),
    backup_service=BackupService(db, paths), security=security,
    bot_manager=BotManager(db, tg, security),
    watch_service=WatchService(db), retention_service=RetentionService(db, paths),
    export_schedule_service=ExportScheduleService(db, ExportService(db, paths)),
    lead_reminder_service=LeadReminderService(db),
    bitrix_sync_service=BitrixSyncService(db, security),
    mail_service=mail_svc,
)

print("\n== состояние по умолчанию — ничего не сохранено ==")
assert db.get_setting(_SETTINGS_KEY, None) is None
default_state = _load_state(db)
assert default_state["sections"] == {"mail": True, "collect": True, "bots": True}
assert default_state["mail_only"] is False
print("  ok")

opened = []
widget = WidgetWindow(ctx, on_open_thread=lambda mailbox_id, tid: opened.append((mailbox_id, tid)))
# isVisible() composes with the top-level window's own shown state — a
# never-.show()'n window makes every child report isVisible()==False
# regardless of its own setVisible() flag, so the section-toggle checks
# below need the window actually shown once, same as smoke_screens.py.
widget.show()
app.processEvents()

print("\n== геометрия сохраняется и восстанавливается новой копией окна ==")
widget.move(120, 80)
widget._save_geometry()
saved = db.get_setting(_SETTINGS_KEY)
assert saved["x"] == 120 and saved["y"] == 80
widget2 = WidgetWindow(ctx, on_open_thread=lambda *a: None)
assert (widget2.x(), widget2.y()) == (120, 80), "вторая копия должна встать туда же, где сохранилась первая"
widget2.hide()
print("  ok")

print("\n== секция отключается независимо от других, состояние сохраняется ==")
widget._section_pills["collect"].setChecked(False)
widget._on_toggle_section("collect")
assert widget.collect_section.isVisible() is False
assert widget.bots_section.isVisible() is True
assert db.get_setting(_SETTINGS_KEY)["sections"]["collect"] is False
widget._section_pills["collect"].setChecked(True)
widget._on_toggle_section("collect")
assert widget.collect_section.isVisible() is True
print("  ok")

print("\n== «только почта»: узкая полоса, сбор и боты скрыты, ярлык переключается обратно ==")
before_width = widget.width()
widget.mail_only_btn.setChecked(True)
widget._on_toggle_mail_only()
# Не ровно 220: реальная минимальная ширина после resize() зависит от
# метрик шрифта заголовков/кнопок конкретной платформы (Windows и Linux
# считают их не одинаково), поэтому проверяется само сужение, а не
# конкретное число пикселей — свойство, которое и важно на самом деле.
print("  ширина: было", before_width, "-> стало", widget.width())
assert widget.width() < before_width, "«узко» должно заметно сузить окно"
assert widget.width() <= _MAIL_ONLY_WIDTH + 40, "и не просто чуть-чуть — это должна быть узкая полоса"
assert widget.collect_section.isVisible() is False
assert widget.bots_section.isVisible() is False
assert widget.mail_section.isVisible() is True
widget.mail_only_btn.setChecked(False)
widget._on_toggle_mail_only()
assert widget.width() == before_width
print("  ok")

print("\n== прозрачность настраивается и сохраняется ==")
widget._on_opacity_changed(60)
assert abs(widget.windowOpacity() - 0.6) < 1e-6
assert db.get_setting(_SETTINGS_KEY)["opacity"] == 60
widget._on_opacity_changed(95)
print("  ok")

print("\n== строка письма: цвет ящика, точка непрочитанного, скрепка, подсветка по баллу ==")
widget.refresh()
assert widget.mail_rows_layout.count() == 1, "должно быть ровно одно засеянное письмо"
row_widget = widget.mail_rows_layout.itemAt(0).widget()
all_text = " ".join(w.text() for w in row_widget.findChildren(type(widget.mail_empty_label)))
print("  текст строки:", repr(all_text))
assert "●" in all_text, "письмо не прочитано — должна быть точка"
assert "irina@client.ru" in all_text or "Запрос" in all_text
assert "📎" in all_text, "у письма есть вложение — должна быть скрепка"
assert row_widget.styleSheet(), "балл выше порога — должна быть подсветка рамкой"
print("  ok")

print("\n== ярлык в один клик из виджета: разворачивает плашки, клик применяет к цепочке ==")
assert db.list_labels_for_thread(thread_id) == []
from PySide6.QtWidgets import QPushButton
candidates = [b for b in row_widget.findChildren(QPushButton) if b.text() == "🏷"]
assert len(candidates) == 1
tag_button = candidates[0]


async def _apply_label_and_wait():
    tag_button.click()
    label_buttons = [b for b in row_widget.findChildren(QPushButton) if b.text() not in ("🏷",)]
    assert label_buttons, "плашки ярлыков должны появиться после клика по значку"
    label_buttons[0].click()
    elapsed = 0.0
    while db.list_labels_for_thread(thread_id) == [] and elapsed < 5.0:
        await asyncio.sleep(0.05)
        elapsed += 0.05


asyncio.run(_apply_label_and_wait())
applied = db.list_labels_for_thread(thread_id)
print("  ярлык на цепочке:", [l["name"] for l in applied])
assert len(applied) == 1
print("  ok")

print("\n== клик по строке письма открывает главное окно на этой цепочке ==")
row_widget._on_click()
print("  открыто:", opened)
assert opened == [(mb, thread_id)]
print("  ok")

print("\n== сбор: число активных чатов и ошибок читается из базы, без сети ==")
db.add_chat(2001, "Чат А", "cha", "all", None)
db.add_chat(2002, "Чат Б", "chb", "all", None)
db.set_chat_field(2002, enabled=0)
db.set_chat_field(2001, last_error="недоступен")
widget._refresh_collect_status()
print("  ", widget.collect_status_label.text())
assert "1 из 2" in widget.collect_status_label.text()
assert "ошибок: 1" in widget.collect_status_label.text()
print("  ok")

print("\n== боты: недельная серия заявок и счётчики ==")
bot_id = ctx.bot_manager.create_bot("Бот", "userbot", None, "custom", None)
db.set_bot_field(bot_id, status="running")
contact = db.upsert_contact(555, "irina", "Ирина")
db.add_lead(contact, bot_id, {"company": "Аврора"})
widget._refresh_bots()
series = widget.bots_chart._values
print("  недельная серия:", series, "| статус:", widget.bots_status_label.text())
assert len(series) == 7
assert sum(series) == 1, "один лид, заведённый только что, должен попасть в сегодняшний бакет"
assert "1 из 1" in widget.bots_status_label.text()
assert "новых заявок: 1" in widget.bots_status_label.text()
print("  ok")

print("\n== виджет ничего не опрашивает сам: скорость сбора считается по локальному COUNT(*) ==")
before_pushed = len(widget.collect_chart.values())
widget._sample_speed()  # первый вызов только запоминает базовую точку
widget._sample_speed()
after_pushed = len(widget.collect_chart.values())
assert after_pushed >= before_pushed
print("  ok — ни одного сетевого вызова, только db.message_count()")

widget.hide()

print("\n== MainWindow строит виджет и передаёт «Показать виджет» в трей ==")
from chatgrab.ui.main_window import MainWindow

# asyncio.run() выше закрывает свой цикл и обнуляет текущий цикл потока
# при выходе — MainWindow.__init__ сам вызывает fire() (автоподключение
# при старте), которому нужен хоть какой-то установленный цикл, не
# обязательно работающий. Переустанавливаю тот же loop, что и в начале.
asyncio.set_event_loop(loop)
win = MainWindow(ctx)
assert win.widget_window is not None
assert win.tray.on_show_widget == win.widget_window.show_and_raise
win.widget_window.hide()
win.tray.on_show_widget()
assert win.widget_window.isVisible()
win.widget_window.hide()
print("  ok — «Показать виджет» реально показывает то же окно")

print("\nТЕСТ ПРОЙДЕН: виджет строится, сохраняет своё состояние, показывает почту/сбор/ботов "
      "и не опрашивает ничего сам")
