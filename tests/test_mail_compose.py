"""П5: core/mail_compose.py — subject prefixing, References-chain
building, reply/forward quoting, the "text says attached but nothing is"
heuristic, and MIME construction. No sqlite, no Qt, no network — same
contract as core/mail_thread.py, tested the same way.
"""
import email
import email.policy
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatgrab.core import mail_compose as mc

print("== Re:/Fwd: не дублируются на письме, у которого префикс уже есть ==")
assert mc.reply_subject("Запрос КП") == "Re: Запрос КП"
assert mc.reply_subject("Re: Запрос КП") == "Re: Запрос КП"
assert mc.reply_subject("RE: запрос") == "RE: запрос"
assert mc.reply_subject("Ответ: запрос") == "Ответ: запрос"
assert mc.reply_subject("") == "Re:"
assert mc.forward_subject("Запрос КП") == "Fwd: Запрос КП"
assert mc.forward_subject("Fwd: Запрос КП") == "Fwd: Запрос КП"
assert mc.forward_subject("Пересл: старое") == "Пересл: старое"
print("  ok")


print("\n== References: цепочка расширяется письмом, на которое отвечаем ==")
assert mc.build_references("<a@x> <b@x>", "<b@x>") == "<a@x> <b@x>", \
    "id письма уже последний в цепочке — дублировать не нужно"
assert mc.build_references("<a@x>", "<b@x>") == "<a@x> <b@x>"
assert mc.build_references(None, "<c@x>") == "<c@x>"
assert mc.build_references(None, None) is None
assert mc.build_references("", "") is None
print("  ok")


print("\n== цитата: заголовок + «> » перед каждой строкой ==")
header = mc.quote_header("Ирина Петрова", "irina@x.ru", "2026-08-17T10:00:00+03:00")
print("  заголовок:", header)
assert header == "17.08.2026, 10:00, Ирина Петрова писал(а):"
quoted = mc.quote_body("Привет\nКак дела?\n\nЖдём КП.", header)
print(quoted)
lines = quoted.splitlines()
assert lines[0] == header
assert lines[1] == "> Привет"
assert lines[2] == "> Как дела?"
assert lines[3] == ">"  # пустая строка тоже цитируется, просто без пробела после >
assert lines[4] == "> Ждём КП."
print("  ok")

no_name = mc.quote_header(None, "irina@x.ru", None)
assert no_name == "irina@x.ru писал(а):"
print("  ok — без имени и даты тоже не падает")


print("\n== пересылка: блок не цитируется «>», показывает исходные реквизиты ==")
block = mc.forward_block("Пётр", "petr@x.ru", "2026-08-17T09:00:00+03:00",
                          ["sales@company.ru"], "Запрос", "Текст письма.")
print(block)
assert "От: Пётр" in block
assert "Тема: Запрос" in block
assert "Кому: sales@company.ru" in block
assert "Текст письма." in block
assert not any(line.startswith(">") for line in block.splitlines())
print("  ok")


print("\n== эвристика «в тексте написано „вложение“, а вложений нет» ==")
assert mc.mentions_attachment("Отправляю во вложении прайс")
assert mc.mentions_attachment("Прилагаю КП")
assert mc.mentions_attachment("Файл приложен")
assert mc.mentions_attachment("See attached file")
assert mc.mentions_attachment("Please find the attachment")
assert not mc.mentions_attachment("Просто текст без файлов")
assert not mc.mentions_attachment("")
print("  ok")


print("\n== число получателей и суммарный размер вложений — простые агрегаты ==")
assert mc.total_recipients(["a@x", "B@X"], ["a@x", "c@x"]) == 3, "повтор адреса в разном регистре — один получатель"
assert mc.total_recipients([], []) == 0
assert mc.total_attachment_size([1000, 2000, None]) == 3000
assert mc.ATTACHMENT_WARN_BYTES == 20 * 1024 * 1024
print("  ok")


print("\n== MIME: заголовки, тело, вложение — то, что реально уйдёт по сети ==")
tmp = Path(tempfile.mktemp(suffix=".txt"))
tmp.write_bytes(b"file content")
raw = mc.build_mime_message(
    "sales@company.ru", "Отдел продаж", ["client@x.ru"], ["boss@company.ru"],
    "Re: Запрос КП", "Добрый день!\nВысылаем КП.",
    in_reply_to="<orig@x.ru>", references="<orig@x.ru>",
    attachments=[("price.txt", str(tmp))])
parsed = email.message_from_bytes(raw, policy=email.policy.default)
print("  From:", parsed["From"], "| To:", parsed["To"], "| Cc:", parsed["Cc"])
assert parsed["To"] == "client@x.ru"
assert parsed["Cc"] == "boss@company.ru"
assert parsed["Subject"] == "Re: Запрос КП"
assert parsed["In-Reply-To"] == "<orig@x.ru>"
assert parsed["References"] == "<orig@x.ru>"
assert parsed["Message-ID"], "у каждого отправленного письма должен быть свой Message-ID"
assert parsed["From"].endswith("<sales@company.ru>")
assert parsed.is_multipart()
body = parsed.get_body(preferencelist=("plain",)).get_content()
assert "Высылаем КП." in body
attachments = list(parsed.iter_attachments())
assert len(attachments) == 1 and attachments[0].get_filename() == "price.txt"
assert attachments[0].get_content() == b"file content"
tmp.unlink()
print("  ok — заголовки цепочки, тело и вложение собраны верно")

no_reply_raw = mc.build_mime_message("a@x.ru", None, ["b@x.ru"], [], "Тема", "Текст")
parsed2 = email.message_from_bytes(no_reply_raw, policy=email.policy.default)
assert parsed2["In-Reply-To"] is None and parsed2["References"] is None
assert not parsed2.is_multipart(), "без вложений письмо должно оставаться однокомпонентным"
print("  ok — новое письмо без ответа/вложений не тащит лишние заголовки")

print("\nТЕСТ ПРОЙДЕН: составление письма — темы, цепочка, цитаты, эвристика, MIME")
