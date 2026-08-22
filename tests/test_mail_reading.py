"""П2: сборка цепочек в реальном конвейере синхронизации, список цепочек,
чтение переписки, локальный и серверный поиск, пометка «прочитано» —
локально и на сервере. core/mail_thread.py's чистая логика проверена
отдельно, в test_mail_threads.py; здесь — то, что вокруг неё: очереди в
базе и настоящий (поддельный) IMAP.
"""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _bootstrap import fresh_env
from _fake_imap import make_client_factory
from chatgrab.integrations.mail import credentials as mail_credentials
from chatgrab.services.mail_service import MailService

paths, db, config, security = fresh_env("cgmailread")


def _msg(message_id, subject, sender, to, date, in_reply_to=None, body="Текст письма."):
    headers = [
        f"From: {sender}",
        f"To: {to}",
        f"Subject: {subject}",
        f"Date: {date}",
        f"Message-ID: <{message_id}>",
    ]
    if in_reply_to:
        headers.append(f"In-Reply-To: <{in_reply_to}>")
    headers.append("Content-Type: text/plain; charset=utf-8")
    return ("\r\n".join(headers) + "\r\n\r\n" + body + "\r\n").encode("utf-8")


state = {
    "imap.read.test": {
        "INBOX": {"uidvalidity": 1, "messages": {
            # Ветка 1: запрос + ответ по References/In-Reply-To.
            1: _msg("q1@avrora.ru", "Запрос КП на глицерин", "irina@avrora.ru",
                    "sales@company.ru", "Mon, 17 Aug 2026 10:00:00 +0300",
                    body="Нужен глицерин, пришлите КП."),
            2: _msg("q2@company.ru", "Re: Запрос КП на глицерин", "sales@company.ru",
                    "irina@avrora.ru", "Mon, 17 Aug 2026 11:00:00 +0300",
                    in_reply_to="q1@avrora.ru", body="Вот наше предложение."),
            # Ветка 2: тот же «повод» по теме, но без References — должна
            # склеиться через откат на тему+участников (ответ той же Ирины).
            3: _msg("q3@avrora.ru", "Запрос КП на глицерин", "irina@avrora.ru",
                    "sales@company.ru", "Mon, 17 Aug 2026 12:00:00 +0300",
                    body="Уточнение: нужно 2 тонны, не 1."),
            # Ветка 3: та же тема, но другой человек — не должна слиться
            # ни с веткой 1, ни с веткой 2.
            4: _msg("q4@drugoy.ru", "Запрос КП на глицерин", "petr@drugoy.ru",
                    "sales@company.ru", "Mon, 17 Aug 2026 13:00:00 +0300",
                    body="Тоже интересует глицерин."),
            # Отдельная тема — заведомо отдельная ветка.
            5: _msg("q5@drugoy.ru", "Вопрос по счёту №42", "buh@drugoy.ru",
                    "sales@company.ru", "Mon, 17 Aug 2026 14:00:00 +0300",
                    body="Когда ждать оплату по счёту №42?"),
        }},
    },
}

svc = MailService(db, paths, security, client_factory=make_client_factory(state))
mb = db.add_mailbox("sales@company.ru", "imap.read.test", 993,
                     password_enc=mail_credentials.encrypt_password(security, "correct-password"))

n = asyncio.run(svc.tick())
print("== письма синхронизированы ==")
print("  новых писем:", n)
assert n == 5


print("\n== ответ по References/In-Reply-To попадает в ту же ветку ==")
m1 = db.get_mail_message_by_uid(mb, "INBOX", 1)
m2 = db.get_mail_message_by_uid(mb, "INBOX", 2)
print("  ветка письма 1:", m1["thread_id"], "| ветка письма 2:", m2["thread_id"])
assert m1["thread_id"] == m2["thread_id"]
print("  ok")


print("\n== та же тема, те же участники, без References — откат по теме склеивает ==")
m3 = db.get_mail_message_by_uid(mb, "INBOX", 3)
print("  ветка письма 3:", m3["thread_id"])
assert m3["thread_id"] == m1["thread_id"], "уточнение от того же отправителя должно попасть в ту же ветку"
print("  ok")


print("\n== та же тема, другой человек — отдельная ветка ==")
m4 = db.get_mail_message_by_uid(mb, "INBOX", 4)
print("  ветка письма 4:", m4["thread_id"])
assert m4["thread_id"] != m1["thread_id"], "чужой человек не должен попасть в переписку с Ириной"
print("  ok")


