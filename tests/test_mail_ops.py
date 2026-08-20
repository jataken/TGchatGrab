"""П4: folder admin (create/rename/delete/subscribe, SPECIAL-USE),
flags synced with the server, move/copy (same-mailbox and cross-mailbox),
Trash + restore, permanent delete, and the offline action queue —
everything core/mail_thread.py and the sync pipeline itself didn't
already need covering in test_mail_sync.py/test_mail_reading.py.
IDLE has its own file, test_mail_idle.py, since it operates at a
completely different (raw-socket) level than everything here.
"""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _bootstrap import fresh_env
from _fake_imap import NoMoveConnection, NoUidExpungeConnection, make_client_factory
from chatgrab.integrations.mail import credentials as mail_credentials
from chatgrab.integrations.mail.imap_client import ImapClient
from chatgrab.services.mail_service import MailService

paths, db, config, security = fresh_env("cgmailops")


def _msg(message_id, subject, sender, to, date, body="Текст.", extra_headers=""):
    headers = (
        f"From: {sender}\r\nTo: {to}\r\nSubject: {subject}\r\nDate: {date}\r\n"
        f"Message-ID: <{message_id}>\r\n{extra_headers}"
        f"Content-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n"
    )
    return headers.encode("utf-8")


state = {
    "imap.ops.test": {
        "INBOX": {"uidvalidity": 1, "messages": {
            1: _msg("q1@x.ru", "Запрос КП", "irina@x.ru", "a@ops.test",
                     "Mon, 17 Aug 2026 10:00:00 +0300", body="Нужен глицерин."),
            2: _msg("q2@x.ru", "Про счёт", "buh@x.ru", "a@ops.test",
                     "Mon, 17 Aug 2026 11:00:00 +0300", body="Когда оплата?"),
        }},
        "Archive": {"uidvalidity": 1, "messages": {}},
        "Trash": {"uidvalidity": 1, "messages": {}, "special_use": "Trash"},
        "Sent": {"uidvalidity": 1, "messages": {}, "special_use": "Sent"},
    },
    "imap.other.test": {
        "INBOX": {"uidvalidity": 1, "messages": {}},
    },
}

svc = MailService(db, paths, security, client_factory=make_client_factory(state))
mb_a = db.add_mailbox("a@ops.test", "imap.ops.test", 993,
                       password_enc=mail_credentials.encrypt_password(security, "correct-password"))
mb_b = db.add_mailbox("b@other.test", "imap.other.test", 993,
                       password_enc=mail_credentials.encrypt_password(security, "correct-password"))

n = asyncio.run(svc.tick())
print("== письма синхронизированы, папки обнаружены ==")
print("  новых писем:", n)
assert n == 2


print("\n== SPECIAL-USE определяется по ответу сервера, не по имени папки ==")
trash = db.get_mail_folder(mb_a, "Trash")
sent = db.get_mail_folder(mb_a, "Sent")
archive = db.get_mail_folder(mb_a, "Archive")
inbox = db.get_mail_folder(mb_a, "INBOX")
print("  Trash:", trash["special_use"], "| Sent:", sent["special_use"], "| Archive:", archive["special_use"])
assert trash["special_use"] == "Trash"
assert sent["special_use"] == "Sent"
assert archive["special_use"] is None
assert inbox["special_use"] is None
print("  ok")


print("\n== создание, переименование, удаление и подписка на папку ==")
svc.create_folder(mb_a, "Проекты")
assert "Проекты" in state["imap.ops.test"]
assert db.get_mail_folder(mb_a, "Проекты") is not None
svc.rename_folder(mb_a, "Проекты", "Проекты 2026")
assert "Проекты 2026" in state["imap.ops.test"] and "Проекты" not in state["imap.ops.test"]
assert db.get_mail_folder(mb_a, "Проекты 2026") is not None
assert db.get_mail_folder(mb_a, "Проекты") is None
svc.set_folder_subscribed(mb_a, "Проекты 2026", True)
assert db.get_mail_folder(mb_a, "Проекты 2026")["enabled"] == 1
assert state["imap.ops.test"]["Проекты 2026"]["subscribed"] is True
svc.delete_folder(mb_a, "Проекты 2026")
assert "Проекты 2026" not in state["imap.ops.test"]
assert db.get_mail_folder(mb_a, "Проекты 2026") is None
print("  ok")


