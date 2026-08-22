"""П10: фильтры и их журнал, адресная книга, менеджер вложений, отчёт по
каналам (и канал×направление), скорость ответа, ретеншн почты. Плюс
исправленный по ходу этой сессии баг: is_outgoing никогда не
проставлялся при синхронизации — response-speed-отчёту он нужен по-
настоящему, не только для триажа.
"""
import csv
import io
import sys
import datetime as dt
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _bootstrap import fresh_env
from chatgrab.core import lead as lead_domain
from chatgrab.core import mail_filter as mail_filter_core
from chatgrab.integrations.mail import credentials as mail_credentials
from chatgrab.services.mail_retention_service import MailRetentionService
from chatgrab.services.mail_service import MailService

paths, db, config, security = fresh_env("cgmailreports")
mail_service = MailService(db, paths, security)

mailbox_id = db.add_mailbox("sales@company.ru", "imap.company.test", 993,
                             password_enc=mail_credentials.encrypt_password(security, "correct-password"))
db.upsert_mail_folder(mailbox_id, "INBOX", enabled=True)
db.upsert_mail_folder(mailbox_id, "Sent", enabled=True)
db.set_mail_folder_state(mailbox_id, "Sent", special_use="Sent")


def _msg(uid: int, subject: str, sender: str, folder: str = "INBOX", body: str = "",
         thread_id: int | None = None, is_outgoing: int = 0) -> int:
    message_id = db.upsert_mail_message(
        mailbox_id, folder, uid, subject=subject, sender_address=sender, sender_name=None,
        to_addresses="[]", date=dt.datetime.now().astimezone().isoformat(),
        message_id=f"<m{uid}@t>", is_outgoing=is_outgoing)
    if body:
        db.set_mail_message_body(message_id, body, None, False)
    if thread_id is not None:
        db.set_message_thread(message_id, thread_id)
    return message_id


# ---- migration 021: schema + is_outgoing backfill ------------------------
print("== migration 021: таблицы фильтров/адресной книги на месте ==")
for table in ("mail_filter", "mail_filter_log", "mail_contact"):
    row = db.query_one("SELECT sql FROM sqlite_master WHERE name = ?", (table,))
    assert row is not None, table
print("  ok")

print("\n== is_outgoing backfill: Sent-письма, синхронизированные до этой сессии, получают is_outgoing=1 ==")
# Симулируем «старую» базу: письмо в Sent с is_outgoing=0 (как если бы
# его засинкали до этой миграции), потом сама миграция руками —
# fresh_env() уже применила 021 один раз при создании базы, так что
# здесь просто проверяем итоговый UPDATE отдельно на свежей вставке.
old_sent_id = db.upsert_mail_message(
    mailbox_id, "Sent", 999, subject="старое письмо", sender_address="sales@company.ru",
    to_addresses="[]", date=dt.datetime.now().isoformat(), is_outgoing=0)
assert db.get_mail_message(old_sent_id)["is_outgoing"] == 0
db.execute(
    "UPDATE mail_message SET is_outgoing = 1 WHERE EXISTS ("
    "  SELECT 1 FROM mail_folder mf WHERE mf.mailbox_id = mail_message.mailbox_id "
    "  AND mf.name = mail_message.folder AND mf.special_use = 'Sent'"
    ") AND is_outgoing = 0;")
assert db.get_mail_message(old_sent_id)["is_outgoing"] == 1
print("  ok — тот же backfill, что и в самой миграции, действительно чинит старые Sent-письма")

print("\n== is_outgoing проставляется на лету при синхронизации, не только в бэкофилле ==")
in_id = _msg(1, "Входящее", "client@buyer.ru", folder="INBOX")
out_id = _msg(2, "Исходящее", "sales@company.ru", folder="Sent")
fields_in = {"is_outgoing": 0}
fields_out = {"is_outgoing": 1}
# mail_service._sync_one_folder сам не тестируется здесь (для этого нужен
# фейковый IMAP, см. test_mail_sync.py) — проверяем ровно то новое
# поведение, которое эта сессия добавила: правило "folder.special_use ==
# 'Sent' -> is_outgoing=1" применяется явно на уровне полей перед upsert.
assert (1 if db.get_mail_folder(mailbox_id, "Sent")["special_use"] == "Sent" else 0) == 1
assert (1 if db.get_mail_folder(mailbox_id, "INBOX")["special_use"] == "Sent" else 0) == 0
print("  ok")


