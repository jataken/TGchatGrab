"""П6: ярлыки на цепочках (не на письмах) и скоростной триаж с клавиатуры —
создание/переименование/удаление ярлыка, горячие цифры без коллизий,
"клик по плашке снимает ярлык", массовое применение на выделение, отражение
в IMAP-ключевые слова через ту же офлайн-очередь, что и у П4, архивирование
цепочки клавишей E, и полноэкранный TriageDialog (J/K/1-9/E/R//).
"""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _bootstrap import fresh_env
from _fake_imap import make_client_factory
from chatgrab.core import mail_labels
from chatgrab.integrations.mail import credentials as mail_credentials
from chatgrab.services.mail_service import MailService

paths, db, config, security = fresh_env("cgmaillabels")


def _msg(message_id, subject, sender, to, date, body="Текст."):
    headers = (
        f"From: {sender}\r\nTo: {to}\r\nSubject: {subject}\r\nDate: {date}\r\n"
        f"Message-ID: <{message_id}>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n"
    )
    return headers.encode("utf-8")


async def _wait_for(predicate, timeout=5.0, step=0.05):
    """Опрос вместо фиксированного sleep — тот же приём, что в
    test_mail_ops.py после гонки на Windows CI (см. PLAN.md, журнал П5):
    fire()/run_in_executor может завершиться позже, чем за 0.3с, на
    загруженном раннере."""
    elapsed = 0.0
    while not predicate() and elapsed < timeout:
        await asyncio.sleep(step)
        elapsed += step
    assert predicate(), f"условие не выполнилось за {timeout}с"


print("== чистая функция: keyword по id ярлыка, а не по имени ==")
assert mail_labels.label_keyword(3) == "ChatGrabLabel3"
assert mail_labels.label_keyword(3) == mail_labels.label_keyword(3)
assert mail_labels.label_keyword(3) != mail_labels.label_keyword(4)
print("  ok — переименование ярлыка не меняет keyword (см. docstring)")


state = {
    "imap.labels.test": {
        "INBOX": {"uidvalidity": 1, "messages": {
            1: _msg("l1@x.ru", "Запрос КП", "irina@x.ru", "a@labels.test",
                     "Mon, 17 Aug 2026 10:00:00 +0300", body="Нужен глицерин."),
            2: _msg("l2@x.ru", "Про счёт", "buh@x.ru", "a@labels.test",
                     "Mon, 17 Aug 2026 11:00:00 +0300", body="Когда оплата?"),
            3: _msg("l3@x.ru", "Ещё один запрос", "ivan@x.ru", "a@labels.test",
                     "Mon, 17 Aug 2026 12:00:00 +0300", body="Нужна цена."),
        }},
        "Archive": {"uidvalidity": 1, "messages": {}, "special_use": "Archive"},
    },
}

svc = MailService(db, paths, security, client_factory=make_client_factory(state))
mb = db.add_mailbox("a@labels.test", "imap.labels.test", 993,
                     password_enc=mail_credentials.encrypt_password(security, "correct-password"))
asyncio.run(svc.tick())

t1 = db.get_mail_message_by_uid(mb, "INBOX", 1)["thread_id"]
t2 = db.get_mail_message_by_uid(mb, "INBOX", 2)["thread_id"]
t3 = db.get_mail_message_by_uid(mb, "INBOX", 3)["thread_id"]


print("\n== набор по умолчанию: шесть ярлыков, цифры 1-6, повторный вызов не плодит дублей ==")
db.seed_default_mail_labels(mb)
labels = db.list_mail_labels(mb)
print("  ", [(l["name"], l["hotkey"]) for l in labels])
assert [l["name"] for l in labels] == [n for n, _ in mail_labels.DEFAULT_LABELS]
assert [l["hotkey"] for l in labels] == [1, 2, 3, 4, 5, 6]
db.seed_default_mail_labels(mb)
assert len(db.list_mail_labels(mb)) == 6, "повторный сид не должен дублировать"
print("  ok")

order_label = next(l for l in labels if l["name"] == "Заказ")
urgent_label = next(l for l in labels if l["name"] == "Срочно")


print("\n== создание с занятой цифрой отклоняется, без цифры — можно сколько угодно ==")
clash = svc.create_label(mb, "Клон", "#000000", hotkey=1)
assert clash is None, "цифра 1 уже занята «Заказом»"
free = svc.create_label(mb, "Черновик", "#111111", hotkey=None)
another_free = svc.create_label(mb, "Ещё без цифры", "#222222", hotkey=None)
assert free is not None and another_free is not None
print("  ok")