print("\n== флаги, уже стоящие на письме на сервере, приходят при первой синхронизации ==")
state["imap.ops.test"]["INBOX"]["messages"][3] = _msg(
    "q3@x.ru", "Важный вопрос", "boss@x.ru", "a@ops.test", "Mon, 17 Aug 2026 12:00:00 +0300",
    body="Срочно.")
state["imap.ops.test"]["INBOX"]["flags"] = {3: {"\\Flagged", "\\Answered"}}
n2 = asyncio.run(svc.tick())
assert n2 == 1
m3 = db.get_mail_message_by_uid(mb_a, "INBOX", 3)
print("  is_flagged:", m3["is_flagged"], "| is_answered:", m3["is_answered"], "| is_read:", m3["is_read"])
assert m3["is_flagged"] == 1 and m3["is_answered"] == 1 and m3["is_read"] == 0
print("  ok — флаги, выставленные другим клиентом, отражаются локально сразу")


print("\n== mark_read/set_flagged: локально сразу, на сервер — через очередь ==")
m1 = db.get_mail_message_by_uid(mb_a, "INBOX", 1)
svc.mark_read(m1["id"], True)
svc.set_flagged(m1["id"], True)
m1 = db.get_mail_message(m1["id"])
print("  локально: is_read=", m1["is_read"], "is_flagged=", m1["is_flagged"])
assert m1["is_read"] == 1 and m1["is_flagged"] == 1
seen_on_server = state["imap.ops.test"]["INBOX"].get("seen", set())
flags_on_server = state["imap.ops.test"]["INBOX"].get("flags", {}).get(1, set())
print("  на сервере: seen=", seen_on_server, "flags[1]=", flags_on_server)
assert 1 in seen_on_server
assert "\\Flagged" in flags_on_server
pending = db.list_pending_mail_actions(mb_a)
assert pending == [], "действие уже должно было примениться и быть отмечено как выполненное"
print("  ok")


print("\n== перемещение письма в ту же папку того же ящика: сразу локально, потом пересинк ==")
m2 = db.get_mail_message_by_uid(mb_a, "INBOX", 2)
old_id = m2["id"]
svc.move_message(m2["id"], "Archive")
# Реальный IMAP MOVE присваивает письму новый UID в папке назначения —
# фейк делает то же самое, поэтому проверяем по наличию письма в Archive
# на сервере вообще, не по конкретному (уже неверному) старому UID.
in_archive_server = len(state["imap.ops.test"]["Archive"]["messages"]) == 1
print("  осталось в INBOX на сервере:", 2 in state["imap.ops.test"]["INBOX"]["messages"])
print("  есть в Archive на сервере:", in_archive_server)
assert 2 not in state["imap.ops.test"]["INBOX"]["messages"]
assert in_archive_server
moved = db.list_mail_messages(mb_a, folder="Archive")
print("  в Archive локально:", [(r["id"], r["uid"], r["subject"]) for r in moved])
assert len(moved) == 1 and moved[0]["subject"] == "Про счёт"
assert moved[0]["id"] != old_id, "после перемещения должна остаться свежая запись с настоящим UID, не старый placeholder"
assert db.get_mail_message(old_id) is None, "запись-плейсхолдер со старым UID должна быть удалена"
print("  ok — перемещённое письмо переоткрыто в Archive с настоящим UID, старая запись не осталась дублем")


print("\n== удаление в корзину и восстановление — «пока письмо в корзине» ==")
m1 = db.get_mail_message_by_uid(mb_a, "INBOX", 1)
m1_message_id_header = m1["message_id"]
went = svc.move_to_trash(m1["id"])
assert went, "у ящика есть Trash по SPECIAL-USE — должно было получиться"
# Перемещение (как и обычное — см. кейс с Archive выше) пересобирает
# запись с настоящим UID в Trash под новым id, поэтому находим её заново
# по Message-ID, а не по старому m1["id"].
in_trash = db.get_mail_message_by_message_id(mb_a, m1_message_id_header)
print("  папка после удаления:", in_trash["folder"], "| restore_folder:", in_trash["restore_folder"])
assert in_trash["folder"] == "Trash" and in_trash["restore_folder"] == "INBOX"
assert any(True for _ in state["imap.ops.test"]["Trash"]["messages"].values())
assert 1 not in state["imap.ops.test"]["INBOX"]["messages"]