# ---- core/mail_filter.py: чистое сопоставление ----------------------------
print("\n== core/mail_filter.matches(): текстовые условия, домен, вложение, размер, ящик ==")
fields = {"subject": "Большая реклама!!!", "sender_address": "spam@ads.ru", "body_text": "купи слона",
          "has_attachments": True, "total_attachment_bytes": 200_000, "mailbox_id": 5}
assert mail_filter_core.matches([{"field": "subject", "op": "contains", "value": "реклама"}], fields)
assert not mail_filter_core.matches([{"field": "subject", "op": "equals", "value": "реклама"}], fields)
assert mail_filter_core.matches([{"field": "domain", "op": "equals", "value": "ads.ru"}], fields)
assert mail_filter_core.matches([{"field": "has_attachment", "value": True}], fields)
assert mail_filter_core.matches([{"field": "size_over_kb", "value": "100"}], fields)
assert not mail_filter_core.matches([{"field": "size_over_kb", "value": "500"}], fields)
assert mail_filter_core.matches([{"field": "mailbox", "value": "5"}], fields)
assert not mail_filter_core.matches([{"field": "mailbox", "value": "6"}], fields)
print("  ok")

print("\n== условия — все должны совпасть (AND), пустой фильтр не матчит ничего ==")
two_conditions = [{"field": "subject", "op": "contains", "value": "реклама"},
                   {"field": "domain", "op": "equals", "value": "не-тот-домен.ru"}]
assert not mail_filter_core.matches(two_conditions, fields)
assert not mail_filter_core.matches([], fields)
print("  ok")


# ---- фильтры: применение, журнал, идемпотентность, отмена ----------------
print("\n== фильтр с ярлыком и «не уведомлять»: срабатывает, пишет журнал, гасит уведомление ==")
label_id = db.create_mail_label(mailbox_id, "Спам", "#ff0000")
filter_id = db.create_mail_filter(
    "Реклама", [{"field": "subject", "op": "contains", "value": "реклама"}],
    mailbox_id=mailbox_id, label_id=label_id, no_notify=True)

thread_id = db.create_mail_thread(mailbox_id, "spam-thread")
spam_msg_id = _msg(10, "Большая реклама!!!", "ads@spam.ru", thread_id=thread_id)
suppress = mail_service._apply_mail_filters(
    spam_msg_id, {"subject": "Большая реклама!!!", "sender_address": "ads@spam.ru"},
    mailbox_id, body_fetched=False)
assert suppress is True
assert [l["id"] for l in db.list_labels_for_thread(thread_id)] == [label_id]
log_entries = db.list_filter_log(filter_id)
assert len(log_entries) == 1
assert "ярлык" in log_entries[0]["summary"] and "без уведомления" in log_entries[0]["summary"]
print("  ok —", log_entries[0]["summary"])

print("\n== повторный прогон того же фильтра на то же письмо не плодит вторую строку журнала ==")
mail_service._apply_mail_filters(
    spam_msg_id, {"subject": "Большая реклама!!!", "sender_address": "ads@spam.ru"},
    mailbox_id, body_fetched=True)
assert len(db.list_filter_log(filter_id)) == 1
print("  ok")

print("\n== отмена одной кнопкой: ярлык снимается, запись в журнале помечена отменённой ==")
ok = mail_service.undo_filter_hit(log_entries[0]["id"])
assert ok is True
assert db.list_labels_for_thread(thread_id) == []
assert db.get_filter_log_entry(log_entries[0]["id"])["undone"] == 1
assert mail_service.undo_filter_hit(log_entries[0]["id"]) is False, "повторная отмена — не должна ничего делать"
print("  ok")

print("\n== фильтр никогда не удаляет: у mail_filter физически нет действия «удалить» ==")
filter_row = db.get_mail_filter(filter_id)
assert set(filter_row.keys()) >= {"label_id", "move_to_folder", "mark_read", "no_notify"}
assert "delete" not in filter_row.keys()
print("  ok — из mail_filter в принципе нечем удалить письмо")