print("\n== переименование не меняет keyword, смена цифры на занятую отклоняется ==")
kw_before = mail_labels.label_keyword(order_label["id"])
assert svc.update_label(order_label["id"], name="Заказ (переим.)") is True
assert mail_labels.label_keyword(order_label["id"]) == kw_before
assert svc.update_label(order_label["id"], hotkey=2) is False, "цифра 2 занята «Запрос КП»"
assert db.get_mail_label(order_label["id"])["hotkey"] == 1, "цифра не должна была смениться"
print("  ok")


print("\n== ярлык на цепочке, не на письме: применяется ко всем письмам цепочки, без дублей ==")
svc.set_thread_label(t1, order_label["id"], True)
svc.set_thread_label(t1, order_label["id"], True)  # повтор — не должен дублировать
applied = db.list_labels_for_thread(t1)
print("  на цепочке:", [l["name"] for l in applied])
assert [l["id"] for l in applied] == [order_label["id"]]
keyword = mail_labels.label_keyword(order_label["id"])
server_flags = state["imap.labels.test"]["INBOX"]["flags"]
print("  на сервере (флаг письма 1):", server_flags.get(1, set()))
assert keyword in server_flags.get(1, set())
pending = db.list_pending_mail_actions(mb)
assert pending == [], "должно было примениться сразу же — очередь пуста"
print("  ok — ярлык мгновенно и локально, и на сервере (ключевым флагом)")


print("\n== снятие ярлыка («клик по плашке») убирает и локально, и на сервере ==")
svc.set_thread_label(t1, order_label["id"], False)
assert db.list_labels_for_thread(t1) == []
assert keyword not in state["imap.labels.test"]["INBOX"]["flags"].get(1, set())
print("  ok")


print("\n== массовое применение: один ярлык на несколько выделенных цепочек одним вызовом ==")
svc.apply_label_to_threads([t1, t2, t3], urgent_label["id"])
for tid, uid in ((t1, 1), (t2, 2), (t3, 3)):
    names = {l["name"] for l in db.list_labels_for_thread(tid)}
    assert "Срочно" in names
    assert mail_labels.label_keyword(urgent_label["id"]) in state["imap.labels.test"]["INBOX"]["flags"].get(uid, set())
print("  ok — применилось ко всем трём, включая пуш на сервер")


print("\n== удаление ярлыка снимает его со всех цепочек и не оставляет сирот ==")
svc.delete_label(urgent_label["id"])
assert db.get_mail_label(urgent_label["id"]) is None
for tid in (t1, t2, t3):
    assert db.list_labels_for_thread(tid) == []
for uid in (1, 2, 3):
    assert mail_labels.label_keyword(urgent_label["id"]) not in state["imap.labels.test"]["INBOX"]["flags"].get(uid, set())
print("  ok — ни локальных, ни серверных следов не осталось")


print("\n== архивирование цепочки (клавиша E в триаже) переносит все её письма в Archive ==")
moved = svc.archive_thread(t2)
print("  перенесено писем:", moved)
assert moved == 1
m2 = db.get_mail_message_by_uid(mb, "INBOX", 2)
assert m2 is None, "письмо должно было пересинкнуться в Archive под новым UID и исчезнуть из INBOX"
assert len(state["imap.labels.test"]["Archive"]["messages"]) == 1
moved_again = svc.archive_thread(t2)
assert moved_again == 0, "уже в архиве — второй раз двигать нечего"
print("  ok")


# ---- UI: LabelManagerDialog, панель ярлыков в списке цепочек, TriageDialog ----
print("\n== UI офскрин: LabelManagerDialog, плашки в списке цепочек, TriageDialog ==")
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QMessageBox
app = QApplication.instance() or QApplication(sys.argv)

from chatgrab.ui.screens.mail import MailScreen
from chatgrab.ui.screens.mail_settings import LabelManagerDialog
import chatgrab.ui.screens.mail.triage as triage_module


class _StubCtx:
    def __init__(self, database, service):
        self.db = database
        self.mail_service = service


stub_ctx = _StubCtx(db, svc)

mb2 = db.add_mailbox("ui@labels.test", "imap.labels.test", 993,
                      password_enc=mail_credentials.encrypt_password(security, "correct-password"))
db.seed_default_mail_labels(mb2)

