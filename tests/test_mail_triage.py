"""П7: скоринг писем — core/mail_triage.py в чистом виде (веса, категории,
причины), интеграция с синком/очередью (частичный балл по заголовкам сразу,
полный после fetch_body()), защита от лавины уведомлений, «Пересчитать»,
и офскрин UI: экран «Почта → Разбор» и клик по уведомлению открывает нужную
цепочку.
"""
import asyncio
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _bootstrap import fresh_env
from _fake_imap import make_client_factory
from chatgrab.core import mail_triage as mt
from chatgrab.integrations.mail import credentials as mail_credentials
from chatgrab.services.mail_service import MailService

paths, db, config, security = fresh_env("cgmailtriage")


def _msg(message_id, subject, sender, to, date, body="Текст.", extra_headers=""):
    headers = (
        f"From: {sender}\r\nTo: {to}\r\nSubject: {subject}\r\nDate: {date}\r\n"
        f"Message-ID: <{message_id}>\r\n{extra_headers}"
        f"Content-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n"
    )
    return headers.encode("utf-8")


async def _wait_for(predicate, timeout=5.0, step=0.05):
    elapsed = 0.0
    while not predicate() and elapsed < timeout:
        await asyncio.sleep(step)
        elapsed += step
    assert predicate(), f"условие не выполнилось за {timeout}с"


def _reasons(row) -> list[str]:
    """mail_message.triage_reasons is stored as JSON text (see
    db.set_message_triage) — decode before treating it as a list, unlike
    what mail_triage.score() itself returns straight from Python."""
    return json.loads(row["triage_reasons"] or "[]")


# ==== core/mail_triage.py — чистая функция, без базы ==========================
print("== normalize(): дефолты, клампинг, некорректный ввод не роняет ==")
d = mt.normalize(None)
assert d["threshold"] == 50 and d["max_notifications_per_tick"] == 5
assert d["weights"]["direction_keyword"] == 40
custom = mt.normalize({"weights": {"direction_keyword": 999, "bulk_signal": "не число"},
                        "threshold": "60", "max_notifications_per_tick": 0, "llm_borderline_enabled": 1})
print("  ", custom)
assert custom["weights"]["direction_keyword"] == 100, "должно быть заклампено в _WEIGHT_BOUNDS"
assert custom["weights"]["bulk_signal"] == -40, "нечисловое значение — остаётся дефолт"
assert custom["threshold"] == 60
assert custom["max_notifications_per_tick"] == 1, "0 меньше нижней границы (1) — заклампено"
assert custom["llm_borderline_enabled"] is True
print("  ok")


print("\n== score(): ключевое слово направления + запросный оборот, категория «запрос» ==")
directions = [{"name": "Глицерин", "keywords": ["глицерин"], "stop_words": ["вакансия"]}]
settings = mt.normalize({})
fields = {
    "subject": "Запрос КП", "body_text": "Нужен глицерин, какая стоимость?",
    "sender_address": "irina@client.ru", "has_list_unsubscribe": False, "is_bulk_precedence": False,
    "attachment_filenames": [], "known_sender": False, "reply_in_thread": False,
}
score, category, reasons = mt.score(fields, "", directions, settings)
print("  ", score, category, reasons)
assert score == 40 + 25
assert category == mt.CATEGORY_REQUEST
assert len(reasons) == 2

print("\n== score(): стоп-слово перевешивает, причина видна ==")
stop_fields = dict(fields, body_text="Вакансия менеджера, но мы и глицерин продаём")
score2, category2, reasons2 = mt.score(stop_fields, "", directions, settings)
print("  ", score2, reasons2)
assert score2 == 40 - 30
assert any("вакансия" in r for r in reasons2)