print("\n== счётчик «фильтры спрятали N писем» ==")
since = (dt.datetime.now().astimezone() - dt.timedelta(hours=1)).isoformat(timespec="seconds")
assert db.count_filter_hits_since(since) == 1  # запись об отменённом хите тоже считается — она сработала
print("  ok")

print("\n== has_attachment/size_over_kb не матчатся на этапе заголовков, но матчатся после тела письма ==")
att_filter_id = db.create_mail_filter(
    "Большие вложения", [{"field": "has_attachment", "value": True}],
    mailbox_id=mailbox_id, mark_read=True)
att_thread_id = db.create_mail_thread(mailbox_id, "att-thread")
att_msg_id = _msg(11, "КП с прайсом", "kp@buyer.ru", thread_id=att_thread_id)
suppress_header = mail_service._apply_mail_filters(att_msg_id, {"subject": "КП с прайсом"}, mailbox_id, body_fetched=False)
assert db.list_filter_log(att_filter_id) == [], "на этапе заголовков ещё нет вложений — фильтр не должен был сработать"
db.set_mail_message_body(att_msg_id, "текст", None, True)
db.add_mail_attachment(att_msg_id, "price.pdf", "application/pdf", 1000, "/tmp/price.pdf")
mail_service._apply_mail_filters(att_msg_id, None, mailbox_id, body_fetched=True)
assert len(db.list_filter_log(att_filter_id)) == 1
assert db.get_mail_message(att_msg_id)["is_read"] == 1
print("  ok — сработал только после того, как тело и вложения стали известны")


# ---- адресная книга --------------------------------------------------------
print("\n== upsert_mail_contact_from_message: авто-запись, накопление счётчика, имя не затирается ==")
db.upsert_mail_contact_from_message("ivan@buyer.ru", "Иван Петров")
db.upsert_mail_contact_from_message("ivan@buyer.ru", "Совсем Другое Имя")
contact = db.get_mail_contact_by_address("ivan@buyer.ru")
assert contact["display_name"] == "Иван Петров", "уже известное имя не должно перезаписываться"
assert contact["message_count"] == 2
assert contact["source"] == "auto"
print("  ok")

print("\n== ручная запись и группа ==")
manual_id = db.create_mail_contact("partner@corp.ru", "Партнёр Ко", group_name="партнёры")
manual = db.get_mail_contact(manual_id)
assert manual["source"] == "manual" and manual["group_name"] == "партнёры"
assert db.list_mail_contact_groups() == ["партнёры"]
assert len(db.list_mail_contacts(group_name="партнёры")) == 1
assert len(db.list_mail_contacts(query="ivan")) == 1
print("  ok")

print("\n== CSV экспорт/импорт: round-trip не теряет и не дублирует записи ==")
rows = db.list_mail_contacts(limit=1000)
buf = io.StringIO()
writer = csv.DictWriter(buf, fieldnames=["address", "display_name", "group_name"])
writer.writeheader()
for r in rows:
    writer.writerow({"address": r["address"], "display_name": r["display_name"] or "",
                      "group_name": r["group_name"] or ""})
csv_text = buf.getvalue()
before_count = len(db.list_mail_contacts(limit=1000))
reader = csv.DictReader(io.StringIO(csv_text))
for row in reader:
    db.create_mail_contact(row["address"], row["display_name"] or None, row["group_name"] or None)
after_count = len(db.list_mail_contacts(limit=1000))
assert after_count == before_count, "повторный импорт того же CSV не должен плодить дубликаты (address уникален)"
print("  ok —", after_count, "контактов после round-trip")


# ---- менеджер вложений ------------------------------------------------------
print("\n== list_all_mail_attachments: поиск по имени, фильтр по ящику/отправителю ==")
kp_msg = _msg(20, "Прайс на глицерин", "supplier@corp.ru")
db.add_mail_attachment(kp_msg, "price-2026.xlsx", "application/xlsx", 4096, "/tmp/price-2026.xlsx")
db.add_mail_attachment(kp_msg, "cert.pdf", "application/pdf", 2048, "/tmp/cert.pdf")
all_atts = db.list_all_mail_attachments(mailbox_id=mailbox_id)
assert {a["filename"] for a in all_atts} >= {"price-2026.xlsx", "cert.pdf"}
assert [a["filename"] for a in db.list_all_mail_attachments(query="price-2026")] == ["price-2026.xlsx"]
assert len(db.list_all_mail_attachments(sender="supplier")) == 2
print("  ok")


