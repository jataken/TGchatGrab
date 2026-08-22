"""П5: sending — drafts (new/reply/reply-all/forward/LLM), signatures,
draft-to-server sync, and the actual send: correct threading headers,
sent copy in Sent, server draft cleaned up. Central invariant, checked
last and hardest: no path except the explicit "send" button (here:
MailService.send_draft()) ever reaches SmtpClient.send() — draft
creation, autosave, server sync, and LLM-assisted drafting all stay
silent on the SMTP side no matter what.
"""
import asyncio
import sys
from email.header import Header
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _bootstrap import fresh_env
from _fake_imap import make_client_factory
from _fake_smtp import make_smtp_factory
from chatgrab.integrations.mail import credentials as mail_credentials
from chatgrab.services.mail_service import MailService

paths, db, config, security = fresh_env("cgmailsend")


def _msg(message_id, subject, sender, sender_name, to, date, body="Текст.",
         refs=None, in_reply_to=None):
    # Имя — через RFC 2047 encoded-word (Header), как реально формируют
    # серверы, а не сырыми UTF-8-байтами прямо в заголовке — сырые байты
    # в From имени email.message_from_bytes(policy=compat32) отдаёт как
    # email.header.Header, на котором email.utils.parseaddr() молча
    # возвращает ('', ''); отдельная, не относящаяся к П5 находка,
    # зафиксирована в журнале PLAN.md, не в этом фикстурном хелпере.
    encoded_name = Header(sender_name, "utf-8").encode()
    headers = [f"From: {encoded_name} <{sender}>", f"To: {to}", f"Subject: {subject}",
               f"Date: {date}", f"Message-ID: <{message_id}>"]
    if refs:
        headers.append(f"References: {refs}")
    if in_reply_to:
        headers.append(f"In-Reply-To: <{in_reply_to}>")
    headers.append("Content-Type: text/plain; charset=utf-8")
    return ("\r\n".join(headers) + "\r\n\r\n" + body + "\r\n").encode("utf-8")


state = {
    "imap.send.test": {
        "INBOX": {"uidvalidity": 1, "messages": {
            1: _msg("q1@x.ru", "Запрос КП на глицерин", "irina@x.ru", "Ирина Петрова",
                     "sales@company.ru", "Mon, 17 Aug 2026 10:00:00 +0300",
                     body="Добрый день!\nНужен глицерин, пришлите КП."),
        }},
        "Sent": {"uidvalidity": 1, "messages": {}, "special_use": "Sent"},
        "Drafts": {"uidvalidity": 1, "messages": {}, "special_use": "Drafts"},
    },
}
sent_log: list = []

svc = MailService(db, paths, security, client_factory=make_client_factory(state),
                   smtp_factory=make_smtp_factory(sent_log))
mb = db.add_mailbox("sales@company.ru", "imap.send.test", 993, "smtp.send.test", 465,
                     password_enc=mail_credentials.encrypt_password(security, "correct-password"))
asyncio.run(svc.tick())  # обнаруживает Sent/Drafts и подтягивает письмо в INBOX

identity_id = db.add_mail_identity(mb, "Иван Иванов", "sales@company.ru",
                                    signature="С уважением,\n{имя}\n{email}", is_default=True)

original = db.get_mail_message_by_uid(mb, "INBOX", 1)
svc.fetch_body(original["id"])  # тело по требованию (П1) — цитате в ответе нечего цитировать без него
original = db.get_mail_message(original["id"])


print("== новый черновик: подпись подставлена, место для текста — над ней ==")
new_draft_id = svc.start_new_draft(mb)
new_draft = db.get_mail_draft(new_draft_id)
print(repr(new_draft["body_text"]))
assert "Иван Иванов" in new_draft["body_text"]
assert "sales@company.ru" in new_draft["body_text"]
assert new_draft["identity_id"] == identity_id
assert new_draft["author"] == "human"
print("  ok")


print("\n== ответ: тема с Re:, получатель — отправитель исходного, цитата снизу ==")
reply_id = svc.start_reply_draft(original["id"])
reply = db.get_mail_draft(reply_id)
print("  тема:", reply["subject"], "| кому:", reply["to_addresses"])
assert reply["subject"] == "Re: Запрос КП на глицерин"
assert reply["to_addresses"] == '["irina@x.ru"]'
assert reply["kind"] == "reply"
assert "Ирина Петрова писал(а):" in reply["body_text"]
assert "> Нужен глицерин, пришлите КП." in reply["body_text"]
assert reply["in_reply_to_message_id"] == original["id"]
print("  ok")


