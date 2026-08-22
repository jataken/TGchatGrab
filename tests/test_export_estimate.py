"""Оценка выгрузки не тянет тексты сообщений и совпадает с полной выборкой."""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _bootstrap import fresh_db
from chatgrab.services.export_service import ExportService, ExportParams

paths, db = fresh_db("cgest")
db.add_chat(1, "Биржа", "b", "all", None)
db.add_chat(2, "Вторая", "v", "all", None)

N = 4000
BODY = "Ищем поставщика глицерина 99,5% пищевого, объём и цена в личку. " * 6
for i in range(1, N + 1):
    db.upsert_message({"chat_id": 1 if i % 2 else 2, "message_id": i, "chat_title": "c",
        "date": f"2026-{(i % 12) + 1:02d}-01T10:00:00", "edited_date": None,
        "sender_id": 1, "sender_username": "u", "sender_display_name": "U",
        "text": BODY + str(i), "reply_to_message_id": None, "forwarded_from": None,
        "media_type": "photo" if i % 5 == 0 else None, "media_caption": None,
        "media_path": f"photos/1/{i}.jpg" if i % 5 == 0 else None, "views": None,
        "link": "", "is_hidden": 0, "char_len": 0, "is_reply": 0, "is_forward": 0})
print(f"в базе {db.message_count()} сообщений")

svc = ExportService(db, paths)
params = ExportParams(chat_ids=[1, 2], format="jsonl", split_mode="tokens", token_limit=20000)

print("\n== лёгкая выборка не содержит текста ==")
meta = db.export_select_meta(chat_ids=[1, 2])
print("  колонки:", meta[0].keys())
assert "text" not in meta[0].keys(), "текст всё ещё тянется"
assert set(meta[0].keys()) == {"chat_id", "date", "char_len", "media_path"}

print("\n== оценка совпадает с полной выборкой ==")
full = db.export_select(chat_ids=[1, 2])
from chatgrab.services.export_service import _row_tokens
tokens_full = sum(_row_tokens(r) for r in full)
est = svc.estimate(params)
print(f"  строк: {est.row_count} (полная выборка {len(full)})")
print(f"  токенов: {est.token_count} (по полным строкам {tokens_full})")
assert est.row_count == len(full)
assert est.token_count == tokens_full, "оценка разошлась с полной выборкой"
print(f"  файлов: {est.file_count} -> {est.file_names[:3]} …")
assert est.file_count > 1, "разбивка по токенам не сработала"
assert "chatgrab_media.zip" not in est.file_names
params_zip = ExportParams(chat_ids=[1, 2], format="jsonl", zip_photos=True)
assert "chatgrab_media.zip" in svc.estimate(params_zip).file_names, "медиа-архив не учтён"

print("\n== оценка заметно быстрее полной выборки ==")
t0 = time.perf_counter()
for _ in range(5): db.export_select(chat_ids=[1, 2])
heavy = time.perf_counter() - t0
t0 = time.perf_counter()
for _ in range(5): db.export_select_meta(chat_ids=[1, 2])
light = time.perf_counter() - t0
print(f"  полная: {heavy*1000:.0f} мс за 5 прогонов, лёгкая: {light*1000:.0f} мс")
assert light < heavy, "лёгкая выборка не быстрее"

print("\n== разбивка по месяцам считается по тем же данным ==")
p_month = ExportParams(chat_ids=[1, 2], format="jsonl", split_mode="month", merge=True)
names = svc.estimate(p_month).file_names
print(f"  файлов по месяцам: {len(names)} -> {sorted(names)[:2]} …")
assert len(names) == 12, names

print("\n== фильтр «только уникальные» уменьшает оценку ==")
db.upsert_message({"chat_id": 1, "message_id": 99999, "chat_title": "c",
    "date": "2026-01-01T10:00:00", "edited_date": None, "sender_id": 1,
    "sender_username": "u", "sender_display_name": "U", "text": BODY + "1",
    "reply_to_message_id": None, "forwarded_from": None, "media_type": None,
    "media_caption": None, "media_path": None, "views": None, "link": "",
    "is_hidden": 0, "char_len": 0, "is_reply": 0, "is_forward": 0})
a = svc.estimate(ExportParams(chat_ids=[1], format="jsonl")).row_count
b = svc.estimate(ExportParams(chat_ids=[1], format="jsonl", unique_only=True)).row_count
print(f"  все: {a}, уникальные: {b}")
assert b == a - 1

print("\nТЕСТ ПРОЙДЕН: оценка выгрузки лёгкая и точная")