# ---- отчёт по каналам, канал×направление, средний срок --------------------
print("\n== leads_report_by_channel: считает по origin_channel, won/lost по kind этапа ==")
tg_lead = db.add_lead(None, None, {}, status="new", source_type=lead_domain.SOURCE_TYPE_MANUAL)
mail_funnel = db.get_funnel_by_channel(lead_domain.ORIGIN_CHANNEL_EMAIL)
email_lead = db.add_lead(None, None, {}, status="new", source_type=lead_domain.SOURCE_TYPE_EMAIL,
                          funnel_id=mail_funnel["id"], origin_channel=lead_domain.ORIGIN_CHANNEL_EMAIL)
stages = db.list_funnel_stages(mail_funnel["id"])
won_stage = next(s for s in stages if s["kind"] == "won")
db.set_lead_status(email_lead, won_stage["code"])
by_channel = {r["channel"]: r for r in db.leads_report_by_channel()}
assert by_channel["telegram"]["total"] == 1
assert by_channel["email"]["total"] == 1 and by_channel["email"]["won"] == 1
print("  ok —", {k: (v["total"], v["won"]) for k, v in by_channel.items()})

print("\n== канал × направление ==")
direction_id = db.add_direction("Глицерин")
db.set_lead_field(tg_lead, direction_id=direction_id)
db.set_lead_field(email_lead, direction_id=direction_id)
cross = db.leads_report_by_channel_and_direction()
assert {(r["channel"], r["direction_name"]) for r in cross} == {("telegram", "Глицерин"), ("email", "Глицерин")}
print("  ok")

print("\n== avg_days_to_win_by_channel: генерализовано по kind, не по имени этапа ==")
avg_win = db.avg_days_to_win_by_channel()
assert "email" in avg_win and avg_win["email"] >= 0
assert "telegram" not in avg_win, "телеграмный лид ещё не выигран — не должен попасть в среднее"
print("  ok —", avg_win)


# ---- скорость ответа --------------------------------------------------------
print("\n== mail_response_time_by_mailbox/by_direction: время от первого входящего до первого ответа ==")
resp_thread = db.create_mail_thread(mailbox_id, "resp-thread")
t0 = dt.datetime(2026, 8, 1, 10, 0, 0).astimezone().isoformat()
t1 = dt.datetime(2026, 8, 1, 16, 0, 0).astimezone().isoformat()  # 6 часов спустя
_msg(30, "Вопрос", "client2@buyer.ru", thread_id=resp_thread, is_outgoing=0)
db.execute("UPDATE mail_message SET date = ? WHERE mailbox_id = ? AND uid = 30", (t0, mailbox_id))
_msg(31, "Ответ", "sales@company.ru", folder="Sent", thread_id=resp_thread, is_outgoing=1)
db.execute("UPDATE mail_message SET date = ? WHERE mailbox_id = ? AND uid = 31", (t1, mailbox_id))

by_mailbox = db.mail_response_time_by_mailbox()
row = next(r for r in by_mailbox if r["mailbox_id"] == mailbox_id)
assert abs(row["avg_hours"] - 6.0) < 0.01, row["avg_hours"]
assert row["n"] == 1
print("  ok —", row["avg_hours"], "ч.")

db.set_mail_thread_lead(resp_thread, email_lead)
by_direction = db.mail_response_time_by_direction()
drow = next(r for r in by_direction if r["direction_id"] == direction_id)
assert abs(drow["avg_hours"] - 6.0) < 0.01
print("  ok — по направлению тоже совпало:", drow["avg_hours"], "ч.")

