"""Разрывы в собранной последовательности видны и латаются."""
import os, sys
import tempfile
import shutil
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from chatgrab.paths import Paths
from chatgrab.db.database import Database

base = os.path.join(tempfile.gettempdir(), "cggaps"); shutil.rmtree(base, ignore_errors=True)
paths = Paths(Path(base)); paths.ensure()
db = Database(paths.db_path)
db.add_chat(1, "Чат", "c", "all", None)

def put(mid):
    db.upsert_message({"chat_id":1,"message_id":mid,"chat_title":"Чат","date":f"2026-08-01T00:00:{mid%60:02d}",
        "edited_date":None,"sender_id":1,"sender_username":"u","sender_display_name":"U",
        "text":f"m{mid}","reply_to_message_id":None,"forwarded_from":None,"media_type":None,
        "media_caption":None,"media_path":None,"views":None,"link":"","is_hidden":0,
        "char_len":2,"is_reply":0,"is_forward":0})

print("== сплошная последовательность ==")
for i in range(1, 11): put(i)
s = db.gap_summary(1); print("  ", s)
assert s == {"gaps": 0, "missing": 0}

print("\n== два разрыва ==")
for i in [15, 16, 25]: put(i)   # пропуски 11-14 (4) и 17-24 (8)
s = db.gap_summary(1); print("  ", s)
assert s["gaps"] == 2, s
assert s["missing"] == 12, s
# сверяем с эталонной реализацией
ranges = db.find_gaps(1)
print("   диапазоны:", ranges)
assert len(ranges) == 2 and sum(e - b + 1 for b, e in ranges) == s["missing"]

print("\n== латание закрывает разрыв ==")
for i in range(11, 15): put(i)
s = db.gap_summary(1); print("  ", s)
assert s["gaps"] == 1 and s["missing"] == 8

print("\n== пустой чат не ломает подсчёт ==")
db.add_chat(2, "Пустой", "e", "all", None)
print("  ", db.gap_summary(2))
assert db.gap_summary(2) == {"gaps": 0, "missing": 0}

print("\n== одно сообщение ==")
db.add_chat(3, "Одно", "o", "all", None)
db.upsert_message({"chat_id":3,"message_id":7,"chat_title":"Одно","date":"2026-08-01T00:00:00",
    "edited_date":None,"sender_id":1,"sender_username":"u","sender_display_name":"U","text":"x",
    "reply_to_message_id":None,"forwarded_from":None,"media_type":None,"media_caption":None,
    "media_path":None,"views":None,"link":"","is_hidden":0,"char_len":1,"is_reply":0,"is_forward":0})
print("  ", db.gap_summary(3))
assert db.gap_summary(3) == {"gaps": 0, "missing": 0}

print("\nТЕСТ ПРОЙДЕН: разрывы считаются верно")
