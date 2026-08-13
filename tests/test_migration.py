import sqlite3, sys, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from chatgrab.db import schema

db_path = "/tmp/mig_test.db"
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

# migrate() must be safe to run again
schema.migrate(conn)
print("re-migrate OK, rows =", conn.execute("SELECT count(*) FROM bot_scenario_sessions").fetchone()[0])
print("\nMIGRATION TEST PASSED")