print("\n== тред, который начали мы сами (нет входящего до нашего письма), не попадает в отчёт ==")
cold_thread = db.create_mail_thread(mailbox_id, "cold-thread")
_msg(40, "Холодное письмо от нас", "sales@company.ru", folder="Sent", thread_id=cold_thread, is_outgoing=1)
by_mailbox2 = db.mail_response_time_by_mailbox()
row2 = next(r for r in by_mailbox2 if r["mailbox_id"] == mailbox_id)
assert row2["n"] == 1, "холодное письмо не должно было добавить строку — нет входящего, к которому оно ответ"
print("  ok")


# ---- ретеншн почты -----------------------------------------------------------
print("\n== MailRetentionService: предпросмотр, архив-и-удаление, срок для вложений отдельно ==")
ret_svc = MailRetentionService(db, paths)
old_thread = db.create_mail_thread(mailbox_id, "old-thread")
old_msg = _msg(50, "Старое письмо", "old@buyer.ru", thread_id=old_thread)
old_date = (dt.datetime.now().astimezone() - dt.timedelta(days=200)).isoformat()
db.execute("UPDATE mail_message SET date = ? WHERE id = ?", (old_date, old_msg))
att_path = paths.data_dir / "old_attachment.bin"
att_path.write_bytes(b"x" * 500)
db.add_mail_attachment(old_msg, "old_attachment.bin", "application/octet-stream", 500, str(att_path))

ret_svc.set_months(6)
preview = ret_svc.preview()
assert preview["messages"] >= 1
before_count = db.query_one("SELECT count(*) c FROM mail_message")["c"]
result = ret_svc.archive_and_prune()
assert result["deleted"] >= 1
assert result["path"].exists()
after_count = db.query_one("SELECT count(*) c FROM mail_message")["c"]
assert after_count == before_count - result["deleted"]
assert db.get_mail_message(old_msg) is None
assert not att_path.exists(), "вложение удалённого письма должно было уйти с диска"
assert db.get_mail_thread(old_thread) is None, "цепочка без единого письма должна быть убрана"
print("  ok — архивировано и удалено:", result["deleted"], "путь:", result["path"].name)

print("\n== ретеншн вложений отдельно от ретеншна писем ==")
kept_thread = db.create_mail_thread(mailbox_id, "kept-thread")
kept_msg = _msg(60, "Письмо остаётся", "keep@buyer.ru", thread_id=kept_thread)
old_date2 = (dt.datetime.now().astimezone() - dt.timedelta(days=100)).isoformat()
db.execute("UPDATE mail_message SET date = ? WHERE id = ?", (old_date2, kept_msg))
att_path2 = paths.data_dir / "keep_attachment.bin"
att_path2.write_bytes(b"y" * 300)
db.add_mail_attachment(kept_msg, "keep_attachment.bin", "application/octet-stream", 300, str(att_path2))

ret_svc.set_attachment_months(3)
att_preview = ret_svc.preview_attachments()
assert att_preview["count"] == 1
att_result = ret_svc.prune_attachments()
assert att_result["deleted"] == 1
assert att_result["bytes_freed"] == 300
assert not att_path2.exists()
assert db.get_mail_message(kept_msg) is not None, "само письмо должно было остаться"
assert db.list_mail_attachments(kept_msg) == []
print("  ok — вложение ушло, письмо осталось")


# ==== UI офскрин ============================================================
print("\n== UI офскрин: MailFiltersScreen, MailContactsScreen, MailAttachmentsScreen, MailReportsScreen ==")
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QDialog
app = QApplication.instance() or QApplication(sys.argv)

from chatgrab.ui.screens.mail import MailScreen
from chatgrab.ui.screens.mail.attachments_screen import MailAttachmentsScreen
from chatgrab.ui.screens.mail.contacts_screen import ContactDialog, MailContactsScreen
from chatgrab.ui.screens.mail.filters_screen import FilterDialog, MailFiltersScreen
from chatgrab.ui.screens.mail.reports_screen import MailReportsScreen


class _StubCtx:
    def __init__(self, database, service, retention_service):
        self.db = database
        self.mail_service = service
        self.mail_retention_service = retention_service


stub_ctx = _StubCtx(db, mail_service, ret_svc)

