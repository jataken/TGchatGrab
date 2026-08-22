"""П9: письмо → заявка в почтовой воронке. Проверяется: миграция 020
сидирует «Почта · прямой запрос» с правильными кодами/kind, создание
заявки из письма (и идемпотентность — повторный вызов на ту же цепочку
не плодит вторую заявку), автоматические предложения полей (регексом по
телу и таблицей из xlsx/csv-вложения, с приоритетом таблицы над
регексом при пересечении), направление по сработавшему ключевому слову,
цикл напоминания «мы ждём ответа» → «мы ответили — гасим», и, офскрин,
сама UI-часть: MailLeadDialog, кнопка заявки в MessagePane, «L» в
TriageDialog, экран «Почта → Заявки».
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _bootstrap import fresh_env
from chatgrab.core import lead as lead_domain
from chatgrab.core import mail_lead_extract
from chatgrab.integrations.mail import credentials as mail_credentials
from chatgrab.services.mail_service import MailService

paths, db, config, security = fresh_env("cgmailtolead")

mail_service = MailService(db, paths, security)

mailbox_id = db.add_mailbox("sales@company.ru", "imap.company.test", 993,
                             password_enc=mail_credentials.encrypt_password(security, "correct-password"))


def _make_message(uid: int, subject: str, sender_address: str, sender_name: str, body_text: str,
                   thread_id: int | None = None) -> int:
    message_id = db.upsert_mail_message(
        mailbox_id, "INBOX", uid,
        subject=subject, sender_name=sender_name, sender_address=sender_address,
        to_addresses="[]", date="2026-08-17T10:00:00+03:00",
        message_id=f"<msg{uid}@test>",
    )
    db.set_mail_message_body(message_id, body_text, None, False)
    if thread_id is not None:
        db.set_message_thread(message_id, thread_id)
    return message_id


# ---- pure functions: core/mail_lead_extract.py -------------------------
print("== core/mail_lead_extract.py: чистые функции ==")
assert mail_lead_extract.extract_phone("звоните +7 (912) 345-67-89 в любое время") == "+7 (912) 345-67-89"
assert mail_lead_extract.extract_phone("тут номера нет") is None
assert mail_lead_extract.extract_inn("ИНН 7701234567, счёт прилагаю") == "7701234567"
assert mail_lead_extract.extract_inn("сумма 12345") is None  # 5 цифр — не ИНН
assert mail_lead_extract.extract_volume("нужно 2 тонны глицерина") == ("2", "тонны")
assert mail_lead_extract.extract_volume("ничего похожего") is None
fields = mail_lead_extract.extract_body_fields(
    "Здравствуйте, нужно 3,5 тонны, ИНН 7701234567, тел. 8 912 345 67 89")
assert fields == {"phone": "8 912 345 67 89", "inn": "7701234567", "volume": "3,5", "unit": "тонны"}, fields
print("  ok —", fields)

print("\n== extract_table_fields: заголовок из ≥2 колонок, первая непустая строка данных ==")
grid_ok = [
    ["дата", "", ""],
    ["Наименование", "Кол-во", "Срок поставки"],
    ["", "", ""],
    ["Глицерин пищевой", "2 тонны", "до 01.09"],
]
assert mail_lead_extract.extract_table_fields(grid_ok) == {
    "product": "Глицерин пищевой", "volume": "2 тонны", "deadline": "до 01.09"}
grid_weak = [["Срок", "х", "у"]]  # одна колонка совпала — этого мало
assert mail_lead_extract.extract_table_fields(grid_weak) == {}
assert mail_lead_extract.extract_table_fields([]) == {}
print("  ok")


# ---- migration 020: воронка + этапы -------------------------------------
print("\n== migration 020: почтовая воронка засеяна с нужными кодами/kind ==")
mail_funnel = db.get_funnel_by_channel(lead_domain.ORIGIN_CHANNEL_EMAIL)
assert mail_funnel is not None
assert mail_funnel["name"] == "Почта · прямой запрос"
mail_stages = db.list_funnel_stages(mail_funnel["id"])
assert [s["code"] for s in mail_stages] == ["new", "qualified", "quote_sent", "invoiced", "shipped", "lost"]
assert [s["kind"] for s in mail_stages] == ["open", "open", "open", "open", "won", "lost"]
lost_stage = next(s for s in mail_stages if s["code"] == "lost")
assert lost_stage["requires_reason"] == 1
# telegram-воронка не тронута этой миграцией
tg_funnel = db.default_funnel_id()
assert tg_funnel != mail_funnel["id"]
print("  ok —", mail_funnel["id"], [s["code"] for s in mail_stages])

print("\n== mail_thread/mail_message получили lead_id, по умолчанию NULL ==")
thread_id = db.create_mail_thread(mailbox_id, "zapros kp")
msg1 = _make_message(1, "Запрос КП", "ivan@buyer.ru", "Иван Петров",
                      "Здравствуйте, нужно 2 тонны глицерина, тел. 8 912 345 67 89.", thread_id)
assert db.get_mail_thread(thread_id)["lead_id"] is None
assert db.get_mail_message(msg1)["lead_id"] is None
print("  ok")


# ---- create_lead_from_message: создание + идемпотентность ---------------
print("\n== create_lead_from_message: заявка уходит в первый открытый этап почтовой воронки ==")
lead_id = mail_service.create_lead_from_message(msg1)
assert lead_id is not None
lead = db.get_lead(lead_id)
assert lead["funnel_id"] == mail_funnel["id"]
assert lead["origin_channel"] == lead_domain.ORIGIN_CHANNEL_EMAIL
assert lead["source_type"] == lead_domain.SOURCE_TYPE_EMAIL
assert lead["status"] == "new"
assert lead["email"] == "ivan@buyer.ru"
assert lead["display_name"] == "Иван Петров"
events = db.list_lead_events(lead_id)
assert any(e["kind"] == "note" and "2 тонны глицерина" in (e["text"] or "") for e in events), events
assert db.get_mail_thread(thread_id)["lead_id"] == lead_id
assert db.get_mail_message(msg1)["lead_id"] == lead_id
print("  ok —", lead["status"], lead["email"])

print("\n== идемпотентность: второе письмо в ту же цепочку не плодит вторую заявку ==")
msg2 = _make_message(2, "Re: Запрос КП", "ivan@buyer.ru", "Иван Петров",
                      "Уточняю — нужно ещё и 500 кг сорбита.", thread_id)
# set_message_thread() уже должен был проставить lead_id со связанной цепочки
assert db.get_mail_message(msg2)["lead_id"] == lead_id, "реплай в уже привязанной цепочке должен унаследовать lead_id"
lead_id_again = mail_service.create_lead_from_message(msg2)
assert lead_id_again == lead_id
all_email_leads = db.list_leads(funnel_id=mail_funnel["id"])
assert len(all_email_leads) == 1, all_email_leads
print("  ok — заявка одна на всю цепочку")


# ---- suggest_lead_fields: тело + таблица из вложения ---------------------
print("\n== suggest_lead_fields: тело письма (регекс) ==")
proposals = mail_service.suggest_lead_fields(msg1)
assert proposals.get("phone") == "8 912 345 67 89"
assert proposals.get("volume") == "2"
assert proposals.get("unit") == "тонны"
print("  ok —", proposals)

print("\n== suggest_lead_fields: таблица из .csv-вложения перекрывает регекс по объёму ==")
import csv
csv_path = paths.data_dir / "test_lead_table.csv"
with open(csv_path, "w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f, delimiter=";")
    writer.writerow(["Наименование", "Кол-во", "Ед.", "Срок"])
    writer.writerow(["Сорбит пищевой", "500", "кг", "до 05.09"])
db.add_mail_attachment(msg1, "spec.csv", "text/csv", csv_path.stat().st_size, str(csv_path))
proposals2 = mail_service.suggest_lead_fields(msg1)
assert proposals2.get("product") == "Сорбит пищевой", proposals2
assert proposals2.get("volume") == "500", "таблица должна была перекрыть регексное «2» из тела"
assert proposals2.get("unit") == "кг"
assert proposals2.get("phone") == "8 912 345 67 89", "регекс по телу остаётся нетронутым там, где таблица не покрывает поле"
print("  ok —", proposals2)

print("\n== suggest_lead_fields: PDF/Word — нет пути к таблице, только регекс по телу ==")
pdf_path = paths.data_dir / "test_lead.pdf"
pdf_path.write_bytes(b"%PDF-fake, not a real parse target")
msg3 = _make_message(3, "КП вложением", "petr@buyer.ru", "Пётр Сидоров",
                      "Звоните 8 903 111 22 33, ИНН 7701234567.")
db.add_mail_attachment(msg3, "kp.pdf", "application/pdf", pdf_path.stat().st_size, str(pdf_path))
proposals3 = mail_service.suggest_lead_fields(msg3)
assert proposals3 == {"phone": "8 903 111 22 33", "inn": "7701234567"}, proposals3
print("  ok — .pdf пропущен, вернулся только регекс по телу:", proposals3)


# ---- matched_direction_id ------------------------------------------------
print("\n== matched_direction_id: направление по сработавшему ключевому слову ==")
direction_id = db.add_direction("Глицерин", keywords=["глицерин"])
db.add_direction("Сорбит", keywords=["сорбит"])
assert mail_service.matched_direction_id(msg1) == direction_id
msg_no_match = _make_message(4, "Просто привет", "x@y.ru", "X", "без ключевых слов вообще")
assert mail_service.matched_direction_id(msg_no_match) is None
print("  ok")


# ---- напоминание «мы ждём ответа» / «мы ответили» -------------------------
print("\n== цикл напоминания: письмо клиента ставит, наш ответ гасит ==")
mail_service._maybe_start_reply_reminder(msg1)
lead = db.get_lead(lead_id)
assert lead["next_action_text"] == mail_service._REPLY_REMINDER_TEXT
assert lead["next_action_at"] is not None
print("  ok — поставлено:", lead["next_action_at"])

mail_service._clear_reply_reminder(thread_id)
lead = db.get_lead(lead_id)
assert lead["next_action_at"] is None
assert lead["next_action_text"] is None
print("  ok — снято нашим ответом")

print("\n== напоминание не своё — не трогаем ==")
db.set_lead_field(lead_id, next_action_at="2026-09-01T10:00:00+03:00", next_action_text="перезвонить в понедельник")
mail_service._clear_reply_reminder(thread_id)
lead = db.get_lead(lead_id)
assert lead["next_action_text"] == "перезвонить в понедельник", "нельзя гасить напоминание, поставленное вручную"
db.set_lead_field(lead_id, next_action_at=None, next_action_text=None)
print("  ok — ручное напоминание не тронуто")


# ==== UI офскрин ============================================================
print("\n== UI офскрин: MailLeadDialog, кнопка в MessagePane, «L» в TriageDialog, экран «Заявки» ==")
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QDialog
app = QApplication.instance() or QApplication(sys.argv)

from chatgrab.ui.screens.mail import MessagePane
from chatgrab.ui.screens.mail.lead_create_dialog import MailLeadDialog
from chatgrab.ui.screens.mail.leads_screen import MailLeadsScreen
import chatgrab.ui.screens.mail.triage as triage_module


class _StubCtx:
    def __init__(self, database, service, sec):
        self.db = database
        self.mail_service = service
        self.security = sec


stub_ctx = _StubCtx(db, mail_service, security)

print("\n-- MailLeadDialog: предложения из тела предзаполнены и снимаемы --")
msg5 = _make_message(5, "Запрос КП №2", "new@buyer.ru", "Новый Клиент",
                      "Добрый день, нужно 7 тонн, тел. 8 999 222 33 44.")
dlg = MailLeadDialog(stub_ctx, msg5, parent=None)
assert "phone" in dlg._proposal_checks
assert dlg._proposal_checks["phone"].isChecked()
dlg._proposal_checks["phone"].setChecked(False)
dlg._on_create()
new_lead = db.get_lead(dlg.lead_id)
assert new_lead["phone"] is None, "снятая галочка не должна была попасть в заявку"
assert new_lead["volume"] == "7"
assert new_lead["funnel_id"] == mail_funnel["id"]
print("  ok — заявка создана, снятое предложение действительно не применилось")

print("\n-- MessagePane: кнопка «Завести заявку» -> «Открыть заявку» --")
msg6 = _make_message(6, "Ещё запрос", "another@buyer.ru", "Другой Клиент", "просто текст")
pane = MessagePane(stub_ctx, db.get_mail_message(msg6), on_need_body=lambda *a, **kw: None)
assert pane.lead_btn.text() == "Завести заявку"
another_thread = db.get_mail_thread(db.get_mail_message(msg6)["thread_id"])
if another_thread is None:
    # upsert_mail_message без явного thread_id не создаёт цепочку сам по
    # себе (это делает mail_thread.py при живой синхронизации) — здесь
    # цепочка создаётся вручную, тем же способом, что и выше.
    tid = db.create_mail_thread(mailbox_id, "another")
    db.set_message_thread(msg6, tid)
    pane.message = db.get_mail_message(msg6)
mail_service.create_lead_from_message(msg6)
pane._update_lead_button()
assert pane.lead_btn.text() == "Открыть заявку"
print("  ok")

print("\n-- TriageDialog: «L» открывает MailLeadDialog для текущей цепочки --")
opened = {}


class _StubMailLeadDialog:
    def __init__(self, ctx, message_id, parent=None):
        opened["message_id"] = message_id

    def exec(self):
        opened["exec_called"] = True
        return QDialog.Accepted


# Свежая, ничем не связанная цепочка — msg5's уже с заявкой (см. тест
# MailLeadDialog выше), для этого сценария нужна именно «пустая».
msg7 = _make_message(7, "Совсем новый запрос", "fresh@buyer.ru", "Свежий Клиент", "просто текст без вложений")
thread_fresh = db.create_mail_thread(mailbox_id, "fresh")
db.set_message_thread(msg7, thread_fresh)

import chatgrab.ui.screens.mail.lead_create_dialog as lead_create_dialog_module
original_real_cls = lead_create_dialog_module.MailLeadDialog
lead_create_dialog_module.MailLeadDialog = _StubMailLeadDialog
try:
    triage_dlg = triage_module.TriageDialog(stub_ctx, mailbox_id)
    triage_dlg._queue = [thread_fresh]
    triage_dlg._index = 0
    triage_dlg._render_current()
    event = QKeyEvent(QEvent.KeyPress, Qt.Key_L, Qt.NoModifier)
    triage_dlg.keyPressEvent(event)
    assert opened.get("exec_called"), "L должна была открыть диалог заявки"
    assert opened.get("message_id") is not None
finally:
    lead_create_dialog_module.MailLeadDialog = original_real_cls
print("  ok — L на цепочке без заявки открыл MailLeadDialog")

print("\n-- TriageDialog: «L» на уже привязанной цепочке открывает карточку заявки --")
import chatgrab.ui.screens.bots.lead_card as lead_card_module
original_card_cls = lead_card_module.LeadCardDialog
card_opened = {}


class _StubLeadCardDialog:
    def __init__(self, ctx, lead_id, parent=None):
        card_opened["lead_id"] = lead_id

    def exec(self):
        card_opened["exec_called"] = True
        return QDialog.Accepted


lead_card_module.LeadCardDialog = _StubLeadCardDialog
try:
    triage_dlg._queue = [thread_id]
    triage_dlg._index = 0
    triage_dlg._render_current()
    event = QKeyEvent(QEvent.KeyPress, Qt.Key_L, Qt.NoModifier)
    triage_dlg.keyPressEvent(event)
    assert card_opened.get("lead_id") == lead_id
    assert card_opened.get("exec_called")
finally:
    lead_card_module.LeadCardDialog = original_card_cls
print("  ok — L на уже привязанной цепочке открыл LeadCardDialog для правильной заявки")

print("\n-- LeadCardDialog: вкладка «Переписка» для email-заявки читает mail_thread, не Telegram --")
lead_card_module.LeadCardDialog = original_card_cls
card = lead_card_module.LeadCardDialog(stub_ctx, lead_id, parent=None)
assert "тонны глицерина" in card.corr_view.toPlainText() or "500 кг сорбита" in card.corr_view.toPlainText(), \
    card.corr_view.toPlainText()
print("  ok —", card.corr_hint.text())

print("\n-- MailLeadsScreen: своя воронка, свой список, телеграмные заявки не попадают --")
tg_lead_id = db.add_lead(None, None, {"text": "телеграм-заявка"}, status="new",
                          source_type=lead_domain.SOURCE_TYPE_MANUAL)
screen = MailLeadsScreen(stub_ctx, lambda *a, **kw: None)
screen.on_show()
assert screen._funnel_id == mail_funnel["id"]
shown_ids = set(screen.rows.keys())
assert tg_lead_id not in shown_ids, "телеграмная заявка не должна попасть на почтовый экран"
assert lead_id in shown_ids
print("  ok —", len(shown_ids), "заявок на экране, телеграмная заявка отфильтрована")

print("\n== всё сошлось ==")
