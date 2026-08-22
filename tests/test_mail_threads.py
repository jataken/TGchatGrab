"""core/mail_thread.py — сборка цепочек, без sqlite и Qt. Тот же приём
проверки, что у core/lead.py: чистые функции на голых словарях.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatgrab.core import mail_thread


print("== нормализация темы снимает префиксы, включая русские и повторные ==")
cases = [
    ("Запрос КП", "запрос кп"),
    ("Re: Запрос КП", "запрос кп"),
    ("RE: Re: Запрос КП", "запрос кп"),
    ("Fwd: Запрос КП", "запрос кп"),
    ("FW: Запрос КП", "запрос кп"),
    ("Ответ: Запрос КП", "запрос кп"),
    ("Пересл: Запрос КП", "запрос кп"),
    ("[EXT] Re: Запрос КП", "запрос кп"),
    ("Re:Запрос КП", "запрос кп"),
    ("  Запрос   КП  ", "запрос кп"),
]
for raw, expected in cases:
    got = mail_thread.normalize_subject(raw)
    print(f"  {raw!r:30s} -> {got!r}")
    assert got == expected, (raw, got, expected)
assert mail_thread.normalize_subject(None) == ""
assert mail_thread.normalize_subject("") == ""
print("  ok")


print("\n== reference_ids: References + In-Reply-To, без дублей ==")
ids = mail_thread.reference_ids(
    "<a@x> <b@x> <c@x>", "<c@x>")
print("  ", ids)
assert ids == ["<a@x>", "<b@x>", "<c@x>"], ids

ids2 = mail_thread.reference_ids("<a@x>", "<b@x>")
assert ids2 == ["<a@x>", "<b@x>"], ids2

ids3 = mail_thread.reference_ids(None, None)
assert ids3 == []

ids4 = mail_thread.reference_ids("", "<only@x>")
assert ids4 == ["<only@x>"]
print("  ok")


print("\n== откат по теме: совпадение темы и участников в пределах окна — одна ветка ==")
candidates = [
    {"id": 1, "participants": {"irina@avrora.ru", "sales@company.ru"},
     "last_date": "2026-08-01T10:00:00"},
]
msg = {
    "subject": "Re: Запрос КП на глицерин",
    "sender_address": "irina@avrora.ru",
    "to_addresses": ["sales@company.ru"],
    "date": "2026-08-05T10:00:00",
}
thread_id = mail_thread.find_subject_fallback_thread(msg, candidates)
print("  ", thread_id)
assert thread_id == 1
print("  ok — «Ре:» и «Re:» с теми же участниками попадают в одну ветку")


print("\n== та же тема, но другие участники — не та же ветка ==")
msg_other_people = {
    "subject": "Запрос КП на глицерин",
    "sender_address": "someone-else@example.com",
    "to_addresses": ["different@example.com"],
    "date": "2026-08-05T10:00:00",
}
result = mail_thread.find_subject_fallback_thread(msg_other_people, candidates)
print("  ", result)
assert result is None, "разные участники не должны схлопываться в чужую ветку"
print("  ok")


print("\n== та же тема и участники, но за пределами окна — не та же ветка ==")
msg_too_late = dict(msg, date="2026-12-01T10:00:00")
result_late = mail_thread.find_subject_fallback_thread(msg_too_late, candidates)
print("  ", result_late)
assert result_late is None, "спустя месяцы это уже другой повод, не продолжение переписки"
print("  ok")


print("\n== пустая тема — не откатывается ни на что ==")
assert mail_thread.find_subject_fallback_thread(
    {"subject": "", "sender_address": "a@x", "to_addresses": [], "date": None}, candidates
) is None
print("  ok")


print("\n== без даты ни у сообщения, ни у ветки — совпадение не блокируется временем ==")
candidates_no_date = [{"id": 5, "participants": {"a@x"}, "last_date": None}]
msg_no_date = {"subject": "Тема", "sender_address": "a@x", "to_addresses": [], "date": None}
assert mail_thread.find_subject_fallback_thread(msg_no_date, candidates_no_date) == 5
print("  ok")


print("\n== несколько подходящих веток — выбирается самая свежая ==")
candidates_multi = [
    {"id": 10, "participants": {"a@x"}, "last_date": "2026-01-01T00:00:00"},
    {"id": 11, "participants": {"a@x"}, "last_date": "2026-08-01T00:00:00"},
]
msg_multi = {"subject": "Тема", "sender_address": "a@x", "to_addresses": [], "date": "2026-08-10T00:00:00"}
assert mail_thread.find_subject_fallback_thread(msg_multi, candidates_multi) == 11
print("  ok")

print("\nТЕСТ ПРОЙДЕН: сборка цепочек работает")
