import os, sys, asyncio, datetime as dt
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from chatgrab.paths import Paths
from chatgrab.db.database import Database
from chatgrab.bots.rules_engine import RulesEngine
from chatgrab.bots.scheduler import TriggerScheduler, REMINDER_KIND

base = "/tmp/cgsched"; os.system(f"rm -rf {base}")
paths = Paths(Path(base)); paths.ensure()
db = Database(paths.db_path)

bot_id = db.add_bot("Дожимщик", "userbot", None, "custom", "@manager")
db.set_bot_field(bot_id, status="running")

tpl = db.add_template(bot_id, "Напоминание", "{name}, вы интересовались — всё ещё актуально?", ["name"])
trig = db.add_trigger(bot_id, "inactivity", {"days": 7})
db.add_action(trig, "send_dm", {"template_id": tpl}, order_index=0)

now = dt.datetime(2026, 8, 13, 12, 0)
# a contact who went quiet 10 days ago, and one who spoke yesterday
old = db.upsert_contact(111, "molchun", "Молчун")
fresh = db.upsert_contact(222, "aktivnyi", "Активный")
db.execute("UPDATE bot_contacts SET last_active = ? WHERE id = ?",
           ((now - dt.timedelta(days=10)).isoformat(), old))
db.execute("UPDATE bot_contacts SET last_active = ? WHERE id = ?",
           ((now - dt.timedelta(days=1)).isoformat(), fresh))
# both must have talked to this bot before to be in its audience
for cid in (old, fresh):
    db.log_activity(cid, bot_id, None, None, "dm", kind="message")
db.execute("UPDATE bot_activity_log SET timestamp = ? WHERE contact_id = ?",
           ((now - dt.timedelta(days=10)).isoformat(), old))

sent = []
async def send(target, text): sent.append((target, text))
logs = []
sched = TriggerScheduler(db, RulesEngine(db), lambda bid: send,
                         lambda bid, t, tone="": logs.append(t))

print("== тик 1 ==")
n = asyncio.run(sched.tick(now=now))
print("  сработало:", n, "| отправлено:", sent)
assert n == 1, f"ожидали 1 напоминание, получили {n}"
assert sent[0][0] == 111, "напоминание ушло не тому контакту"
assert "molchun" in sent[0][1], "переменная не подставилась: " + sent[0][1]
assert not any(t[0] == 222 for t in sent), "активному контакту слать не должны"

print("== тик 2 (сразу же) — повтора быть не должно ==")
sent.clear()
n = asyncio.run(sched.tick(now=now + dt.timedelta(minutes=15)))
print("  сработало:", n, "| отправлено:", sent)
assert n == 0 and not sent, "напоминание задублировалось"

print("== контакт ответил, снова замолчал -> напоминание опять уместно ==")
later = now + dt.timedelta(days=20)
# он ответил (last_active обновился), потом снова замолчал;
# активного держим "свежим", чтобы проверять именно повторное напоминание
db.execute("UPDATE bot_contacts SET last_active = ? WHERE id = ?",
           ((now + dt.timedelta(days=1)).isoformat(), old))
db.execute("UPDATE bot_contacts SET last_active = ? WHERE id = ?",
           (later.isoformat(), fresh))
n = asyncio.run(sched.tick(now=later))
print("  сработало:", n, "| отправлено:", sent)
assert n == 1 and sent[0][0] == 111, "после нового молчания напоминание должно уйти снова"

print("\n== расписание ==")
sent.clear()
strig = db.add_trigger(bot_id, "schedule", {"at": "10:00", "days": [0,1,2,3,4]})
db.add_action(strig, "notify", {"text": "Ежедневная сводка"}, order_index=0)
thu = dt.datetime(2026, 8, 13, 11, 0)   # четверг, после 10:00
n = asyncio.run(sched.tick(now=thu))
print("  четверг 11:00 -> сработало:", n, "| отправлено:", sent)
assert any("сводка" in t for _, t in sent), "расписание не сработало"
sent.clear()
n = asyncio.run(sched.tick(now=thu + dt.timedelta(hours=2)))
print("  тот же день 13:00 -> сработало:", n, "(должно быть 0)")
assert n == 0, "расписание сработало дважды за день"
sat = dt.datetime(2026, 8, 15, 11, 0)   # суббота — не входит в days
sent.clear()
n = asyncio.run(sched.tick(now=sat))
print("  суббота -> сработало:", n, "(должно быть 0)")
assert n == 0, "расписание сработало в выходной"

print("\n== выключённый бот не шлёт ничего ==")
db.set_bot_field(bot_id, status="stopped")
sent.clear()
n = asyncio.run(sched.tick(now=now + dt.timedelta(days=60)))
assert n == 0 and not sent
print("  ok")

print("\nТЕСТ ПРОЙДЕН: фоновые триггеры работают")