print("\n== score(): рассылка с List-Unsubscribe не проходит порог даже с ключевым словом ==")
bulk_fields = dict(fields, has_list_unsubscribe=True)
score3, category3, reasons3 = mt.score(bulk_fields, "", directions, settings)
print("  ", score3, category3)
assert score3 == 40 + 25 - 40, "ключевое слово, запросный оборот и рассылка все сработали разом"
assert score3 < settings["threshold"]
assert category3 == mt.CATEGORY_BULK, "рассылка перебивает категорию, даже если ключевое слово тоже сработало"

print("\n== score(): no-reply адрес — тоже рассылка, независимо от заголовков ==")
noreply_fields = dict(fields, sender_address="no-reply@shop.ru", has_list_unsubscribe=False)
score4, category4, _ = mt.score(noreply_fields, "", directions, settings)
assert score4 == 40 + 25 - 35
assert category4 == mt.CATEGORY_BULK
print("  ok")

print("\n== score(): known_sender/reply_in_thread — предвычисленные булевы поля ==")
known_fields = dict(fields, subject="Здравствуйте", body_text="", known_sender=True, reply_in_thread=True)
score5, category5, reasons5 = mt.score(known_fields, "", [], settings)
print("  ", score5, reasons5)
assert score5 == 15 + 15
assert category5 == mt.CATEGORY_OTHER, "оба сигнала — не запросные обороты и не ключевое слово"
assert len(reasons5) == 2

print("\n== score(): вложение похоже на заявку — по имени файла или по расширению таблицы ==")
att_fields = dict(fields, subject="", body_text="", attachment_filenames=["Спецификация.docx"])
s, c, r = mt.score(att_fields, "", [], settings)
assert s == 20 and "Спецификация.docx" in r[0]
att_fields2 = dict(fields, subject="", body_text="", attachment_filenames=["price_2026.xlsx"])
s2, c2, r2 = mt.score(att_fields2, "", [], settings)
assert s2 == 20, "расширение таблицы тоже считается признаком заявки"
print("  ok")

print("\n== score(): категории «заказ» и «счёт» ==")
order_fields = dict(fields, subject="Хочу оформить заказ", body_text="", attachment_filenames=[])
_, order_cat, _ = mt.score(order_fields, "", [], settings)
assert order_cat == mt.CATEGORY_ORDER
invoice_fields = dict(fields, subject="Счёт на оплату №42", body_text="", attachment_filenames=[])
_, invoice_cat, _ = mt.score(invoice_fields, "", [], settings)
assert invoice_cat == mt.CATEGORY_INVOICE
print("  ok")

print("\n== score(): причина показывает применённый (не дефолтный) вес ==")
custom_settings = mt.normalize({"weights": {"direction_keyword": 77}})
s3, _, r3 = mt.score(fields, "", directions, custom_settings)
print("  ", r3)
assert "(+77)" in r3[0], "причина должна отражать реально применённый вес"
assert s3 == 77 + 25
print("  ok")

print("\nТЕСТ ПРОЙДЕН (core): скоринг, категории и причины работают без базы")


# ==== интеграция: синк, частичный/полный балл, лавина, rescan =================
print("\n== ящик: сообщение получает частичный балл сразу (по заголовкам), полный — после чтения ==")
state = {
    "imap.triage.test": {
        "INBOX": {"uidvalidity": 1, "messages": {
            1: _msg("t1@x.ru", "Запрос КП на глицерин", "irina@x.ru", "a@triage.test",
                     "Mon, 17 Aug 2026 10:00:00 +0300", body="Какая стоимость за тонну?"),
        }},
        "Sent": {"uidvalidity": 1, "messages": {}, "special_use": "Sent"},
    },
}
hits = []
svc = MailService(db, paths, security, client_factory=make_client_factory(state),
                   on_triage_hit=lambda m, s, c, r: hits.append((m["id"], s, c, r)))
db.add_direction("Глицерин", keywords=["глицерин"], stop_words=[])
# Порог ниже дефолтного 50 нарочно: заголовочного балла (40, только тема)
# по умолчанию не хватило бы, чтобы показать разницу «частично при синке —
# полностью после чтения» без затемнения её порогом уведомления.
svc.set_triage_settings({"threshold": 40})
mb = db.add_mailbox("a@triage.test", "imap.triage.test", 993,
                     password_enc=mail_credentials.encrypt_password(security, "correct-password"))
