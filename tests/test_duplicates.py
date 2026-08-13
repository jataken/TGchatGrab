"""Повторы одного и того же текста узнаются и убираются из выгрузки."""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from chatgrab.paths import Paths
from chatgrab.db.database import Database
from chatgrab.db.dedup import fingerprint, normalize, MIN_LENGTH

OFFER = "Флаконы ПЭТ 250 мл с помпой в наличии, 12 000 шт на складе в Москве"

print("== нормализация ==")
assert fingerprint(OFFER) == fingerprint(OFFER.upper()), "регистр должен игнорироваться"
assert fingerprint(OFFER) == fingerprint(OFFER.replace(" ", "  ")), "пробелы должны схлопываться"
assert fingerprint(OFFER) == fingerprint("*** " + OFFER + " ***"), "декор должен игнорироваться"
assert fingerprint(OFFER) != fingerprint(OFFER.replace("250", "500")), "разные объёмы — разный текст"
print("  регистр/пробелы/декор игнорируются, цифры — нет")

print("\n== короткие тексты не считаются повторами ==")
for short in ["да", "в личку", "актуально?", "+", "напишите цену"]:
    assert fingerprint(short) is None, short
assert fingerprint("") is None
print(f"  тексты короче {MIN_LENGTH} символов не получают отпечаток")

base = "/tmp/cgdup"; os.system(f"rm -rf {base}")
paths = Paths(Path(base)); paths.ensure()
db = Database(paths.db_path)
db.add_chat(1, "Биржа", "b", "all", None)
db.add_chat(2, "Другая", "d", "all", None)

def put(chat, mid, text, day=1):
    db.upsert_message({"chat_id":chat,"message_id":mid,"chat_title":"c",
        "date":f"2026-08-{day:02d}T10:00:00","edited_date":None,"sender_id":1,
        "sender_username":"u","sender_display_name":"U","text":text,
        "reply_to_message_id":None,"forwarded_from":None,"media_type":None,
        "media_caption":None,"media_path":None,"views":None,"link":"",
        "is_hidden":0,"char_len":len(text),"is_reply":0,"is_forward":0})

put(1, 1, OFFER, day=1)
put(1, 2, "Ищем поставщика глицерина 99,5% пищевого, 2 тонны в месяц регулярно", day=2)
put(1, 3, OFFER, day=8)             # тот же оффер через неделю
put(1, 4, OFFER.upper(), day=15)    # он же капсом
put(1, 5, "да", day=15)             # короткое — не повтор
put(1, 6, "да", day=15)
put(2, 1, OFFER, day=9)             # тот же текст, но в другом чате

print("\n== сводка по повторам ==")
s = db.repeat_summary([1])
print("  чат 1:", s)
assert s["repeats"] == 2 and s["groups"] == 1, s
print("  чат 2:", db.repeat_summary([2]))
assert db.repeat_summary([2])["repeats"] == 0, "повтор посчитан между разными чатами"

print("\n== выгрузка ==")
allrows = db.export_select([1])
uniq = db.export_select([1], unique_only=True)
print(f"  всего {len(allrows)}, уникальных {len(uniq)}")
assert len(allrows) == 6 and len(uniq) == 4
kept = [r["message_id"] for r in uniq]
print("  оставлены message_id:", kept)
assert 1 in kept, "первое появление должно остаться"
assert 3 not in kept and 4 not in kept, "повторы должны уйти"
assert 5 in kept and 6 in kept, "короткие тексты должны остаться оба"

print("\n== оба чата вместе ==")
both = db.export_select([1, 2], unique_only=True)
ids = [(r["chat_id"], r["message_id"]) for r in both]
print("  ", ids)
assert (2, 1) in ids, "в другом чате тот же текст — это отдельный оффер, он остаётся"

print("\n== правка сообщения меняет его отпечаток ==")
before = db.query_one("SELECT text_hash FROM messages WHERE chat_id=1 AND message_id=3")["text_hash"]
put(1, 3, "Флаконы ПЭТ 500 мл с помпой в наличии, 12 000 шт на складе в Москве", day=8)
after = db.query_one("SELECT text_hash FROM messages WHERE chat_id=1 AND message_id=3")["text_hash"]
print(f"  {before} -> {after}")
assert before != after
assert len(db.export_select([1], unique_only=True)) == 5, "после правки запись перестала быть повтором"

print("\nТЕСТ ПРОЙДЕН: повторы определяются и фильтруются")

print("\n== миграция: отпечатки достраиваются для уже собранной базы ==")
import sqlite3
from chatgrab.db import schema
mig = "/tmp/cgdup_old.db"
if os.path.exists(mig): os.remove(mig)
conn = sqlite3.connect(mig)
conn.execute("""CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL, chat_title TEXT, date TEXT NOT NULL,
    edited_date TEXT, sender_id INTEGER, sender_username TEXT,
    sender_display_name TEXT, text TEXT NOT NULL DEFAULT '',
    reply_to_message_id INTEGER, forwarded_from TEXT, media_type TEXT,
    media_caption TEXT, photo_path TEXT, views INTEGER, link TEXT,
    is_hidden INTEGER NOT NULL DEFAULT 0, char_len INTEGER NOT NULL DEFAULT 0,
    is_reply INTEGER NOT NULL DEFAULT 0, is_forward INTEGER NOT NULL DEFAULT 0,
    UNIQUE(chat_id, message_id));""")
for i, t in enumerate([OFFER, OFFER, "коротко", "Ищем поставщика глицерина 99,5% пищевого, 2 тонны"], start=1):
    conn.execute("INSERT INTO messages(chat_id,message_id,date,text) VALUES (1,?,?,?)",
                 (i, f"2026-08-{i:02d}T10:00:00", t))
conn.commit()
before_rows = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
schema.migrate(conn)
after_rows = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
hashed = conn.execute("SELECT count(*) FROM messages WHERE text_hash IS NOT NULL").fetchone()[0]
print(f"  строк было {before_rows}, стало {after_rows}; с отпечатком: {hashed}")
assert before_rows == after_rows, "миграция потеряла строки"
assert hashed == 3, f"ожидали 3 отпечатка (короткий текст без), получили {hashed}"
dupes = conn.execute(
    "SELECT count(*) FROM (SELECT count(*) n FROM messages WHERE text_hash IS NOT NULL"
    " GROUP BY chat_id, text_hash HAVING n > 1)").fetchone()[0]
print("  групп повторов найдено:", dupes)
assert dupes == 1
schema.migrate(conn)   # повторный запуск безопасен
print("  повторная миграция ок")

print("\nТЕСТ ПРОЙДЕН: повторы определяются, фильтруются и достраиваются в старой базе")