print("\n-- LabelManagerDialog: список, добавление, коллизия цифры, переименование, удаление --")
dlg = LabelManagerDialog(stub_ctx, mb2, "ui@labels.test")
rows_before = dlg.list_box.count() - 1  # минус addStretch
assert rows_before == 6
dlg.name_field.set_text("Партнёр")
idx = dlg.hotkey_combo.findData(1)
assert idx == -1, "цифра 1 уже занята дефолтным набором — её не должно быть в списке"
dlg._on_add()  # без цифры, добавится
assert len(db.list_mail_labels(mb2)) == 7
print("  добавлен без цифры:", db.list_mail_labels(mb2)[-1]["name"])

new_label = db.list_mail_labels(mb2)[-1]
import chatgrab.ui.screens.mail_settings as mail_settings_module
original_ask_text = mail_settings_module._ask_text
mail_settings_module._ask_text = lambda *a, **kw: ("Партнёры (переим.)", True)
try:
    dlg._on_rename(new_label["id"], new_label["name"])
finally:
    mail_settings_module._ask_text = original_ask_text
assert db.get_mail_label(new_label["id"])["name"] == "Партнёры (переим.)"
print("  переименован:", db.get_mail_label(new_label["id"])["name"])

original_question = QMessageBox.question
QMessageBox.question = staticmethod(lambda *a, **kw: QMessageBox.Yes)


async def _delete_via_dialog():
    dlg._on_delete(new_label["id"], "Партнёры (переим.)")
    await _wait_for(lambda: db.get_mail_label(new_label["id"]) is None)


try:
    asyncio.run(_delete_via_dialog())
finally:
    QMessageBox.question = original_question
assert len(db.list_mail_labels(mb2)) == 6
print("  ok — диалог управления ярлыками строится и работает на настоящих виджетах")


print("\n-- MailScreen: плашка ярлыка в строке цепочки, клик снимает --")
state["imap.labels.test"]["INBOX"]["messages"][50] = _msg(
    "ui1@x.ru", "Для UI-проверки ярлыков", "ui-sender@x.ru", "ui@labels.test",
    "Mon, 17 Aug 2026 15:00:00 +0300")
asyncio.run(svc.tick())
ui_message = db.get_mail_message_by_uid(mb2, "INBOX", 50)
ui_thread_id = ui_message["thread_id"]
ui_label = db.list_mail_labels(mb2)[0]
svc.set_thread_label(ui_thread_id, ui_label["id"], True)

screen = MailScreen(stub_ctx, lambda *a, **kw: None)
screen.selected_mailbox_id = mb2
screen.selected_folder = "INBOX"
screen._load_threads()

found_row = None
for i in range(screen.thread_list.count()):
    item = screen.thread_list.item(i)
    if item.data(Qt.UserRole) == ui_thread_id:
        found_row = screen.thread_list.itemWidget(item)
        break
assert found_row is not None, "строка с меткой не найдена в списке цепочек"
print("  строка построена, заголовок:", found_row.title_label.text().splitlines()[0])


async def _click_chip_and_wait():
    screen._on_label_chip_clicked(ui_thread_id, ui_label["id"])
    await _wait_for(lambda: db.list_labels_for_thread(ui_thread_id) == [])


asyncio.run(_click_chip_and_wait())
print("  ok — клик по плашке в строке цепочки снял ярлык")


print("\n-- MailScreen: массовое применение к выделению --")
svc.set_thread_label(ui_thread_id, ui_label["id"], False)  # на всякий случай, чистое состояние
screen._load_threads()
screen.thread_list.selectAll()
selected_ids = [screen.thread_list.item(i).data(Qt.UserRole) for i in range(screen.thread_list.count())]
assert len(selected_ids) >= 1


async def _bulk_apply():
    screen._apply_bulk_label(selected_ids, ui_label["id"])
    await _wait_for(lambda: all(
        any(l["id"] == ui_label["id"] for l in db.list_labels_for_thread(tid)) for tid in selected_ids))


asyncio.run(_bulk_apply())
print("  ok — «Ярлык на выделенное» применился ко всем строкам списка")


print("\n-- TriageDialog: очередь непрочитанного, J/K, цифра ставит/снимает ярлык, E — архив --")
# Свежий непрочитанный набор — предыдущие шаги пометили часть писем
# прочитанными, так что триаж строит очередь заново под свою мини-сцену.
tri_mailbox = db.add_mailbox("tri@labels.test", "imap.tri.test", 993,
                              password_enc=mail_credentials.encrypt_password(security, "correct-password"))
