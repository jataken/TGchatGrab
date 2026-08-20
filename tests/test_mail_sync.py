"""П1: синхронизация почты — без сети, вместо IMAP-сервера подставлена
заглушка соединения (тот же приём, что и с Telegram-клиентами в
test_accounts.py). Проверяется: повторный прогон не дублирует письма,
смена UIDVALIDITY перечитывает папку, кривая кодировка не роняет забор,
и что синхронизация не держит блокировку базы на время «сетевого» вызова
— иначе сбор Telegram вставал бы на время, пока идёт почта.
"""
import asyncio
import base64
import sys
import threading
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _bootstrap import fresh_env
from _fake_imap import SlowFakeImapConnection, make_client_factory
from chatgrab.integrations.mail import credentials as mail_credentials
from chatgrab.integrations.mail.credentials import register_mailbox_rotation
from chatgrab.integrations.mail.imap_client import ImapClient, autodetect
from chatgrab.services.mail_service import MailService

paths, db, config, security = fresh_env("cgmail")


# ---- тестовые письма ----------------------------------------------------
def _b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


MSG_PLAIN = (
    b"From: Ivan Petrov <ivan@example.com>\r\n"
    b"To: sales@company.ru\r\n"
    b"Subject: Zapros KP\r\n"
    b"Date: Mon, 17 Aug 2026 10:00:00 +0300\r\n"
    b"Message-ID: <msg1@example.com>\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Hello, please send a quote.\r\n"
)

MSG_CYRILLIC = (
    f"From: =?UTF-8?B?{_b64('Ирина')}?= <irina@avrora.ru>\r\n"
    f"To: sales@company.ru\r\n"
    f"Subject: =?UTF-8?B?{_b64('Запрос КП на глицерин')}?=\r\n"
    "Date: Mon, 17 Aug 2026 11:00:00 +0300\r\n"
    "Message-ID: <msg2@avrora.ru>\r\n"
    "In-Reply-To: <msg1@example.com>\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n"
    "\r\n"
    "Нужен глицерин, 2 тонны.\r\n"
).encode("utf-8")

MSG_BAD_ENCODING = (
    b"From: bad@example.com\r\n"
    b"To: sales@company.ru\r\n"
    b"Subject: =?totally-bogus-xyz?B?0LrRgNC40LLQvtC5?=\r\n"
    b"Date: Mon, 17 Aug 2026 12:00:00 +0300\r\n"
    b"Message-ID: <msg3@example.com>\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Body text.\r\n"
)


print("== автоопределение сервера по известным доменам ==")
assert autodetect("someone@yandex.ru") == ("imap.yandex.ru", 993, "smtp.yandex.ru", 465)
assert autodetect("someone@mail.ru") == ("imap.mail.ru", 993, "smtp.mail.ru", 465)
assert autodetect("someone@gmail.com")[0] == "imap.gmail.com"
assert autodetect("someone@corp-server.example") is None
print("  ok")


print("\n== два ящика синхронизируются параллельно, каждый — со своим состоянием ==")
state = {
    "imap.one.test": {
        "INBOX": {"uidvalidity": 100, "messages": {1: MSG_PLAIN, 2: MSG_CYRILLIC}},
        "Sent": {"uidvalidity": 55, "messages": {1: MSG_PLAIN}},
    },
    "imap.two.test": {
        "INBOX": {"uidvalidity": 200, "messages": {1: MSG_BAD_ENCODING}},
    },
}
mail_service = MailService(db, paths, security, client_factory=make_client_factory(state))

mb1 = db.add_mailbox("one@one.test", "imap.one.test", 993,
                      password_enc=mail_credentials.encrypt_password(security, "correct-password"))
mb2 = db.add_mailbox("two@two.test", "imap.two.test", 993,
                      password_enc=mail_credentials.encrypt_password(security, "correct-password"))

n = asyncio.run(mail_service.tick())
print("  новых писем за первый тик:", n)
assert n == 3, n
assert db.count_mail_messages(mb1) == 2
assert db.count_mail_messages(mb2) == 1
mbox1_row = db.get_mailbox(mb1)
assert mbox1_row["last_sync_at"] is not None and not mbox1_row["last_error"]
print("  ok — оба ящика синхронизированы, ошибок нет")