print("\n== ответить всем: остальные получатели уходят в копию, свой адрес исключён ==")
original_multi_uid = 2
state["imap.send.test"]["INBOX"]["messages"][original_multi_uid] = _msg(
    "q2@x.ru", "Совещание по поставке", "petr@x.ru", "Пётр Сидоров",
    "sales@company.ru", "Mon, 17 Aug 2026 11:00:00 +0300", body="Обсудим завтра.")
# «to_addresses» письма при разборе имён не несёт — подменим руками то,
# что реально хранится в базе после апсерта, добавив второго адресата.
asyncio.run(svc.tick())
m2 = db.get_mail_message_by_uid(mb, "INBOX", original_multi_uid)
db.upsert_mail_message(mb, "INBOX", original_multi_uid,
                        to_addresses='["sales@company.ru", "buh@x.ru"]')
m2 = db.get_mail_message_by_uid(mb, "INBOX", original_multi_uid)
reply_all_id = svc.start_reply_draft(m2["id"], reply_all=True)
reply_all = db.get_mail_draft(reply_all_id)
print("  кому:", reply_all["to_addresses"], "| копия:", reply_all["cc_addresses"])
assert reply_all["to_addresses"] == '["petr@x.ru"]'
assert reply_all["cc_addresses"] == '["buh@x.ru"]', "sales@company.ru — свой адрес, не должен попасть в копию"
print("  ok")


print("\n== пересылка: вложения исходного письма копируются в черновик ==")
db.set_mail_message_body(original["id"], "Нужен глицерин.", None, True)
att_src = paths.data_dir / "src_attach.txt"
att_src.write_bytes(b"price list content")
db.add_mail_attachment(original["id"], "price.txt", "text/plain", att_src.stat().st_size, str(att_src))
fwd_id = svc.start_forward_draft(original["id"])
fwd = db.get_mail_draft(fwd_id)
fwd_atts = db.list_mail_draft_attachments(fwd_id)
print("  тема:", fwd["subject"], "| вложения:", [(a["filename"], Path(a["path"]).exists()) for a in fwd_atts])
assert fwd["subject"] == "Fwd: Запрос КП на глицерин"
assert "Пересылаемое сообщение" in fwd["body_text"]
assert len(fwd_atts) == 1 and fwd_atts[0]["filename"] == "price.txt"
assert Path(fwd_atts[0]["path"]).exists()
assert Path(fwd_atts[0]["path"]) != att_src, "вложение должно быть скопировано, не указывать на исходный файл"
print("  ok")


print("\n== ручное вложение к черновику копируется в собственную папку черновика ==")
manual_src = paths.data_dir / "manual_attach.txt"
manual_src.write_bytes(b"manual file")
svc.add_draft_attachment(new_draft_id, str(manual_src))
manual_atts = db.list_mail_draft_attachments(new_draft_id)
print("  ", [(a["filename"], a["size_bytes"]) for a in manual_atts])
assert len(manual_atts) == 1 and manual_atts[0]["filename"] == "manual_attach.txt"
assert manual_atts[0]["size_bytes"] == manual_src.stat().st_size
print("  ok")


print("\n== черновик от LLM: автор «assistant», SMTP не тронут ==")
llm_draft_id = svc.create_llm_draft(original["id"], "Спасибо за обращение, направим КП завтра.")
llm_draft = db.get_mail_draft(llm_draft_id)
print("  автор:", llm_draft["author"])
assert llm_draft["author"] == "assistant"
assert "Спасибо за обращение" in llm_draft["body_text"]
assert "> Нужен глицерин." in llm_draft["body_text"]
assert sent_log == [], "создание черновика (в т.ч. от LLM) не должно ничего отправлять"
print("  ok")


print("\n== черновик сохраняется на сервере: появляется в Drafts, server_uid запоминается ==")
db.update_mail_draft(new_draft_id, to_addresses=["client@x.ru"], subject="Черновик для проверки")
svc.sync_draft_to_server(new_draft_id)
draft_after_sync = db.get_mail_draft(new_draft_id)
print("  server_uid:", draft_after_sync["server_uid"], "| в Drafts на сервере:",
      len(state["imap.send.test"]["Drafts"]["messages"]))
