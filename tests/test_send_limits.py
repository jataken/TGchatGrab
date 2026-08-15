"""Исходящие сообщения не должны уходить пачкой — это главный способ
получить ограничение на обычный аккаунт Telegram."""
import os, sys, asyncio, time
import tempfile
import shutil
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from chatgrab.paths import Paths
from chatgrab.db.database import Database
from chatgrab.bots.rules_engine import RulesEngine
from chatgrab.bots.userbot_runner import UserbotRunner
from chatgrab.bots.outbox import Outbox
from chatgrab.bots import settings as bot_settings
import datetime as dt

base = os.path.join(tempfile.gettempdir(), "cglimits"); shutil.rmtree(base, ignore_errors=True)
paths = Paths(Path(base)); paths.ensure()
db = Database(paths.db_path)
bot_id = db.add_bot("Юзербот", "userbot", None, "custom", None)
db.set_bot_field(bot_id, status="running")

print("== настройки по умолчанию и клампинг ==")
s = bot_settings.load(db.get_bot(bot_id))
print("  defaults:", s)
assert s["send_gap_seconds"] == 3.0 and s["max_reminders_per_tick"] == 25
db.set_bot_field(bot_id, settings=bot_settings.dumps({"send_gap_seconds": 9999, "max_reminders_per_tick": 0}))
s = bot_settings.load(db.get_bot(bot_id))
print("  после запредельных значений:", s)
assert s["send_gap_seconds"] == 600.0, "верхняя граница не сработала"
assert s["max_reminders_per_tick"] == 1, "нижняя граница не сработала"

# --- burst test through the real send path, with a fake Telethon client ---
class FakeClient:
    def __init__(self): self.sent = []
    async def send_message(self, entity, text): self.sent.append((entity, time.monotonic()))
    async def get_entity(self, t): return t
class FakeTg:
    def __init__(self): self.client = FakeClient()

GAP = 0.15
db.set_bot_field(bot_id, settings=bot_settings.dumps(
    {"send_gap_seconds": GAP, "dm_cooldown_seconds": 0, "max_reminders_per_tick": 500}))

tg = FakeTg()
runner = UserbotRunner(tg, db, RulesEngine(db), lambda *a, **k: None, lambda *a, **k: None, Outbox(db))
send = runner.make_send(bot_id)

print(f"\n== 10 сообщений разным контактам, gap={GAP}s ==")
async def burst():
    # concurrently, as a reminder sweep + incoming messages would be
    await asyncio.gather(*(send(1000 + i, f"msg {i}") for i in range(10)))
t0 = time.monotonic()
asyncio.run(burst())
elapsed = time.monotonic() - t0
stamps = [t for _, t in tg.client.sent]
gaps = [round(b - a, 3) for a, b in zip(stamps, stamps[1:])]
print(f"  отправлено: {len(stamps)} за {elapsed:.2f}s")
print(f"  промежутки: {gaps}")
assert len(stamps) == 10, "часть сообщений потерялась"
assert all(g >= GAP * 0.85 for g in gaps), f"сообщения ушли пачкой: {gaps}"
assert elapsed >= GAP * 9 * 0.85, "суммарное время меньше ожидаемого"
print("  ok — каждое следующее ждало своей очереди")

print("\n== кулдаун по одному контакту ==")
db.set_bot_field(bot_id, settings=bot_settings.dumps({"send_gap_seconds": 0, "dm_cooldown_seconds": 60}))
tg.client.sent.clear()
asyncio.run(send(2001, "первое"))
asyncio.run(send(2001, "второе — должно быть пропущено"))
asyncio.run(send(2002, "другому контакту — должно пройти"))
print("  отправлено:", [e for e, _ in tg.client.sent])
assert [e for e, _ in tg.client.sent] == [2001, 2002]

print("\n== потолок напоминаний за один тик ==")
from chatgrab.bots.scheduler import TriggerScheduler
db.set_bot_field(bot_id, settings=bot_settings.dumps(
    {"max_reminders_per_tick": 3, "send_gap_seconds": 0, "dm_cooldown_seconds": 0}))
tpl = db.add_template(bot_id, "T", "Вы ещё здесь?", [])
trig = db.add_trigger(bot_id, "inactivity", {"days": 7})
db.add_action(trig, "send_dm", {"template_id": tpl}, 0)
now = dt.datetime(2026, 8, 13, 12, 0)
for i in range(10):
    c = db.upsert_contact(3000 + i, f"u{i}", f"U{i}")
    db.execute("UPDATE bot_contacts SET last_active = ? WHERE id = ?",
               ((now - dt.timedelta(days=30)).isoformat(), c))
    db.log_activity(c, bot_id, None, None, "dm", kind="message")
    db.execute("UPDATE bot_activity_log SET timestamp = ? WHERE contact_id = ? AND kind='message'",
               ((now - dt.timedelta(days=30)).isoformat(), c))
sent = []
async def cap_send(t, x): sent.append(t)
logs = []
sched = TriggerScheduler(db, RulesEngine(db), lambda b: cap_send, lambda b, t, tone="": logs.append(t))
n = asyncio.run(sched.tick(now=now))
print(f"  молчащих 10, потолок 3 -> отправлено {n}")
assert n == 3, f"потолок не соблюдён: {n}"
print("  журнал:", logs[-1])
assert "в следующий заход" in logs[-1]
n2 = asyncio.run(sched.tick(now=now + dt.timedelta(minutes=15)))
print(f"  следующий тик -> ещё {n2}")
assert n2 == 3, "остаток не разбирается следующими тиками"

print("\nТЕСТ ПРОЙДЕН: всплеск отправки исключён")