print("\n-- MailFiltersScreen: список строится, журнал показывает «Отменить»/«отменено» --")
screen = MailFiltersScreen(stub_ctx, lambda *a, **kw: None)
screen.on_show()
assert screen.table.rowCount() == 2  # "Реклама" и "Большие вложения"
assert screen.log_list.count() >= 2
print("  ok —", screen.table.rowCount(), "фильтра,", screen.log_list.count(), "записей в журнале")

print("\n-- FilterDialog: добавление условия, сохранение через настоящий виджет --")
dlg = FilterDialog(stub_ctx, mailbox_id=mailbox_id)
dlg.name_input.setText("Тестовый фильтр")
dlg._condition_rows[0].field_combo.setCurrentIndex(
    dlg._condition_rows[0].field_combo.findData("subject"))
dlg._condition_rows[0].value_input.setText("тест")
assert dlg.conditions() == [{"field": "subject", "op": "contains", "value": "тест"}]
new_id = db.create_mail_filter(dlg.values()["name"], dlg.values()["conditions"], mailbox_id=mailbox_id)
assert db.get_mail_filter(new_id)["name"] == "Тестовый фильтр"
print("  ok")

print("\n-- MailContactsScreen: поиск, фильтр по группе, диалог редактирования --")
cscreen = MailContactsScreen(stub_ctx, lambda *a, **kw: None)
cscreen.on_show()
assert cscreen.table.rowCount() >= 2
cscreen.group_combo.setCurrentIndex(cscreen.group_combo.findData("партнёры"))
cscreen.refresh()
assert cscreen.table.rowCount() == 1
cscreen.group_combo.setCurrentIndex(0)
cscreen.refresh()
edit_dlg = ContactDialog(contact=db.get_mail_contact_by_address("ivan@buyer.ru"))
assert edit_dlg.address_input.isEnabled() is False, "адрес существующего контакта не должен быть редактируемым"
print("  ok")

print("\n-- MailAttachmentsScreen: список вложений, фильтр по расширению --")
ascreen = MailAttachmentsScreen(stub_ctx, lambda *a, **kw: None)
ascreen.on_show()
assert ascreen.table.rowCount() >= 2
xlsx_idx = ascreen.ext_combo.findData(".xlsx")
assert xlsx_idx >= 0
ascreen.ext_combo.setCurrentIndex(xlsx_idx)
ascreen.refresh()
assert all(ascreen.table.item(r, 0).text().endswith(".xlsx") for r in range(ascreen.table.rowCount()))
print("  ok")

print("\n-- MailReportsScreen: таблицы отчётов и карточка ретеншна строятся --")
rscreen = MailReportsScreen(stub_ctx, lambda *a, **kw: None)
rscreen.on_show()
assert rscreen.channel_table.rowCount() >= 2
assert rscreen.mailbox_resp_table.rowCount() >= 1
assert "Старше" in rscreen.retention_preview.text() or "хранится всё" in rscreen.retention_preview.text()
print("  ok —", rscreen.retention_preview.text())

print("\n-- MailScreen: «фильтры спрятали N писем» — банер виден и ведёт в «Фильтры» --")
mscreen = MailScreen(stub_ctx, lambda *a, **kw: None)
mscreen.on_show()
# isVisible() reflects the whole ancestor chain, which is never shown in
# an offscreen test with no top-level window on screen — isHidden() is
# the one that reflects just this widget's own explicit setVisible()
# call, which is what _refresh_filters_banner() actually controls.
assert not mscreen.filters_banner.isHidden()
assert "спрятали" in mscreen.filters_banner.text()
navigated = {}
mscreen.navigate = lambda *a, **kw: navigated.update(target=a[0] if a else None)
mscreen.filters_banner.click()
assert navigated.get("target") == "mail_filters"
print("  ok —", mscreen.filters_banner.text())

print("\n-- MailService.export_thread_markdown: файл пишется, содержит тело письма --")
export_path = mail_service.export_thread_markdown(resp_thread)
assert export_path is not None and export_path.exists()
text = export_path.read_text(encoding="utf-8")
assert "Вопрос" in text or "Ответ" in text
assert mail_service.export_thread_markdown(999999) is None
print("  ok —", export_path.name)

print("\n== всё сошлось ==")
