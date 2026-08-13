"""Bot API-бот, добавленный в группу, не должен считать сообщения группы
личной перепиской."""
import os, sys, asyncio, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from chatgrab.paths import Paths
from chatgrab.db.database import Database
from chatgrab.bots.rules_engine import RulesEngine
from chatgrab.bots.bot_api_runner import BotApiRunner

base = "/tmp/cgchattype"; os.system(f"rm -rf {base}")
paths = Paths(Path(base)); paths.ensure()
db = Database(paths.db_path)
bot_id = db.add_bot("Ассистент", "bot_api", None, "custom", None)
db.set_bot_field(bot_id, status="running")

class Chat:
    def __init__(self, t, i): self.type, self.id = t, i
class User:
    def __init__(self, i, u): self.id, self.username = i, u
class Msg:
    def __init__(self, text, chat_type, chat_id=None, uid=777):
        self.text, self.caption = text, None
        self.chat = Chat(chat_type, chat_id or 0)
        self.from_user = User(uid, "petya")

rules = RulesEngine(db)
row = db.get_bot(bot_id)
sent, logs = [], []
runner = BotApiRunner(db, None, rules, row, lambda b, t, tone="": logs.append(t), lambda *a: None)
async def fake_send(target, text): sent.append((target, text))
runner.send_dm = fake_send

print("== классификация ==")
for raw, expect in [("private", "dm"), ("group", "group"), ("supergroup", "group"), ("channel", "channel")]:
    got = BotApiRunner._classify(Msg("x", raw, 123))[0]
    print(f"  {raw:11s} -> {got}")
    assert got == expect, f"{raw} определён как {got}"

# правило «написали в личку» -> отправить ответ
tpl = db.add_template(bot_id, "Ответ", "Здравствуйте!", [])
t_dm = db.add_trigger(bot_id, "incoming_dm", {})
db.add_action(t_dm, "send_dm", {"template_id": tpl}, 0)

print("\n== правило «написали в личку» ==")
sent.clear()
asyncio.run(runner._handle_message(Msg("привет", "private")))
print("  личка   -> отправлено:", sent)
assert len(sent) == 1, "в личке правило должно срабатывать"

sent.clear()
asyncio.run(runner._handle_message(Msg("привет всем", "supergroup", -100500)))
print("  группа  -> отправлено:", sent)
assert not sent, "в группе правило «написали в личку» срабатывать не должно"

print("\n== сценарий не продолжается в группе ==")
sc = db.add_scenario(bot_id, "S", [
    {"question": "Из какой компании?", "field": "company", "validation": "text"},
    {"question": "Телефон?", "field": "phone", "validation": "text"}])
rules.scenarios.start(bot_id, sc, 777)
sent.clear()
asyncio.run(runner._handle_message(Msg("случайное сообщение в группе", "supergroup", -100500)))
print("  группа  -> отправлено:", sent)
assert not sent, "сценарий продолжился в группе"
sess = db.get_active_scenario_session(bot_id, 777)
assert json.loads(sess["answers"]) == {}, "ответ из группы попал в заявку: " + sess["answers"]
print("  ответы сценария не тронуты:", sess["answers"])

sent.clear()
asyncio.run(runner._handle_message(Msg("Аврора Косметик", "private")))
print("  личка   -> отправлено:", sent)
sess = db.get_active_scenario_session(bot_id, 777)
print("  ответы сценария:", sess["answers"])
assert json.loads(sess["answers"]) == {"company": "Аврора Косметик"}

print("\n== триггер по чату работает для Bot API в группе ==")
t_chat = db.add_trigger(bot_id, "chat_message", {"chat_id": -100500, "keywords": ["куплю"]})
db.add_action(t_chat, "save_lead", {}, 0)
before = len(db.list_leads(bot_id=bot_id))
asyncio.run(runner._handle_message(Msg("куплю глицерин", "supergroup", -100500)))
after = len(db.list_leads(bot_id=bot_id))
print(f"  заявок было {before}, стало {after}")
assert after == before + 1, "триггер по чату не сработал"

print("\n== служебное сообщение без автора не роняет обработчик ==")
m = Msg("x", "supergroup", -100500); m.from_user = None
asyncio.run(runner._handle_message(m))
print("  ok")

print("\nТЕСТ ПРОЙДЕН: тип чата учитывается")