assert draft_after_sync["server_uid"] is not None
assert len(state["imap.send.test"]["Drafts"]["messages"]) == 1
first_uid = draft_after_sync["server_uid"]

svc.sync_draft_to_server(new_draft_id)  # повторное сохранение — не плодит копии
draft_after_second_sync = db.get_mail_draft(new_draft_id)
print("  после повторного сохранения — писем в Drafts:", len(state["imap.send.test"]["Drafts"]["messages"]))
assert len(state["imap.send.test"]["Drafts"]["messages"]) == 1
assert first_uid not in state["imap.send.test"]["Drafts"]["messages"], "старая копия должна быть удалена"
print("  ok")


print("\n== отправка: письмо уходит по SMTP с верными заголовками цепочки ==")
sent_before = len(sent_log)
svc.send_draft(reply_id)
print("  отправлено писем:", len(sent_log) - sent_before)
assert len(sent_log) == sent_before + 1
sent_item = sent_log[-1]
print("  from:", sent_item["from"], "| to:", sent_item["to"])
assert sent_item["from"] == "sales@company.ru"
assert sent_item["to"] == ["irina@x.ru"]
import email
import email.policy
parsed = email.message_from_bytes(sent_item["raw"], policy=email.policy.default)
print("  In-Reply-To:", parsed["In-Reply-To"], "| References:", parsed["References"])
assert parsed["In-Reply-To"] == "<q1@x.ru>"
assert parsed["References"] == "<q1@x.ru>"
assert parsed["Subject"] == "Re: Запрос КП на глицерин"
print("  ok")


print("\n== копия отправленного попала в Sent, черновик отмечен отправленным ==")
print("  писем в Sent на сервере:", len(state["imap.send.test"]["Sent"]["messages"]))
assert len(state["imap.send.test"]["Sent"]["messages"]) == 1
sent_local = db.list_mail_messages(mb, folder="Sent")
print("  в Sent локально:", [(r["subject"]) for r in sent_local])
assert len(sent_local) == 1 and sent_local[0]["subject"] == "Re: Запрос КП на глицерин"
reply_after_send = db.get_mail_draft(reply_id)
assert reply_after_send["sent_at"] is not None
assert reply_id not in [d["id"] for d in db.list_mail_drafts(mb)], "отправленный черновик не должен быть в списке черновиков"
print("  ok")


print("\n== отправленный черновик убирается из серверных Drafts, если был там ==")
svc.sync_draft_to_server(new_draft_id)
draft_synced = db.get_mail_draft(new_draft_id)
assert draft_synced["server_uid"] is not None
before_send_drafts_count = len(state["imap.send.test"]["Drafts"]["messages"])
db.update_mail_draft(new_draft_id, to_addresses=["client@x.ru"])
svc.send_draft(new_draft_id)
print("  писем в Drafts на сервере после отправки:", len(state["imap.send.test"]["Drafts"]["messages"]))
assert len(state["imap.send.test"]["Drafts"]["messages"]) == before_send_drafts_count - 1
print("  ok")


print("\n== без получателя и без SMTP-сервера — понятная ошибка, не отправка ==")
empty_draft_id = svc.start_new_draft(mb)
try:
    svc.send_draft(empty_draft_id)
    raise AssertionError("должен был поднять ValueError — нет получателя")
except ValueError as e:
    print("  ok, нет получателя:", e)

no_smtp_mb = db.add_mailbox("noSMTP@x.ru", "imap.send.test", 993,
                             password_enc=mail_credentials.encrypt_password(security, "correct-password"))
no_smtp_draft_id = db.create_mail_draft(no_smtp_mb, to_addresses=["a@x.ru"], subject="Тест", body_text="Текст")
try:
    svc.send_draft(no_smtp_draft_id)
    raise AssertionError("должен был поднять ValueError — нет SMTP-сервера")
except ValueError as e:
    print("  ok, нет SMTP:", e)