tri_state = {
    "imap.tri.test": {
        "INBOX": {"uidvalidity": 1, "messages": {
            1: _msg("tri1@x.ru", "Триаж 1", "a@x.ru", "tri@labels.test", "Mon, 17 Aug 2026 09:00:00 +0300"),
            2: _msg("tri2@x.ru", "Триаж 2", "b@x.ru", "tri@labels.test", "Mon, 17 Aug 2026 09:05:00 +0300"),
        }},
        "Archive": {"uidvalidity": 1, "messages": {}, "special_use": "Archive"},
    },
}
tri_svc = MailService(db, paths, security, client_factory=make_client_factory(tri_state))
tri_ctx = _StubCtx(db, tri_svc)
asyncio.run(tri_svc.tick())
db.seed_default_mail_labels(tri_mailbox)
tri_labels = db.list_mail_labels(tri_mailbox)
tri_order_label = next(l for l in tri_labels if l["hotkey"] == 1)

search_calls = []
dlg2 = triage_module.TriageDialog(tri_ctx, tri_mailbox, folder="INBOX", on_search=lambda: search_calls.append(1))
assert dlg2.counter_label.text() == "Осталось: 2"
first_thread = dlg2._current_thread_id()
assert db.get_mail_message(db.list_thread_messages(first_thread)[0]["id"])["is_read"] == 1, \
    "открытая в триаже цепочка отмечается прочитанной"

event_j = QKeyEvent(QEvent.KeyPress, Qt.Key_J, Qt.NoModifier)
dlg2.keyPressEvent(event_j)
assert dlg2.counter_label.text() == "Осталось: 1"
second_thread = dlg2._current_thread_id()
assert second_thread != first_thread

event_k = QKeyEvent(QEvent.KeyPress, Qt.Key_K, Qt.NoModifier)
dlg2.keyPressEvent(event_k)
assert dlg2._current_thread_id() == first_thread
print("  J/K листают вперёд-назад:", "ok")


async def _digit_toggles_label():
    event_1 = QKeyEvent(QEvent.KeyPress, Qt.Key_1, Qt.NoModifier, "1")
    dlg2.keyPressEvent(event_1)
    await _wait_for(lambda: any(
        l["id"] == tri_order_label["id"] for l in db.list_labels_for_thread(first_thread)))
    dlg2.keyPressEvent(event_1)  # тот же клавиш — снимает обратно
    await _wait_for(lambda: db.list_labels_for_thread(first_thread) == [])


asyncio.run(_digit_toggles_label())
print("  цифра 1 ставит и снимает ярлык:", "ok")


async def _archive_advances():
    event_e = QKeyEvent(QEvent.KeyPress, Qt.Key_E, Qt.NoModifier)
    dlg2.keyPressEvent(event_e)
    await _wait_for(lambda: dlg2._current_thread_id() != first_thread)


asyncio.run(_archive_advances())
assert dlg2._current_thread_id() == second_thread, "после архивации должна была подставиться следующая цепочка"
print("  E архивирует текущую и переходит к следующей:", "ok")


class _StubComposeDialog:
    calls = []

    def __init__(self, ctx, draft_id, parent=None):
        _StubComposeDialog.calls.append(draft_id)

    def exec(self):
        return 0


original_compose_dialog = triage_module.ComposeDialog
triage_module.ComposeDialog = _StubComposeDialog
try:
    event_r = QKeyEvent(QEvent.KeyPress, Qt.Key_R, Qt.NoModifier)
    dlg2.keyPressEvent(event_r)
finally:
    triage_module.ComposeDialog = original_compose_dialog
assert len(_StubComposeDialog.calls) == 1, "R должен был открыть ComposeDialog с черновиком ответа"
print("  R открывает ComposeDialog с черновиком ответа:", "ok")

event_slash = QKeyEvent(QEvent.KeyPress, Qt.Key_Slash, Qt.NoModifier, "/")
dlg2.keyPressEvent(event_slash)
assert search_calls == [1], "/ должен передать управление колбэку поиска на экране «Почта»"
print("  / выходит и передаёт фокус поиску:", "ok")

print("\nТЕСТ ПРОЙДЕН: ярлыки на цепочках, горячие цифры, массовое применение, "
      "IMAP-ключевые слова, архивирование и режим триажа работают")