asyncio.run(svc.tick())
db.set_mail_folder_state(mb, "Sent", enabled=True)  # только теперь папка уже обнаружена синком
m1 = db.get_mail_message_by_uid(mb, "INBOX", 1)
print("  сразу после синка:", m1["triage_score"], _reasons(m1))
assert m1["triage_score"] == 40, "тело ещё не загружено — только сигнал по теме"

print("\n== письмо выше порога уведомляет через on_triage_hit сразу при синке (по заголовкам) ==")
print("  ", hits)
assert len(hits) == 1, "балл по одним заголовкам (40) уже очистил заниженный для теста порог (40)"
assert hits[0][0] == m1["id"] and hits[0][1] == 40 and hits[0][2] == mt.CATEGORY_REQUEST

svc.fetch_body(m1["id"])
m1 = db.get_mail_message(m1["id"])
print("\n  после чтения письма:", m1["triage_score"], _reasons(m1))
assert m1["triage_score"] == 40 + 25, "после fetch_body — балл за оборот «стоимость» тоже учтён"
assert len(hits) == 1, "чтение уже открытого письма не должно порождать второе уведомление"
svc.set_triage_settings({"threshold": 50})  # возвращаем дефолт для остальных сцен теста


print("\n== рассылка (List-Unsubscribe) не уведомляет ==")
hits.clear()
state["imap.triage.test"]["INBOX"]["messages"][2] = _msg(
    "t2@x.ru", "Скидки недели!", "promo@bulk.ru", "a@triage.test",
    "Mon, 17 Aug 2026 10:05:00 +0300", body="Не пропустите!",
    extra_headers="List-Unsubscribe: <mailto:x@x.ru>\r\n")
asyncio.run(svc.tick())
print("  уведомлений:", len(hits))
assert hits == []


print("\n== защита от лавины: не больше max_notifications_per_tick за один тик ==")
svc.set_triage_settings({"max_notifications_per_tick": 2, "threshold": 40})
hits.clear()
for i in range(3, 8):
    state["imap.triage.test"]["INBOX"]["messages"][i] = _msg(
        f"t{i}@x.ru", "Запрос КП на глицерин", f"client{i}@x.ru", "a@triage.test",
        f"Mon, 17 Aug 2026 11:0{i}:00 +0300", body="Стоимость?")
asyncio.run(svc.tick())
print("  уведомлений за тик:", len(hits), "(лимит 2)")
assert len(hits) == 2, "остальные должны быть отложены, не показаны все разом"
svc.set_triage_settings({"max_notifications_per_tick": 5, "threshold": 50})


print("\n== известный отправитель (email уже в заявках): +15, причина видна ==")
db.add_lead(None, None, {}, email="vip@partner.ru")
state["imap.triage.test"]["INBOX"]["messages"][20] = _msg(
    "t20@x.ru", "Добрый день", "vip@partner.ru", "a@triage.test",
    "Mon, 17 Aug 2026 12:00:00 +0300", body="Как обычно.")
asyncio.run(svc.tick())
m20 = db.get_mail_message_by_uid(mb, "INBOX", 20)
print("  ", m20["triage_score"], _reasons(m20))
assert m20["triage_score"] == 15
assert any("заявках" in r for r in _reasons(m20))


print("\n== ответ в цепочке, где мы уже писали (Sent), даёт +15 ==")
state["imap.triage.test"]["Sent"]["messages"][100] = _msg(
    "sent1@x.ru", "Про поставку", "a@triage.test", "buyer@x.ru",
    "Mon, 17 Aug 2026 09:00:00 +0300", body="Высылаем предложение.")