print("\n== список цепочек: три ветки, у первой — три письма ==")
threads = db.list_mail_threads(mb)
print("  всего цепочек:", len(threads))
assert len(threads) == 3, [dict(t) for t in threads]
by_id = {t["thread_id"]: t for t in threads}
assert by_id[m1["thread_id"]]["message_count"] == 3
assert by_id[m4["thread_id"]]["message_count"] == 1
# Свежая активность сверху.
assert threads[0]["thread_id"] == db.get_mail_message_by_uid(mb, "INBOX", 5)["thread_id"]
print("  ok — сортировка по свежей активности, счётчики верные")


print("\n== чтение ветки: письма по порядку дат ==")
msgs = db.list_thread_messages(m1["thread_id"])
print("  ", [m["uid"] for m in msgs])
assert [m["uid"] for m in msgs] == [1, 2, 3]
print("  ok")


print("\n== все письма изначально непрочитаны ==")
assert by_id[m1["thread_id"]]["unread_count"] == 3
print("  ok")


print("\n== пометка «прочитано» — локально ==")
changed = db.mark_thread_read(m1["thread_id"])
print("  изменено писем:", len(changed))
assert len(changed) == 3  # письма 1, 2, 3 в одной ветке
assert all(row["is_read"] == 0 for row in changed), "changed должен вернуть то, что БЫЛО непрочитанным"
assert db.get_mail_message(m1["id"])["is_read"] == 1
assert db.get_mail_message(m2["id"])["is_read"] == 1
# повторная пометка уже прочитанной ветки — ничего не меняет
assert db.mark_thread_read(m1["thread_id"]) == []
print("  ok")


print("\n== пометка «прочитано» доходит и до сервера ==")
items = [(row["folder"], row["uid"]) for row in changed]
svc.push_read_flags(mb, items)
seen_on_server = state["imap.read.test"]["INBOX"].get("seen", set())
print("  отмечено на сервере:", sorted(seen_on_server))
assert seen_on_server == {1, 2, 3}
print("  ok — STORE ушёл на сервер именно за эти письма")


print("\n== локальный поиск по FTS находит письмо по теме сразу ==")
found_subject = db.search_mail(mb, "счёту")
print("  найдено по теме:", [(r["uid"], r["subject"]) for r in found_subject])
assert any(r["uid"] == 5 for r in found_subject)
print("  ok")


print("\n== по тексту тела — только после того, как тело забрано («по требованию») ==")
before_fetch = db.search_mail(mb, "2 тонны")
print("  до fetch_body:", [r["uid"] for r in before_fetch])
assert not any(r["uid"] == 3 for r in before_fetch), \
    "тело письма 3 ещё не забрано — искать в нём нечего, это не баг"
svc.fetch_body(m3["id"])
after_fetch = db.search_mail(mb, "2 тонны")
print("  после fetch_body:", [r["uid"] for r in after_fetch])
assert any(r["uid"] == 3 for r in after_fetch), "после fetch_body текст должен попасть в FTS-индекс"
print("  ok — двухфазный забор: заголовки сразу ищутся, тело — после явного запроса")


print("\n== серверный поиск подтягивает письмо, которого ещё нет локально ==")
state["imap.read.test"]["INBOX"]["messages"][6] = _msg(
    "q6@drugoy.ru", "Срочный вопрос про упаковку", "zakaz@drugoy.ru",
    "sales@company.ru", "Mon, 17 Aug 2026 15:00:00 +0300",
    body="Ищем поставщика картонной упаковки, объём небольшой.")
before = db.count_mail_messages(mb)
pulled = svc.search_server(mb, "INBOX", "картонной упаковки")
after = db.count_mail_messages(mb)
print("  подтянуто новых писем:", pulled, "| было:", before, "| стало:", after)
assert pulled == 1
assert after == before + 1
m6 = db.get_mail_message_by_uid(mb, "INBOX", 6)
assert m6 is not None and m6["subject"] == "Срочный вопрос про упаковку"
assert m6["thread_id"] is not None, "письмо, найденное поиском, тоже должно попасть в свою ветку"
print("  ok — найденное на сервере письмо сохранено и привязано к ветке")

print("\n== повторный серверный поиск не тянет то же письмо снова ==")
pulled_again = svc.search_server(mb, "INBOX", "картонной упаковки")
assert pulled_again == 0
assert db.count_mail_messages(mb) == after
print("  ok")


print("\nТЕСТ ПРОЙДЕН: цепочки, список, чтение, поиск и пометка «прочитано» работают")
