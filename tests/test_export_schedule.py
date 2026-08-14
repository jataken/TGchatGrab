"""Плановая выгрузка запускается по времени, а не по расписанию cron,
и переживает выключенный компьютер."""
import os, sys, asyncio, json, datetime as dt
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from chatgrab.paths import Paths
from chatgrab.db.database import Database
from chatgrab.services.export_service import ExportService, ExportParams
from chatgrab.services.export_schedule_service import ExportScheduleService

base="/tmp/cgsched2"; os.system(f"rm -rf {base}")
paths=Paths(Path(base)); paths.ensure(); db=Database(paths.db_path)
db.add_chat(1, "Биржа", "b", "all", None)
for i in range(1, 21):
    db.upsert_message({"chat_id":1,"message_id":i,"chat_title":"Биржа",
        "date":f"2026-08-{(i%28)+1:02d}T10:00:00","edited_date":None,"sender_id":1,
        "sender_username":"u","sender_display_name":"U","text":f"сообщение номер {i} про глицерин",
        "reply_to_message_id":None,"forwarded_from":None,"media_type":None,"media_caption":None,
        "media_path":None,"views":None,"link":"","is_hidden":0,"char_len":0,"is_reply":0,"is_forward":0})

svc = ExportService(db, paths)
params = ExportParams(chat_ids=[1], format="jsonl", split_mode="none",
                       folder=str(paths.exports_dir))
db.save_preset("Еженедельная", {k: v for k, v in params.__dict__.items()})

logs=[]
sched = ExportScheduleService(db, svc, on_log=lambda t, tone="": logs.append((tone, t)))
sid = db.add_export_schedule("Еженедельная", every_hours=168, at_hour=9)

print("== до назначенного часа не запускается ==")
row = db.list_export_schedules()[0]
assert not sched.due(row, dt.datetime(2026, 8, 14, 8, 0))
assert sched.due(row, dt.datetime(2026, 8, 14, 9, 30))
print("  08:00 — нет, 09:30 — да")

print("\n== запуск создаёт файл и отмечается в расписании ==")
n = asyncio.run(sched.tick(now=dt.datetime(2026, 8, 14, 9, 30)))
row = db.list_export_schedules()[0]
files = list(paths.exports_dir.glob("*.jsonl"))
print(f"  запусков: {n}, файлов: {len(files)} -> {[f.name for f in files]}")
print("  результат:", row["last_result"])
assert n == 1 and files and "готово" in row["last_result"]
assert logs and logs[-1][0] == "ok"

print("\n== раньше срока повторно не запускается ==")
n = asyncio.run(sched.tick(now=dt.datetime(2026, 8, 16, 9, 30)))
print("  через 2 дня при периоде 168 ч:", n)
assert n == 0

print("\n== после периода — запускается снова ==")
n = asyncio.run(sched.tick(now=dt.datetime(2026, 8, 22, 9, 30)))
print("  через 8 дней:", n)
assert n == 1

print("\n== пропущенный момент не теряется (компьютер был выключен) ==")
# прошлый запуск был 22-го; включаем машину только 30-го в 23:50
n = asyncio.run(sched.tick(now=dt.datetime(2026, 8, 30, 23, 50)))
print("  запусков:", n, "— cron бы пропустил, здесь наверстали")
assert n == 1

print("\n== выключенное расписание молчит ==")
db.set_export_schedule(sid, enabled=0)
n = asyncio.run(sched.tick(now=dt.datetime(2026, 9, 30, 12, 0)))
assert n == 0
print("  ok")

print("\n== удалённый пресет не роняет тик, а пишет ошибку ==")
db.set_export_schedule(sid, enabled=1)
db.delete_preset("Еженедельная")
n = asyncio.run(sched.tick(now=dt.datetime(2026, 10, 30, 12, 0)))
row = db.list_export_schedules()[0]
print("  запусков:", n, "| результат:", row["last_result"])
assert n == 0 and "ошибка" in row["last_result"]
assert logs[-1][0] == "warn"

print("\nТЕСТ ПРОЙДЕН: плановая выгрузка работает")
