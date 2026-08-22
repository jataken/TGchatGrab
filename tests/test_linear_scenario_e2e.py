"""Пошаговый чат-бот целиком: от сообщения в чате до заявки у менеджера.

Это проверка того, что линейный сценарий действительно работает — та
самая, на которую опирается решение добавлять ветвление. Проходится весь
путь, а не отдельные куски: правило срабатывает в групповом чате, бот
пишет в личку, контакт отвечает шаг за шагом, невалидный ответ
переспрашивается, ответы складываются в заявку, контакту уходит
подтверждение по шаблону, менеджеру — сводка. Отдельно проверяется, что
диалог переживает перезапуск и что вернувшийся контакт может пройти
сценарий второй раз.

Сеть не нужна: отправка подменена списком, всё остальное — настоящее.
"""
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _bootstrap import fresh_env
from chatgrab.db.database import Database
from chatgrab.bots.rules_engine import IncomingEvent, RulesEngine

paths, db, config, security = fresh_env("cglinear")
loop = asyncio.new_event_loop()

sent: list[tuple] = []


async def send_dm(target, text):
    sent.append((target, text))


logs: list[tuple] = []
def log(text, tone=""): logs.append((text, tone))

from chatgrab.bots.manager import BotManager
from chatgrab.telegram.service import TelegramService

MANAGER = "@manager"
mgr = BotManager(db, TelegramService(config), security)
bot_id = mgr.create_bot("Заявки", "userbot", None, "custom", MANAGER)

db.add_chat(1001, "Косметическое сырьё · Биржа", "cosmo", "all", None)

# сценарий из трёх шагов + шаблон подтверждения
scenario_id = db.add_scenario(bot_id, "Заявка на сырьё", [
    {"question": "Что именно нужно?", "field": "что", "validation": "text"},
    {"question": "Какой объём в месяц?", "field": "объём", "validation": "text"},
    {"question": "Телефон для связи?", "field": "телефон", "validation": "phone"},
])
tpl_id = db.add_template(
    bot_id, "Подтверждение",
    "Спасибо, {имя}! Записал: {что}, {объём}. Менеджер свяжется по {телефон}.",
    ["имя", "что", "объём", "телефон"])
db.update_scenario(scenario_id, done_template_id=tpl_id)

trig = db.add_trigger(bot_id, "chat_message", {"chat_id": 1001, "keywords": ["куплю", "ищу поставщика"]})
db.add_action(trig, "run_scenario", {"scenario_id": scenario_id}, 0)

rules = RulesEngine(db)
CONTACT = 555


def event(text, chat_id=None, chat_type="dm"):
    return IncomingEvent(contact_telegram_id=CONTACT, username="irina", text=text,
                         chat_id=chat_id, chat_type=chat_type)


print("== правило не срабатывает на постороннее сообщение ==")
assert rules.triggers_for(bot_id, event("добрый день всем", 1001, "group")) == []
print("  ok")

print("\n== правило срабатывает по ключевому слову в нужном чате ==")
triggers = rules.triggers_for(bot_id, event("Куплю глицерин 99,5%", 1001, "group"))
assert len(triggers) == 1, triggers
loop.run_until_complete(
    rules.fire(bot_id, triggers[0], event("Куплю глицерин 99,5%", 1001, "group"), send_dm, log))
print("  бот написал:", sent[-1])
assert sent[-1] == (CONTACT, "Что именно нужно?"), sent

print("\n== в чужом чате то же слово не срабатывает ==")
assert rules.triggers_for(bot_id, event("куплю глицерин", 2002, "group")) == []
print("  ok")

print("\n== контакт отвечает шаг за шагом ==")
assert rules.has_active_scenario(bot_id, CONTACT)
loop.run_until_complete(rules.continue_scenario(bot_id, event("глицерин 99,5%"), send_dm, log))
print("  ->", sent[-1][1])
assert sent[-1][1] == "Какой объём в месяц?"

print("\n== диалог переживает перезапуск приложения ==")
rules_after_restart = RulesEngine(Database(paths.db_path))
assert rules_after_restart.has_active_scenario(bot_id, CONTACT), \
    "состояние должно лежать в базе, а не в памяти"
rules = rules_after_restart
loop.run_until_complete(rules.continue_scenario(bot_id, event("2 тонны"), send_dm, log))
print("  ->", sent[-1][1])
assert sent[-1][1] == "Телефон для связи?"

print("\n== негодный ответ переспрашивается, а не проглатывается ==")
before = len(db.list_leads(bot_id))
loop.run_until_complete(rules.continue_scenario(bot_id, event("позвоните в телеграм"), send_dm, log))
print("  ->", sent[-1][1])
assert "номер телефона" in sent[-1][1].lower(), sent[-1]
assert len(db.list_leads(bot_id)) == before, "заявка не должна создаваться раньше времени"
assert rules.has_active_scenario(bot_id, CONTACT), "сценарий должен остаться на том же шаге"

print("\n== последний шаг: заявка, подтверждение контакту, сводка менеджеру ==")
sent.clear()
loop.run_until_complete(rules.continue_scenario(bot_id, event("+7 921 000-00-00"), send_dm, log))
for target, text in sent:
    print(f"  -> {target}: {text}")

leads = db.list_leads(bot_id)
assert len(leads) == 1, leads
answers = json.loads(leads[0]["content"])
print("  заявка:", answers)
assert answers == {"что": "глицерин 99,5%", "объём": "2 тонны", "телефон": "+7 921 000-00-00"}

to_contact = [t for tgt, t in sent if tgt == CONTACT]
assert len(to_contact) == 1, to_contact
assert "глицерин 99,5%" in to_contact[0] and "+7 921 000-00-00" in to_contact[0], to_contact[0]
assert "{" not in to_contact[0], f"подстановка не сработала: {to_contact[0]}"

to_manager = [t for tgt, t in sent if tgt == MANAGER]
assert len(to_manager) == 1, to_manager
assert "@irina" in to_manager[0] and "2 тонны" in to_manager[0], to_manager[0]

assert not rules.has_active_scenario(bot_id, CONTACT), "после завершения сессия должна закрыться"

print("\n== вернувшийся контакт проходит сценарий второй раз ==")
sent.clear()
triggers = rules.triggers_for(bot_id, event("Ищу поставщика отдушек", 1001, "group"))
loop.run_until_complete(
    rules.fire(bot_id, triggers[0], event("Ищу поставщика отдушек", 1001, "group"), send_dm, log))
for answer in ["отдушки", "50 кг", "+7 921 111-11-11"]:
    loop.run_until_complete(rules.continue_scenario(bot_id, event(answer), send_dm, log))
leads = db.list_leads(bot_id)
print("  заявок всего:", len(leads))
assert len(leads) == 2, "вторая заявка того же контакта не должна теряться"
# Свежая — первой: список заявок читают сверху вниз.
assert json.loads(leads[0]["content"])["что"] == "отдушки", [dict(l) for l in leads]

print("\n== ошибки в журнале нет ==")
warns = [t for t, tone in logs if tone == "warn"]
print("  предупреждений:", warns or "нет")
assert not warns, warns

print("\nТЕСТ ПРОЙДЕН: пошаговый чат-бот работает от слова в чате до заявки")