print("\n== письма с кириллицей декодированы правильно ==")
msg2 = db.get_mail_message_by_uid(mb1, "INBOX", 2)
print("  тема:", msg2["subject"], "| отправитель:", msg2["sender_name"])
assert msg2["subject"] == "Запрос КП на глицерин"
assert msg2["sender_name"] == "Ирина"
assert msg2["in_reply_to"] == "<msg1@example.com>"
print("  ok")

print("\n== только INBOX синхронизируется автоматически, Sent — нет ==")
folders_mb1 = {f["name"]: f for f in db.list_mail_folders(mb1)}
print("  ", {k: (v["enabled"], v["last_uid"]) for k, v in folders_mb1.items()})
assert folders_mb1["INBOX"]["enabled"] == 1 and folders_mb1["INBOX"]["last_uid"] == 2
assert folders_mb1["Sent"]["enabled"] == 0 and folders_mb1["Sent"]["last_uid"] == 0
print("  ok — Sent обнаружена, но не читалась (П4)")

print("\n== кривая кодировка не роняет забор ==")
msg3 = db.get_mail_message_by_uid(mb2, "INBOX", 1)
print("  тема:", repr(msg3["subject"]))
assert msg3["subject"], "неизвестная кодировка должна дать хоть какой-то текст, не пусто и не падение"
print("  ok — прогон не упал, письмо сохранено")

print("\n== повторный прогон не дублирует письма ==")
n2 = asyncio.run(mail_service.tick())
print("  новых писем за второй тик:", n2)
assert n2 == 0
assert db.count_mail_messages(mb1) == 2
assert db.count_mail_messages(mb2) == 1
print("  ok")

print("\n== смена UIDVALIDITY перечитывает папку ==")
state["imap.one.test"]["INBOX"] = {"uidvalidity": 999, "messages": {1: MSG_PLAIN}}
n3 = asyncio.run(mail_service.tick())
print("  новых писем после смены UIDVALIDITY:", n3)
assert db.count_mail_messages(mb1) == 1, "старые письма должны были уйти вместе со старым UIDVALIDITY"
folder_after = db.get_mail_folder(mb1, "INBOX")
assert folder_after["uidvalidity"] == 999 and folder_after["last_uid"] == 1
print("  ok — папка перечитана с чистого листа")

print("\n== недоступный/неверный пароль не роняет остальные ящики ==")
db.set_mailbox_field(mb2, password_enc=mail_credentials.encrypt_password(security, "wrong-password"))
n4 = asyncio.run(mail_service.tick())
print("  результат тика с одним неверным паролем:", n4)
assert db.get_mailbox(mb2)["last_error"], "ошибка неверного пароля должна быть записана"
assert db.get_mailbox(mb1)["last_error"] is None, "рабочий ящик не должен пострадать от чужой ошибки"
print("  ok")
db.set_mailbox_field(mb2, password_enc=mail_credentials.encrypt_password(security, "correct-password"))

print("\n== отключённый интернет оставляет запись об ошибке и ничего не роняет ==")
def _offline_factory(host, port):
    def boom():
        raise OSError("Network is unreachable")
    return ImapClient(host, port, connection_factory=boom)


offline_service = MailService(db, paths, security, client_factory=_offline_factory)
n_offline = asyncio.run(offline_service.tick())
print("  результат тика без сети:", n_offline)
assert n_offline == 0
assert db.get_mailbox(mb1)["last_error"] == "Network is unreachable"
print("  ok — ошибка записана, приложение не упало")


print("\n== выключенный ящик не трогается ==")
db.set_mailbox_field(mb2, enabled=0)
before = db.count_mail_messages(mb2)
state["imap.two.test"]["INBOX"]["messages"][2] = MSG_PLAIN
asyncio.run(mail_service.tick())
assert db.count_mail_messages(mb2) == before, "выключенный ящик не должен синхронизироваться"
print("  ok")
db.set_mailbox_field(mb2, enabled=1)


