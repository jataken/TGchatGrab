"""Ретеншн: старое сначала уходит в архив, только потом удаляется."""
import sys, json, datetime as dt
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _bootstrap import fresh_db
from chatgrab.services.retention_service import RetentionService, cutoff_for

paths, db = fresh_db("cgret")
db.add_chat(1, "Биржа", "b", "all", None)

now = dt.datetime(2026, 8, 14, 12, 0)
def put(mid, when, media=None):
    db.upsert_message({"chat_id":1,"message_id":mid,"chat_title":"Биржа","date":when.isoformat(),
        "edited_date":None,"sender_id":1,"sender_username":"u","sender_display_name":"U",
        "text":f"сообщение {mid}, достаточно длинное чтобы попасть в отпечаток текста",
        "reply_to_message_id":None,"forwarded_from":None,"media_type":"photo" if media else None,
        "media_caption":None,"media_path":media,"views":None,"link":"","is_hidden":0,
        "char_len":0,"is_reply":0,"is_forward":0})

for i in range(1, 13):     # по одному в месяц за прошедший год
    put(i, now - dt.timedelta(days=30*i), media=f"photos/1/{i}.jpg" if i > 9 else None)
print("всего сообщений:", db.message_count())

svc = RetentionService(db, paths)
print("\n== граница считается календарными месяцами ==")
c = cutoff_for(6, now)
print("  6 месяцев назад ->", c[:10])
assert c.startswith("2026-02")

print("\n== предпросмотр не удаляет ==")
svc.set_months(6)
p = svc.preview()
print("  под удаление:", p["messages"])
assert p["messages"] > 0
assert db.message_count() == 12, "предпросмотр тронул базу"

print("\n== хранить всё (0) — ничего не считается ==")
assert svc.preview(0)["messages"] == 0

print("\n== архив пишется до удаления ==")
before = db.message_count()
res = svc.archive_and_prune()
print(f"  архивировано {res['archived']}, удалено {res['deleted']}")
print("  файл:", res["path"].name if res["path"] else None)
assert res["path"] and res["path"].exists(), "архив не создан"
lines = res["path"].read_text(encoding="utf-8").strip().splitlines()
assert len(lines) == res["deleted"], "в архиве не столько же записей, сколько удалено"
first = json.loads(lines[0])
assert "text" in first and "date" in first, "архив не содержит содержимого"
print(f"  в архиве {len(lines)} строк, поля на месте")
assert db.message_count() == before - res["deleted"]
print(f"  в базе осталось {db.message_count()}")

print("\n== свежие сообщения не тронуты ==")
rows = db.query("SELECT date FROM messages ORDER BY date")
assert all(r["date"] >= cutoff_for(6, now)[:4] for r in rows)
print("  самое старое:", rows[0]["date"][:10])

print("\n== повторный прогон уже нечего удалять ==")
res2 = svc.archive_and_prune()
print("  удалено:", res2["deleted"])
assert res2["deleted"] == 0 and res2["path"] is None

print("\n== осиротевшие медиафайлы находятся, но не удаляются ==")
(paths.photos_dir / "1").mkdir(parents=True, exist_ok=True)
for name in ["10.jpg", "11.jpg", "99.jpg"]:
    (paths.photos_dir / "1" / name).write_bytes(b"x")
orphans = svc.orphaned_media()
print("  найдено осиротевших:", len(orphans), [o.name for o in orphans])
assert any(o.name == "99.jpg" for o in orphans), "не найден файл без сообщения"
assert all(o.exists() for o in orphans), "файлы не должны удаляться сами"

print("\nТЕСТ ПРОЙДЕН: ретеншн архивирует перед удалением")