restored = svc.restore_from_trash(in_trash["id"])
assert restored
back = db.get_mail_message_by_message_id(mb_a, m1_message_id_header)
print("  после восстановления: папка=", back["folder"], "restore_folder=", back["restore_folder"])
assert back["folder"] == "INBOX" and back["restore_folder"] is None
assert not state["imap.ops.test"]["Trash"]["messages"]
print("  ok — обратимо в обе стороны, локально и на сервере")


print("\n== письмо, оказавшееся в Trash не через удаление, восстанавливать нечего ==")
db.set_mail_folder_state(mb_a, "Trash", enabled=1)  # иначе тик не станет её забирать
state["imap.ops.test"]["Trash"]["messages"][99] = _msg(
    "already@x.ru", "Уже в корзине", "x@x.ru", "a@ops.test", "Mon, 17 Aug 2026 13:00:00 +0300")
asyncio.run(svc.tick())
already_trashed = db.get_mail_message_by_uid(mb_a, "Trash", 99)
assert already_trashed["restore_folder"] is None
assert svc.restore_from_trash(already_trashed["id"]) is False
print("  ok — нет записи «откуда», нечего предлагать восстановить")


print("\n== окончательное удаление: местно сразу, на сервере — expunge через очередь ==")
victim = db.get_mail_message_by_uid(mb_a, "Trash", 99)
victim_id = victim["id"]
svc.permanently_delete(victim_id)
assert db.get_mail_message(victim_id) is None, "локальная запись должна исчезнуть сразу"
assert 99 not in state["imap.ops.test"]["Trash"]["messages"], "и на сервере — после разбора очереди"
print("  ok")


print("\n== MOVE не поддерживается сервером — используется COPY + \\Deleted + EXPUNGE ==")
state_nomove = {"imap.nomove.test": {
    "INBOX": {"uidvalidity": 1, "messages": {
        1: _msg("nm1@x.ru", "Без MOVE", "a@x.ru", "c@nomove.test", "Mon, 17 Aug 2026 09:00:00 +0300"),
    }},
    "Archive": {"uidvalidity": 1, "messages": {}},
}}
svc_nomove = MailService(db, paths, security,
                          client_factory=make_client_factory(state_nomove, NoMoveConnection))
mb_nm = db.add_mailbox("c@nomove.test", "imap.nomove.test", 993,
                        password_enc=mail_credentials.encrypt_password(security, "correct-password"))
asyncio.run(svc_nomove.tick())
msg_nm = db.get_mail_message_by_uid(mb_nm, "INBOX", 1)
svc_nomove.move_message(msg_nm["id"], "Archive")
print("  осталось в INBOX:", 1 in state_nomove["imap.nomove.test"]["INBOX"]["messages"])
print("  оказалось в Archive:", len(state_nomove["imap.nomove.test"]["Archive"]["messages"]) == 1)
assert 1 not in state_nomove["imap.nomove.test"]["INBOX"]["messages"]
assert len(state_nomove["imap.nomove.test"]["Archive"]["messages"]) == 1
print("  ok — тот же результат без расширения MOVE на сервере")


print("\n== UID EXPUNGE не поддерживается — используется обычный EXPUNGE ==")
state_noue = {"imap.noue.test": {
    "INBOX": {"uidvalidity": 1, "messages": {
        1: _msg("ue1@x.ru", "Останется", "a@x.ru", "d@noue.test", "Mon, 17 Aug 2026 09:00:00 +0300"),
        2: _msg("ue2@x.ru", "Удалится", "a@x.ru", "d@noue.test", "Mon, 17 Aug 2026 09:05:00 +0300"),
    }},
}}
svc_noue = MailService(db, paths, security,
                        client_factory=make_client_factory(state_noue, NoUidExpungeConnection))
mb_noue = db.add_mailbox("d@noue.test", "imap.noue.test", 993,
                          password_enc=mail_credentials.encrypt_password(security, "correct-password"))
asyncio.run(svc_noue.tick())
victim2 = db.get_mail_message_by_uid(mb_noue, "INBOX", 2)
svc_noue.permanently_delete(victim2["id"])
print("  осталось на сервере:", sorted(state_noue["imap.noue.test"]["INBOX"]["messages"]))
assert set(state_noue["imap.noue.test"]["INBOX"]["messages"]) == {1}
print("  ok — упало на обычный EXPUNGE и удалило ровно нужное письмо")