print("\n== «тело по требованию»: вложение сохраняется безопасным именем ==")
attach_msg = (
    b"From: client@example.com\r\n"
    b"To: sales@company.ru\r\n"
    b"Subject: with attachment\r\n"
    b"Date: Mon, 17 Aug 2026 13:00:00 +0300\r\n"
    b"Message-ID: <msg4@example.com>\r\n"
    b'Content-Type: multipart/mixed; boundary="B"\r\n'
    b"\r\n"
    b"--B\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"See attached.\r\n"
    b"--B\r\n"
    b'Content-Type: application/pdf; name="../../evil.pdf"\r\n'
    b'Content-Disposition: attachment; filename="../../evil.pdf"\r\n'
    b"\r\n"
    b"%PDF-fake-bytes\r\n"
    b"--B--\r\n"
)
state["imap.one.test"]["INBOX"] = {"uidvalidity": 999, "messages": {1: MSG_PLAIN, 2: attach_msg}}
asyncio.run(mail_service.tick())
attach_row = db.get_mail_message_by_uid(mb1, "INBOX", 2)
assert not attach_row["body_fetched"]
mail_service.fetch_body(attach_row["id"])
fetched = db.get_mail_message(attach_row["id"])
assert fetched["body_fetched"] == 1
assert fetched["body_text"].strip() == "See attached."
attachments = db.list_mail_attachments(attach_row["id"])
print("  вложения:", [a["filename"] for a in attachments], "| путь:", attachments[0]["path"] if attachments else None)
assert len(attachments) == 1
assert attachments[0]["filename"] == "../../evil.pdf", "имя в базе — как прислали"
saved_path = Path(attachments[0]["path"])
assert saved_path.name == "evil.pdf", "на диске — без пути обхода каталогов"
assert saved_path.exists() and saved_path.is_relative_to(paths.mail_dir)
print("  ok — путь traversal обезврежен, файл лежит внутри mail_dir")


print("\n== удаление ящика убирает и его письма ==")
db.delete_mailbox(mb2)
assert db.get_mailbox(mb2) is None
assert db.count_mail_messages(mb2) == 0
assert db.list_mail_folders(mb2) == []
print("  ok")


print("\n== пароль ящика переживает смену мастер-пароля ==")
register_mailbox_rotation(db, security)
security.enable("оченьдлинныйпарольпароль")
stored_after_enable = db.get_mailbox(mb1)["password_enc"]
assert stored_after_enable != "correct-password", "пароль должен быть зашифрован после включения защиты"
assert security.decrypt_secret(stored_after_enable) == "correct-password"
print("  ok — синхронизация продолжит работать тем же паролем после ротации")


print("\n== почта не держит блокировку базы во время «сети» ==")
state_slow = {"imap.slow.test": {"INBOX": {"uidvalidity": 1, "messages": {i: MSG_PLAIN for i in range(1, 4)}}}}
slow_service = MailService(
    db, paths, security, client_factory=make_client_factory(state_slow, SlowFakeImapConnection))
mb_slow = db.add_mailbox("slow@slow.test", "imap.slow.test", 993,
                          password_enc=mail_credentials.encrypt_password(security, "correct-password"))

hammer_elapsed = []


def hammer():
    started = time.time()
    for i in range(20):
        db.add_chat(90000 + i, f"чат {i}", None, "all", None)
    hammer_elapsed.append(time.time() - started)


t = threading.Thread(target=hammer)
t.start()
tick_started = time.time()
asyncio.run(slow_service.tick())
tick_elapsed = time.time() - tick_started
t.join()
print(f"  тик почты: {tick_elapsed:.2f}с | параллельная работа с базой: {hammer_elapsed[0]:.3f}с")
assert tick_elapsed >= 0.25, "тест должен был застать «медленный» сетевой вызов"
# Порог был 0.15с — на медленном/загруженном раннере (замечено на Windows
# CI) 20 обычных INSERT'ов иногда занимают чуть больше. Взят с большим
# запасом: «заблокировано» — это порядка тика (~1.4с здесь), так что даже
# 0.5с однозначно отличает «не ждали» от «ждали», не просто подгоняет
# число под конкретный прогон.
assert hammer_elapsed[0] < 0.5, \
    "сбор Telegram не должен ждать, пока почта работает с «сетью» — блокировка базы держится только на запись"
print("  ok — блокировка базы не удерживается на время сетевого вызова")

print("\nТЕСТ ПРОЙДЕН: синхронизация почты работает независимо от Telegram")
