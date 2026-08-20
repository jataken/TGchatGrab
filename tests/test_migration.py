import sqlite3, sys, os
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from chatgrab.db import schema

db_path = os.path.join(tempfile.gettempdir(), "mig_test.db")
if os.path.exists(db_path): os.remove(db_path)
conn = sqlite3.connect(db_path)

# --- build a schema-v2 database by hand, exactly as it shipped ---
conn.execute("CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT);")
conn.execute("""
CREATE TABLE bot_scenario_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL, scenario_id INTEGER NOT NULL,
    contact_telegram_id INTEGER NOT NULL, step_index INTEGER NOT NULL DEFAULT 0,
    answers TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'active',
    started_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(bot_id, contact_telegram_id, status)
);""")
conn.execute("INSERT INTO app_meta VALUES ('schema_version','2')")
# pre-existing rows: one finished run and one abandoned run for the same contact
conn.execute("INSERT INTO bot_scenario_sessions(bot_id,scenario_id,contact_telegram_id,step_index,answers,status,started_at,updated_at)"
             " VALUES (1,1,555,4,'{\"company\":\"Acme\"}','done','2026-08-01','2026-08-01')")
conn.execute("INSERT INTO bot_scenario_sessions(bot_id,scenario_id,contact_telegram_id,step_index,answers,status,started_at,updated_at)"
             " VALUES (1,1,555,2,'{}','abandoned','2026-08-02','2026-08-02')")
conn.commit()

# prove the OLD schema has the bug
try:
    conn.execute("INSERT INTO bot_scenario_sessions(bot_id,scenario_id,contact_telegram_id,step_index,answers,status,started_at,updated_at)"
                 " VALUES (1,1,555,4,'{}','done','2026-08-03','2026-08-03')")
    print("OLD: second 'done' accepted  <-- unexpected")
except sqlite3.IntegrityError as e:
    print("OLD: second 'done' REJECTED  ->", e)
conn.rollback()

before = conn.execute("SELECT count(*) FROM bot_scenario_sessions").fetchone()[0]
schema.migrate(conn)
after = conn.execute("SELECT count(*) FROM bot_scenario_sessions").fetchone()[0]
print(f"rows before={before} after={after}")
assert before == after, "MIGRATION LOST ROWS"

ver = conn.execute("SELECT value FROM app_meta WHERE key='schema_version'").fetchone()[0]
print("schema_version =", ver)

# the repeat-customer case that used to lose a lead
conn.execute("INSERT INTO bot_scenario_sessions(bot_id,scenario_id,contact_telegram_id,step_index,answers,status,started_at,updated_at)"
             " VALUES (1,1,555,4,'{\"company\":\"Acme second time\"}','done','2026-08-03','2026-08-03')")
conn.commit()
print("NEW: second 'done' accepted  ->", conn.execute(
    "SELECT count(*) FROM bot_scenario_sessions WHERE contact_telegram_id=555 AND status='done'").fetchone()[0], "done rows")

# but two ACTIVE dialogs for one contact must still be impossible
conn.execute("INSERT INTO bot_scenario_sessions(bot_id,scenario_id,contact_telegram_id,step_index,answers,status,started_at,updated_at)"
             " VALUES (1,1,555,0,'{}','active','2026-08-04','2026-08-04')")
conn.commit()
try:
    conn.execute("INSERT INTO bot_scenario_sessions(bot_id,scenario_id,contact_telegram_id,step_index,answers,status,started_at,updated_at)"
                 " VALUES (1,1,555,0,'{}','active','2026-08-05','2026-08-05')")
    conn.commit()
    print("NEW: second ACTIVE accepted  <-- BUG, invariant lost")
    sys.exit(1)
except sqlite3.IntegrityError as e:
    print("NEW: second ACTIVE REJECTED  ->", e)

# колонки, добавленные после v2, должны появиться на старой базе
def columns(table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}

added = {
    "chats": {"account_id"},
    "bots": {"account_id", "settings"},
    "messages": {"media_path", "text_hash"},
    "bot_scenarios": {"kind", "done_template_id"},
    "bot_scenario_sessions": {"step_id"},
    "bot_leads": {"direction_id", "owner", "reject_reason", "attachments",
                  "source_type", "tg_user_id", "next_action_at", "next_action_text"},
}
for table, wanted in added.items():
    missing = wanted - columns(table)
    print(f"{table}: {sorted(wanted)} -> {'ok' if not missing else 'НЕТ ' + str(missing)}")
    assert not missing, (table, missing)

# уже настроенный сценарий обязан остаться пошаговым: ветвление добавлено
# рядом, а не вместо
conn.execute("INSERT INTO bot_scenarios(bot_id,name,steps,created_at) VALUES (1,'Старый','[]','2026-08-01')")
conn.commit()
kind = conn.execute("SELECT kind FROM bot_scenarios ORDER BY id DESC LIMIT 1").fetchone()[0]
print("вид сценария по умолчанию:", kind)
assert kind == "linear", kind

# migrate() must be safe to run again
schema.migrate(conn)
print("re-migrate OK, rows =", conn.execute("SELECT count(*) FROM bot_scenario_sessions").fetchone()[0])

# новый справочник направлений появляется на старой базе, и учёт миграций
# отражает применённые шаги ровно по одному разу
applied = {row[0] for row in conn.execute("SELECT id FROM schema_migrations")}
print("применённые миграции:", sorted(applied))
assert applied == {"006", "007", "008", "009", "010", "011", "012", "014", "015", "016"}, applied
tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
assert "direction" in tables, "таблица direction не создалась"
assert "lead_events" in tables, "таблица lead_events не создалась"
assert "outbox_sends" in tables, "таблица outbox_sends не создалась"
assert "outbox_drafts" in tables, "таблица outbox_drafts не создалась"
assert "outbox_blacklist" in tables, "таблица outbox_blacklist не создалась"
assert "crm_queue" in tables, "таблица crm_queue не создалась"
direction_cols = {r[1] for r in conn.execute("PRAGMA table_info(direction)")}
assert "crm_source_id" in direction_cols, "direction.crm_source_id не добавилась"
assert {"crm_id", "crm_synced_at"} <= columns("bot_leads"), "поля Bitrix не добавились в bot_leads"

# 009: contact_id/bot_id больше не NOT NULL — лид может существовать без
# бота и без контакта (ручное создание, лид из найденного сообщения)
notnull = {r[1]: r[3] for r in conn.execute("PRAGMA table_info(bot_leads)")}
print("bot_leads.contact_id notnull:", notnull["contact_id"], "· bot_id notnull:", notnull["bot_id"])
assert notnull["contact_id"] == 0 and notnull["bot_id"] == 0
conn.execute(
    "INSERT INTO bot_leads(contact_id, bot_id, status, created_at, updated_at) "
    "VALUES (NULL, NULL, 'new', '2026-08-14', '2026-08-14')"
)
conn.commit()
print("лид без contact_id/bot_id принят")

before_count = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
schema.migrate(conn)
after_count = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
print(f"записей об применённых миграциях: было {before_count}, стало {after_count}")
assert before_count == after_count, "повторный migrate() не должен дублировать записи учёта"

print("\nMIGRATION TEST PASSED")
