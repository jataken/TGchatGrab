"""Миграция 008 на настоящих старых заявках: ремап статуса, флаг
неоднозначности у бывших «closed», история не пустая после обновления.

test_migration.py уже проверяет, что новые колонки появляются — здесь
проверяется то, что реально может потерять данные пользователя: смысл
старого статуса при переносе на новую воронку.
"""
import os, sys
import sqlite3
import shutil
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatgrab.db import schema
from chatgrab.core import lead as lead_domain

db_path = os.path.join(tempfile.gettempdir(), "cgleadmig.db")
if os.path.exists(db_path):
    os.remove(db_path)
conn = sqlite3.connect(db_path)

# Старая модель bot_leads (contact_id, bot_id, status, manager, content) —
# без учёта миграций вовсе, как у пользователя, который не обновлялся с
# самого начала.
conn.execute("""CREATE TABLE bot_leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT, contact_id INTEGER NOT NULL, bot_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'new', manager TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, content TEXT NOT NULL DEFAULT '{}'
);""")
conn.execute("INSERT INTO bot_leads(contact_id,bot_id,status,manager,created_at,updated_at,content) "
             "VALUES (1,1,'new',NULL,'2026-08-01','2026-08-01','{\"company\":\"Аврора\"}')")
conn.execute("INSERT INTO bot_leads(contact_id,bot_id,status,manager,created_at,updated_at,content) "
             "VALUES (2,1,'in_progress','Ирина','2026-08-02','2026-08-02','{}')")
conn.execute("INSERT INTO bot_leads(contact_id,bot_id,status,manager,created_at,updated_at,content) "
             "VALUES (3,1,'closed','Ирина','2026-08-03','2026-08-03','{}')")
conn.commit()

schema.migrate(conn)

print("== статусы перенесены на воронку ==")
rows = {r[0]: r[1] for r in conn.execute("SELECT id, status FROM bot_leads ORDER BY id")}
print(" ", rows)
assert rows == {1: lead_domain.NEW, 2: lead_domain.NEGOTIATION, 3: lead_domain.WON}, rows

print("\n== ничего не потеряно из старых полей ==")
row = conn.execute("SELECT manager, content FROM bot_leads WHERE id = 1").fetchone()
assert row[1] == '{"company":"Аврора"}', row
row2 = conn.execute("SELECT manager FROM bot_leads WHERE id = 2").fetchone()
assert row2[0] == "Ирина"

print("\n== новые колонки — с безопасными значениями по умолчанию ==")
row = conn.execute(
    "SELECT source_type, owner, attachments, direction_id, reject_reason FROM bot_leads WHERE id = 1"
).fetchone()
print(" ", row)
assert row[0] == "bot"
assert row[1] == lead_domain.DEFAULT_OWNER
assert row[2] == "[]"
assert row[3] is None
assert row[4] is None

print("\n== у каждой старой заявки есть хотя бы одна запись истории ==")
for lead_id in (1, 2, 3):
    events = conn.execute(
        "SELECT kind, source FROM lead_events WHERE lead_id = ? ORDER BY id", (lead_id,)
    ).fetchall()
    print(f"  лид {lead_id}:", events)
    assert events, f"у лида {lead_id} нет ни одной записи истории после миграции"
    assert events[0] == ("created", "migration")

print("\n== бывший «closed» получает флаг неоднозначности, остальные — нет ==")
events3 = conn.execute(
    "SELECT kind, from_status, to_status, source FROM lead_events WHERE lead_id = 3 ORDER BY id"
).fetchall()
print("  лид 3 (был closed):", events3)
assert len(events3) == 2, events3
assert events3[1][:3] == ("status", "closed", "won")
assert events3[1][3] == "migration"

events1 = conn.execute("SELECT kind FROM lead_events WHERE lead_id = 1").fetchall()
print("  лид 1 (был new, отображение не изменилось):", events1)
assert len(events1) == 1, "статус не менялся — лишней записи о переходе быть не должно"

events2 = conn.execute("SELECT kind FROM lead_events WHERE lead_id = 2").fetchall()
print("  лид 2 (был in_progress -> переговоры, однозначно):", events2)
assert len(events2) == 1, "in_progress -> negotiation однозначен, флага неоднозначности не нужно"

print("\n== повторный migrate() не дублирует историю ==")
schema.migrate(conn)
recount = conn.execute("SELECT count(*) FROM lead_events").fetchone()[0]
print("  записей истории:", recount)
assert recount == 4, "повторный запуск не должен добавлять историю заново"

conn.close()
print("\nТЕСТ ПРОЙДЕН: старые заявки переносятся на воронку без потерь")