print("\n== неверный пароль SMTP — ошибка, письмо не считается отправленным ==")
bad_pw_factory = make_smtp_factory(sent_log, valid_password="совсем другой пароль")
svc_bad = MailService(db, paths, security, client_factory=make_client_factory(state), smtp_factory=bad_pw_factory)
bad_draft_id = svc.start_new_draft(mb)
db.update_mail_draft(bad_draft_id, to_addresses=["client@x.ru"], subject="Не должно уйти")
sent_before_fail = len(sent_log)
from chatgrab.integrations.mail.smtp_client import SmtpError
try:
    svc_bad.send_draft(bad_draft_id)
    raise AssertionError("должен был поднять SmtpError")
except SmtpError as e:
    print("  ok:", e)
assert len(sent_log) == sent_before_fail, "sendmail не должен был вызваться после неудачного login"
assert db.get_mail_draft(bad_draft_id)["sent_at"] is None
print("  ok — черновик остался неотправленным")


print("\n== инвариант: ни один путь, кроме send_draft(), не зовёт SMTP ==")


class _ExplodingSmtpConnection:
    def login(self, *a, **kw):
        raise AssertionError("SMTP.login() не должен вызываться вне send_draft()")

    def sendmail(self, *a, **kw):
        raise AssertionError("SMTP.sendmail() не должен вызываться вне send_draft()")

    def quit(self):
        pass


def _exploding_smtp_factory(host, port):
    from chatgrab.integrations.mail.smtp_client import SmtpClient
    return SmtpClient(host, port, connection_factory=_ExplodingSmtpConnection)


svc_guarded = MailService(db, paths, security, client_factory=make_client_factory(state),
                           smtp_factory=_exploding_smtp_factory)
guarded_new = svc_guarded.start_new_draft(mb)
guarded_reply = svc_guarded.start_reply_draft(original["id"])
guarded_fwd = svc_guarded.start_forward_draft(original["id"])
guarded_llm = svc_guarded.create_llm_draft(original["id"], "Черновик от помощника.")
db.update_mail_draft(guarded_new, to_addresses=["x@x.ru"])
svc_guarded.sync_draft_to_server(guarded_new)
print("  создание/пересылка/LLM-черновик/синхронизация с сервером не тронули SMTP — ok")

print("\n== ComposeDialog/SendConfirmDialog/IdentityManagerDialog: строятся и работают офскрин ==")
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication(sys.argv)

from PySide6.QtWidgets import QLabel as _QLabel
from chatgrab.ui.screens.mail.compose import ComposeDialog, SendConfirmDialog
from chatgrab.ui.screens.mail_settings import IdentityManagerDialog


class _StubCtx:
    def __init__(self, database, service, security_service):
        self.db = database
        self.mail_service = service
        self.security = security_service


stub_ctx = _StubCtx(db, svc, security)

ui_draft_id = svc.start_new_draft(mb)
dlg = ComposeDialog(stub_ctx, ui_draft_id, parent=None)
print("  тело при открытии:", repr(dlg.body_edit.toPlainText())[:60])
dlg.to_field.set_text("client@x.ru")
dlg.subject_field.set_text("Проверка из UI")
dlg.body_edit.setPlainText("Текст письма из диалога.")
dlg._save_local()
saved = db.get_mail_draft(ui_draft_id)
print("  после _save_local:", saved["to_addresses"], saved["subject"])
assert saved["to_addresses"] == '["client@x.ru"]'
assert saved["subject"] == "Проверка из UI"
assert saved["body_text"] == "Текст письма из диалога."
print("  ok — поля читаются и сохраняются")

manual_att = paths.data_dir / "ui_manual_attach.txt"
manual_att.write_bytes(b"x" * 100)
svc.add_draft_attachment(ui_draft_id, str(manual_att))
dlg._refresh_attachments()
print("  вложений в списке:", dlg.attachment_list.count())
assert dlg.attachment_list.count() == 1
print("  ok — вложение через диалог отражается в списке")

sent_before_ui = len(sent_log)
confirm_dlg = SendConfirmDialog(stub_ctx, ui_draft_id, parent=dlg)
confirm_text = " ".join(w.text() for w in confirm_dlg.findChildren(_QLabel))
print("  окно подтверждения построено, получатель показан:", "client@x.ru" in confirm_text)
assert confirm_dlg.windowTitle() == "Проверьте перед отправкой"
assert "client@x.ru" in confirm_text