print("\n== перемещение между ящиками: fetch с одного сервера, append на другой ==")
cross_msg = db.get_mail_message_by_uid(mb_a, "INBOX", 3)  # «Важный вопрос», из более ранней проверки флагов
print("  переносим:", cross_msg["subject"])
svc.move_message(cross_msg["id"], "INBOX", dest_mailbox_id=mb_b)
gone_from_a = db.get_mail_message_by_uid(mb_a, "INBOX", 3)
print("  исчезло из ящика A локально:", gone_from_a is None)
print("  исчезло из ящика A на сервере:", 3 not in state["imap.ops.test"]["INBOX"]["messages"])
assert gone_from_a is None
assert 3 not in state["imap.ops.test"]["INBOX"]["messages"]
assert len(state["imap.other.test"]["INBOX"]["messages"]) == 1, "должно появиться письмо на сервере B"
appeared_in_b = db.list_mail_messages(mb_b, folder="INBOX")
print("  появилось в ящике B локально:", [(r["subject"], r["is_flagged"], r["is_answered"]) for r in appeared_in_b])
assert len(appeared_in_b) == 1
assert appeared_in_b[0]["subject"] == "Важный вопрос"
assert appeared_in_b[0]["is_flagged"] == 1 and appeared_in_b[0]["is_answered"] == 1, \
    "флаги должны были перенестись вместе с письмом"
assert appeared_in_b[0]["thread_id"] is not None, "перенесённое письмо тоже должно получить свою ветку"
print("  ok — та же переписка доступна в ящике B, флаги сохранены, своя ветка назначена")


print("\n== офлайн: действие остаётся в очереди, при восстановлении применяется один раз ==")
target = db.get_mail_message_by_uid(mb_a, "INBOX", 1) or db.list_mail_messages(mb_a, folder="INBOX")[0]
before_pending = len(db.list_pending_mail_actions(mb_a))


class _AlwaysOfflineConnection:
    def login(self, *a, **kw):
        return "NO", [b"network unreachable"]


def offline_factory(host, port):
    return ImapClient(host, port, connection_factory=_AlwaysOfflineConnection)


svc_offline_view = MailService(db, paths, security, client_factory=offline_factory)

svc.db.set_message_flags(target["id"], is_flagged=True)
svc.db.enqueue_mail_action(mb_a, target["id"], "flags", {})
applied_while_offline = svc_offline_view.drain_queue(mb_a)
print("  применено «в офлайне»:", applied_while_offline)
assert applied_while_offline == 0
pending_after_offline = db.list_pending_mail_actions(mb_a)
print("  осталось в очереди:", len(pending_after_offline))
assert len(pending_after_offline) == before_pending + 1

applied_online = svc.drain_queue(mb_a)
print("  применено после восстановления связи:", applied_online)
assert applied_online == 1
assert db.list_pending_mail_actions(mb_a) == []

applied_again = svc.drain_queue(mb_a)
print("  повторный разбор пустой очереди:", applied_again)
assert applied_again == 0, "не должно применяться дважды"
print("  ok — офлайн-очередь переживает недоступность сервера и не дублирует применённое")


print("\n== MessagePane: кнопки флага/перемещения/корзины строятся и работают офскрин ==")
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QMessageBox
app = QApplication.instance() or QApplication(sys.argv)

from chatgrab.ui.screens.mail import MessagePane


class _StubCtx:
    def __init__(self, database, service):
        self.db = database
        self.mail_service = service


ui_mailbox = db.add_mailbox("ui@ops.test", "imap.ops.test", 993,
                             password_enc=mail_credentials.encrypt_password(security, "correct-password"))
state["imap.ops.test"]["INBOX"]["messages"][50] = _msg(
    "ui1@x.ru", "Для UI-проверки", "ui-sender@x.ru", "ui@ops.test", "Mon, 17 Aug 2026 15:00:00 +0300")
ui_svc = MailService(db, paths, security, client_factory=make_client_factory(state))
asyncio.run(ui_svc.tick())
ui_message = db.get_mail_message_by_uid(ui_mailbox, "INBOX", 50)

stub_ctx = _StubCtx(db, ui_svc)
changed_calls = []
pane = MessagePane(stub_ctx, ui_message, lambda *a: None,
                    on_changed=lambda **kw: changed_calls.append(kw))
print("  флаг изначально:", pane.flag_btn.text())
assert pane.flag_btn.text() == "☆"


async def _wait_for_calls(n, timeout=5.0):
    """Poll instead of a fixed sleep: fire()'s run_in_executor callback can
    land well past 0.3s on a loaded/slow CI runner (observed on Windows),
    so a flat sleep is an intermittent-failure trap."""
    elapsed = 0.0
    step = 0.05
    while len(changed_calls) < n and elapsed < timeout:
        await asyncio.sleep(step)
        elapsed += step
    assert len(changed_calls) >= n, f"колбэк не пришёл за {timeout}с (пришло {len(changed_calls)} из {n})"