# Отдельным тиком — «Sent» синкается по алфавиту после «INBOX», так что
# наше письмо должно уже быть в базе (и иметь свою цепочку) к моменту,
# когда ответ на него появится, иначе find_reference_thread ещё не
# найдёт, к чему привязать входящий ответ.
asyncio.run(svc.tick())
state["imap.triage.test"]["INBOX"]["messages"][30] = _msg(
    "reply1@x.ru", "Re: Про поставку", "buyer@x.ru", "a@triage.test",
    "Mon, 17 Aug 2026 13:00:00 +0300", body="Спасибо, интересно.",
    extra_headers="In-Reply-To: <sent1@x.ru>\r\nReferences: <sent1@x.ru>\r\n")
asyncio.run(svc.tick())
m30 = db.get_mail_message_by_uid(mb, "INBOX", 30)
print("  ", m30["triage_score"], _reasons(m30))
assert m30["triage_score"] == 15
assert any("цепочке" in r for r in _reasons(m30))


print("\n== rescan_triage(): дозагружает тело для непрочитанных и пересчитывает ==")
state["imap.triage.test"]["INBOX"]["messages"][40] = _msg(
    "t40@x.ru", "Здравствуйте", "someone@x.ru", "a@triage.test",
    "Mon, 17 Aug 2026 14:00:00 +0300", body="Пришлите КП пожалуйста.")
asyncio.run(svc.tick())
m40_before = db.get_mail_message_by_uid(mb, "INBOX", 40)
assert m40_before["triage_score"] == 0, "тело ещё не загружено, тема без сигналов"
found = svc.rescan_triage(mb, limit=50)
m40_after = db.get_mail_message(m40_before["id"])
print("  до:", m40_before["triage_score"], "после rescan:", m40_after["triage_score"], "| найдено:", found)
assert m40_after["triage_score"] == 25, "запросный оборот «пришлите КП» из тела теперь учтён"
assert found >= 1


print("\n== preview_score(): не пишет в базу — только считает под черновыми весами ==")
before = db.get_mail_message(m1["id"])["triage_score"]
preview = svc.preview_score(m1["id"], {"weights": {"direction_keyword": 1}})
after = db.get_mail_message(m1["id"])["triage_score"]
print("  preview:", preview, "| в базе было и осталось:", before, after)
assert before == after, "preview_score не должен трогать сохранённый балл"
assert preview[0] != before, "но сам расчёт должен использовать переданные (черновые) веса"


print("\nТЕСТ ПРОЙДЕН (интеграция): частичный/полный балл, уведомления, лавина, известный "
      "отправитель, ответ в своей цепочке, rescan и preview работают")


# ==== UI офскрин: экран «Почта → Разбор», клик по уведомлению открывает цепочку ====
print("\n== UI офскрин: MailTriageScreen и клик по уведомлению ==")
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication(sys.argv)

from chatgrab.ui.screens.mail_triage import MailTriageScreen


class _StubCtx:
    def __init__(self, database, service):
        self.db = database
        self.mail_service = service


stub_ctx = _StubCtx(db, svc)
screen = MailTriageScreen(stub_ctx, lambda *a, **kw: None)
print("  вес «direction_keyword» в форме при открытии:", screen._weight_fields["direction_keyword"].value())
assert screen._weight_fields["direction_keyword"].value() == 40
assert screen.threshold_field.value() == 50
rows_before = screen.results_box.count()
print("  строк в «последние 50»:", rows_before)
assert rows_before > 0

screen._weight_fields["direction_keyword"].setValue(1)
screen._on_save()
saved = svc.get_triage_settings()
print("  после «Сохранить»:", saved["weights"]["direction_keyword"])
assert saved["weights"]["direction_keyword"] == 1
stored_after_save = db.get_mail_message(m1["id"])["triage_score"]
assert stored_after_save == 65, "«Сохранить» настройки не должно само по себе пересчитывать письма"

screen._weight_fields["direction_keyword"].setValue(40)
screen._on_save()
print("  ok — экран строится, показывает последние письма, сохраняет и не трогает старые баллы")


