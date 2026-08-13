import os, sys, asyncio
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from chatgrab.paths import Paths
from chatgrab.db.database import Database
from chatgrab.bots.scenario_engine import ScenarioEngine

base = "/tmp/cgrepeat"; os.system(f"rm -rf {base}")
paths = Paths(Path(base)); paths.ensure()
db = Database(paths.db_path)
eng = ScenarioEngine(db)

bot_id = db.add_bot("Тест", "userbot", None, "custom", None)
sc_id = db.add_scenario(bot_id, "Квалификация", [
    {"question": "Из какой компании?", "field": "company", "validation": "text"},
    {"question": "Телефон?", "field": "phone", "validation": "phone"},
])
CONTACT = 555

def run_once(company, phone, label):
    r = eng.start(bot_id, sc_id, CONTACT)
    assert r.question == "Из какой компании?", r
    r = eng.submit_answer(bot_id, CONTACT, company)
    assert r.question == "Телефон?", r
    r = eng.submit_answer(bot_id, CONTACT, phone)
    print(f"  {label}: done={r.done} answers={r.answers} error={r.error}")
    return r

print("Проход 1 (новый контакт):")
r1 = run_once("Аврора Косметик", "+7 916 442 18 03", "результат")
assert r1.done and r1.answers == {"company": "Аврора Косметик", "phone": "+7 916 442 18 03"}

print("Проход 2 (тот же контакт вернулся) — до фикса здесь терялась заявка:")
r2 = run_once("Аврора Косметик втор", "+7 916 000 00 00", "результат")
assert r2.done, "второй проход не завершился"
assert r2.answers == {"company": "Аврора Косметик втор", "phone": "+7 916 000 00 00"}, r2.answers

print("Проход 3 (для верности):")
r3 = run_once("Третий раз", "+7 916 111 11 11", "результат")
assert r3.done and r3.answers["company"] == "Третий раз"

rows = db.query("SELECT status, count(*) c FROM bot_scenario_sessions GROUP BY status")
print("\nсессии в базе:", {r["status"]: r["c"] for r in rows})

# abandoning mid-dialog then restarting must also work (old schema broke on the 2nd abandon)
eng.start(bot_id, sc_id, CONTACT)
eng.submit_answer(bot_id, CONTACT, "брошу на середине")
eng.start(bot_id, sc_id, CONTACT)   # abandons the previous active one
print("повторный обрыв диалога — ок")

rows = db.query("SELECT status, count(*) c FROM bot_scenario_sessions GROUP BY status")
print("сессии в базе:", {r["status"]: r["c"] for r in rows})
assert db.query_one("SELECT count(*) c FROM bot_scenario_sessions WHERE status='active'")["c"] == 1
print("\nТЕСТ ПРОЙДЕН: повторный контакт больше не теряет заявку")