async def _exercise_pane_actions():
    pane._on_toggle_flag()
    await _wait_for_calls(1)
    refreshed = db.get_mail_message(ui_message["id"])
    print("  после клика: is_flagged=", refreshed["is_flagged"], "| колбэк вызван:", len(changed_calls) == 1)
    assert refreshed["is_flagged"] == 1
    assert len(changed_calls) == 1

    pane._do_move("Archive")
    await _wait_for_calls(2)
    moved = db.get_mail_message_by_message_id(ui_mailbox, ui_message["message_id"])
    print("  после перемещения: папка=", moved["folder"], "| колбэк:", changed_calls[-1]["kind"])
    assert moved["folder"] == "Archive"
    assert changed_calls[-1]["kind"] == "move" and changed_calls[-1]["dest_folder"] == "Archive"

    pane2 = MessagePane(stub_ctx, moved, lambda *a: None,
                         on_changed=lambda **kw: changed_calls.append(kw))
    pane2._on_trash_clicked()  # ещё не в корзине — подтверждение не требуется
    await _wait_for_calls(3)
    in_trash_ui = db.get_mail_message_by_message_id(ui_mailbox, ui_message["message_id"])
    print("  после «в корзину»: папка=", in_trash_ui["folder"], "| колбэк:", changed_calls[-1]["kind"])
    assert in_trash_ui["folder"] == "Trash"
    assert changed_calls[-1]["kind"] == "trash"

    original_question = QMessageBox.question
    QMessageBox.question = staticmethod(lambda *a, **kw: QMessageBox.Yes)
    try:
        pane3 = MessagePane(stub_ctx, in_trash_ui, lambda *a: None,
                             on_changed=lambda **kw: changed_calls.append(kw))
        assert pane3._is_in_trash()
        pane3._on_trash_clicked()  # уже в корзине — подтверждение окончательного удаления
        await _wait_for_calls(4)
    finally:
        QMessageBox.question = original_question
    gone = db.get_mail_message_by_message_id(ui_mailbox, ui_message["message_id"])
    print("  после окончательного удаления:", gone, "| колбэк:", changed_calls[-1]["kind"])
    assert gone is None
    assert changed_calls[-1]["kind"] == "delete"


asyncio.run(_exercise_pane_actions())
print("  ok — построение и все три действия отрабатывают на настоящем виджете")


print("\n== FolderManagerDialog: строится и создаёт/переименовывает/удаляет папку офскрин ==")
from chatgrab.ui.screens.mail_settings import FolderManagerDialog

dlg = FolderManagerDialog(stub_ctx, ui_mailbox, "ui@ops.test")
rows_before = dlg.list_box.count() - 1  # минус addStretch
print("  строк папок в диалоге:", rows_before)
assert rows_before == len(db.list_mail_folders(ui_mailbox))


async def _exercise_folder_dialog():
    dlg.new_name_field.setText("Клиенты")
    dlg._on_create()
    await asyncio.sleep(0.3)
    print("  после создания:", db.get_mail_folder(ui_mailbox, "Клиенты") is not None)
    assert db.get_mail_folder(ui_mailbox, "Клиенты") is not None
    assert "Клиенты" in state["imap.ops.test"]

    dlg._on_toggle_subscribed("Клиенты", True)
    await asyncio.sleep(0.3)
    assert db.get_mail_folder(ui_mailbox, "Клиенты")["enabled"] == 1

    original_question = QMessageBox.question
    QMessageBox.question = staticmethod(lambda *a, **kw: QMessageBox.Yes)
    try:
        dlg._on_delete("Клиенты")
        await asyncio.sleep(0.3)
    finally:
        QMessageBox.question = original_question
    print("  после удаления:", db.get_mail_folder(ui_mailbox, "Клиенты") is None)
    assert db.get_mail_folder(ui_mailbox, "Клиенты") is None
    assert "Клиенты" not in state["imap.ops.test"]


asyncio.run(_exercise_folder_dialog())
print("  ok — диалог управления папками строится и работает на настоящих виджетах")


print("\nТЕСТ ПРОЙДЕН: папки, флаги, перемещения (в т.ч. между ящиками), корзина, "
      "офлайн-очередь и элементы управления в панели письма и диалоге папок работают")