async def _rescan_via_screen():
    screen._on_rescan()
    await _wait_for(lambda: "Пересчитано" in screen.settings_status.text())


asyncio.run(_rescan_via_screen())
print("  «Пересчитать» из экрана:", screen.settings_status.text())
assert "Пересчитано" in screen.settings_status.text()


print("\n-- клик по уведомлению в трее открывает нужную цепочку --")
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
from chatgrab.ui.main_window import MainWindow

paths2, db2, config2, sec2 = fresh_env("cgmailtriagewin")
tg2 = TelegramService(config2)
# Свой, отдельный «сервер» — не переиспользуем `state`: к этому месту в
# нём уже десяток писем из интеграционного сценария выше, из-за которых
# при первом же синке сработал бы предел лавины, а не ровно одно письмо.
win_state = {
    "imap.win.test": {
        "INBOX": {"uidvalidity": 1, "messages": {
            1: _msg("win1@x.ru", "Запрос КП на глицерин", "irina@x.ru", "a@win.test",
                     "Mon, 17 Aug 2026 10:00:00 +0300", body="Какая стоимость?"),
        }},
    },
}
mail_svc2 = MailService(db2, paths2, sec2, client_factory=make_client_factory(win_state))
ctx2 = AppContext(
    config=config2, paths=paths2, db=db2, tg=tg2,
    collector=Collector(db2, tg2, config2, paths2),
    export_service=ExportService(db2, paths2), ignore_service=IgnoreService(db2),
    backup_service=BackupService(db2, paths2), security=sec2,
    bot_manager=BotManager(db2, tg2, sec2),
    watch_service=WatchService(db2), retention_service=RetentionService(db2, paths2),
    export_schedule_service=ExportScheduleService(db2, ExportService(db2, paths2)),
    lead_reminder_service=LeadReminderService(db2),
    bitrix_sync_service=BitrixSyncService(db2, sec2),
    mail_service=mail_svc2)
# asyncio.run() above (rescan test) tears its loop down on exit and
# clears the "current" one — MainWindow.__init__ fires an async startup
# task via fire()/ensure_future(), which needs a live loop set again.
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
win = MainWindow(ctx2)

notes2 = []


class _FakeTray:
    def setToolTip(self, t):
        self.tip = t

    def showMessage(self, title, text, icon, ms):
        notes2.append((title, text))


win.tray.tray = _FakeTray()

db2.add_direction("Глицерин", keywords=["глицерин"], stop_words=[])
# Порог занижен так же, как в интеграционном сценарии выше: заголовочного
# балла (40, тема без тела) хватает только под заниженный для теста порог.
ctx2.mail_service.set_triage_settings({"threshold": 40})
mb2 = db2.add_mailbox("a@win.test", "imap.win.test", 993,
                       password_enc=mail_credentials.encrypt_password(sec2, "correct-password"))
asyncio.run(ctx2.mail_service.tick())
target = db2.get_mail_message_by_uid(mb2, "INBOX", 1)
print("  уведомлений при первом синке:", len(notes2))
assert len(notes2) == 1, "балл по заголовкам (40) должен был очистить заниженный для теста порог (40)"
assert win.tray._last_message_click is not None

# asyncio.run() above (tick()) tore its loop down again — _on_thread_
# selected()'s _mark_read() fires another async push, same as the
# MainWindow construction fix above.
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
win.tray._on_message_clicked()
app.processEvents()
mail_screen = win.screens["mail"]
print("  выбранная цепочка на экране «Почта»:", mail_screen.selected_thread_id, "| ожидалась:", target["thread_id"])
assert mail_screen.selected_thread_id == target["thread_id"]
assert mail_screen.selected_mailbox_id == mb2
print("  ok — клик по уведомлению переключил экран на нужную цепочку")

print("\nТЕСТ ПРОЙДЕН (UI): экран «Почта → Разбор» строится и работает, клик по уведомлению открывает цепочку")
