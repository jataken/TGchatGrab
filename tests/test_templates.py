import os, sys, asyncio
import tempfile
import shutil
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from chatgrab.paths import Paths
from chatgrab.db.database import Database
from chatgrab.bots.rules_engine import IncomingEvent, RulesEngine
from chatgrab.bots import presets
from chatgrab.bots.templating import render, variables_in

# --- pure templating ---
print("== подстановка ==")
assert render("Привет, {name}!", {"name": "Ирина"}) == "Привет, Ирина!"
print("  ok:", render("Привет, {name}!", {"name": "Ирина"}))
# unknown var stays literal instead of raising or blanking
assert render("Привет, {name} из {company}!", {"name": "Ирина"}) == "Привет, Ирина из {company}!"
print("  неизвестная переменная остаётся как есть:", render("{a} и {b}", {"a": "X"}))
# the cases that would break str.format
for tricky in ["Цена {price} руб — скидка 20%", 'JSON: {"k": 1}', "Формула a{b}c{", "100% предоплата"]:
    out = render(tricky, {"price": "250"})
    print(f"  без падения: {tricky!r} -> {out!r}")
assert variables_in("{a} {b} {a}") == ["a", "b"]

# --- engine: send_dm via template ---
print("\n== движок ==")
base = os.path.join(tempfile.gettempdir(), "cgtpl"); shutil.rmtree(base, ignore_errors=True)
paths = Paths(Path(base)); paths.ensure()
db = Database(paths.db_path)
bot_id = db.add_bot("Бот", "userbot", None, "custom", "@manager")
presets.apply_preset(db, bot_id, "b2b")

sent = []
async def send_dm(target, text): sent.append((target, text))

rules = RulesEngine(db)
contact_id = db.upsert_contact(555, "irina_supply", "Ирина")

tpl_id = db.add_template(bot_id, "Приветствие", "Здравствуйте, {name}! Вы писали: «{text}»", ["name","text"])
trig_id = db.add_trigger(bot_id, "incoming_dm", {})
db.add_action(trig_id, "send_dm", {"template_id": tpl_id}, order_index=0)

ev = IncomingEvent(contact_telegram_id=555, username="irina_supply", text="нужен глицерин", chat_type="dm")
trig = db.get_trigger(trig_id)
asyncio.run(rules.fire(bot_id, trig, ev, send_dm, log=lambda t, tone="": print("   log:", t)))
print("  отправлено:", sent[-1])
assert "Здравствуйте, irina_supply!" in sent[-1][1] and "нужен глицерин" in sent[-1][1]

# legacy action with raw text keeps working
sent.clear()
trig2 = db.add_trigger(bot_id, "incoming_dm", {})
db.add_action(trig2, "send_dm", {"text": "Старое действие без шаблона, {name}"}, order_index=0)
asyncio.run(rules.fire(bot_id, db.get_trigger(trig2), ev, send_dm, log=lambda t, tone="": None))
print("  legacy text:", sent[-1][1])
assert sent[-1][1] == "Старое действие без шаблона, irina_supply"

# --- scenario completion confirmation (the preset template that was dead) ---
print("\n== подтверждение по завершении сценария ==")
sent.clear()
sc = db.list_scenarios(bot_id)[0]
print("  сценарий:", sc["name"], "| done_template_id =", sc["done_template_id"])
assert sc["done_template_id"] is not None, "пресет не привязал шаблон подтверждения"

rules.scenarios.start(bot_id, sc["id"], 555)
import json
steps = json.loads(sc["steps"])
for i, step in enumerate(steps):
    ev2 = IncomingEvent(contact_telegram_id=555, username="irina_supply", text=f"ответ {i}", chat_type="dm")
    asyncio.run(rules.continue_scenario(bot_id, ev2, send_dm, log=lambda t, tone="": None))
for tgt, txt in sent:
    print(f"  -> {tgt}: {txt}")
confirm = [t for tgt, t in sent if tgt == 555 and "Спасибо" in t]
assert confirm, "подтверждение контакту не отправлено"
assert "{company}" not in confirm[0], "переменная не подставилась: " + confirm[0]
print("  подтверждение с подставленной переменной:", confirm[0])

leads = db.list_leads(bot_id=bot_id)
print("  заявок создано:", len(leads))
assert len(leads) == 1
print("\nТЕСТ ПРОЙДЕН: шаблоны работают")