async def _confirm_and_send():
    confirm_dlg._on_confirm()
    # Poll instead of a fixed sleep: fire()'s run_in_executor callback can
    # land well past 0.3s on a loaded/slow CI runner (observed on Windows
    # in the equivalent MessagePane test), so a flat sleep is an
    # intermittent-failure trap.
    elapsed = 0.0
    step = 0.05
    timeout = 5.0
    while len(sent_log) == sent_before_ui and elapsed < timeout:
        await asyncio.sleep(step)
        elapsed += step


asyncio.run(_confirm_and_send())
print("  писем отправлено:", len(sent_log) - sent_before_ui)
assert len(sent_log) == sent_before_ui + 1
assert db.get_mail_draft(ui_draft_id)["sent_at"] is not None
print("  ok — подтверждение реально вызывает отправку, письмо ушло по SMTP")


print("\n== SendConfirmDialog предупреждает про >5 получателей и «вложение без файла» ==")
warn_draft_id = svc.start_new_draft(mb)
many = [f"a{i}@x.ru" for i in range(6)]
db.update_mail_draft(warn_draft_id, to_addresses=many, subject="Массовая рассылка",
                      body_text="Отправляю во вложении прайс.")
warn_dlg = SendConfirmDialog(stub_ctx, warn_draft_id, parent=None)
all_text = " ".join(w.text() for w in warn_dlg.findChildren(_QLabel))
print("  тексты диалога содержат предупреждение о получателях:", "больше 5" in all_text)
print("  тексты диалога содержат предупреждение о вложении:", "не прикреплено" in all_text)
assert "больше 5" in all_text
assert "не прикреплено" in all_text
print("  ok")


print("\n== IdentityManagerDialog: добавление, назначение основной, удаление — офскрин ==")
id_dlg = IdentityManagerDialog(stub_ctx, mb, "sales@company.ru", parent=None)
before_identities = len(db.list_mail_identities(mb))
id_dlg.name_field.set_text("Отдел закупок")
id_dlg.address_field.set_text("zakupki@company.ru")
id_dlg.signature_edit.setPlainText("Отдел закупок")
id_dlg._on_add()
after_identities = db.list_mail_identities(mb)
print("  личностей стало:", len(after_identities), "(было", before_identities, ")")
assert len(after_identities) == before_identities + 1
new_identity = next(i for i in after_identities if i["from_address"] == "zakupki@company.ru")
assert new_identity["is_default"] == 0, "первая (уже существующая) личность остаётся основной"
id_dlg._on_set_default(new_identity["id"])
assert db.get_default_mail_identity(mb)["id"] == new_identity["id"]
id_dlg._on_delete(new_identity["id"])
assert db.get_mail_identity(new_identity["id"]) is None
print("  ok — добавление, назначение основной и удаление личности работают")


print("\n== DraftsListDialog: показывает сохранённые черновики, двойной клик открывает ComposeDialog ==")
from chatgrab.ui.screens.mail.compose import DraftsListDialog

resume_draft_id = svc.start_new_draft(mb)
db.update_mail_draft(resume_draft_id, subject="Черновик для возврата", to_addresses=["resume@x.ru"])
drafts_dlg = DraftsListDialog(stub_ctx, mb, parent=None)
found = [drafts_dlg.list_widget.item(i).data(Qt.UserRole)
         for i in range(drafts_dlg.list_widget.count())]
print("  черновиков в списке:", len(found), "| наш черновик найден:", resume_draft_id in found)
assert resume_draft_id in found
item = next(drafts_dlg.list_widget.item(i) for i in range(drafts_dlg.list_widget.count())
            if drafts_dlg.list_widget.item(i).data(Qt.UserRole) == resume_draft_id)
# _on_open() opens ComposeDialog модально (.exec() блокирует до закрытия
# диалога человеком) — в автотесте вызывать его напрямую нечем закрыть,
# поэтому здесь проверяется только то, что действительно тестируемо:
# построение самого ComposeDialog на данных существующего черновика,
# без реального модального показа.
resumed = ComposeDialog(stub_ctx, item.data(Qt.UserRole), parent=None)
print("  тема в возобновлённом диалоге:", resumed.subject_field.text())
assert resumed.subject_field.text() == "Черновик для возврата"
assert resumed.to_field.text() == "resume@x.ru"
print("  ok — черновик открывается с ранее сохранёнными полями")

print("\nТЕСТ ПРОЙДЕН: черновики, подписи, синхронизация и отправка работают, "
      "автоотправки нет ни на одном пути, интерфейс составления и личностей строится и работает")
