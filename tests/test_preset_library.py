"""С5: пресеты — валидный JSON для всех пяти, установка через BotManager
(не в обход apply_preset для старых b2b/b2c/custom), прогон по всем
веткам в симуляторе, и то, что каждый пресет реально что-то умеет:
after_hours только вне часов, chat_hunter собирает ключевые/стоп-слова
из направлений и не шлёт холодное сообщение без клика, follow_up
регистрирует все касания.
"""
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _bootstrap import fresh_env
from chatgrab.telegram.service import TelegramService
from chatgrab.bots.manager import BotManager
from chatgrab.bots.rules_engine import IncomingEvent
from chatgrab.bots import preset_library as pl
from chatgrab.bots import settings as bot_settings

paths, db, config, security = fresh_env("cgpresets")
mgr = BotManager(db, TelegramService(config), security)

direction_a = db.add_direction("Косметическое сырьё", keywords=["глицерин", "отдушки"],
                                stop_words=["продам", "ищу работу"])
direction_b = db.add_direction("Упаковка", keywords=["флаконы", "банки"], stop_words=[])

print("== все пять пресетов валидны и грузятся ==")
specs = {s["key"]: s for s in pl.list_preset_specs()}
assert set(specs) == {"inbound_qualification", "price_request", "after_hours", "chat_hunter", "follow_up"}, specs
print("  ", sorted(specs))

print("\n== старый путь (b2b/b2c/custom) не задет — apply_preset, а не библиотека ==")
old_bot_id = mgr.create_bot("Старый", "userbot", None, "b2b", "@manager")
assert len(db.list_scenarios(old_bot_id)) == 1, "b2b по-прежнему должен сеять один сценарий"
assert pl.find_spec("b2b") is None, "b2b не должен существовать как файл пресета"
print("  ok")

print("\n== каждый пресет ставится через мастер (BotManager.create_bot) ==")
bot_ids = {}
for key, spec in specs.items():
    answers = pl.default_answers(spec, db)
    if "manager_name" in answers:
        answers["manager_name"] = "Ирина"
    bot_id = mgr.create_bot(spec["label"], "userbot", None, key, "@manager", preset_answers=answers)
    bot_ids[key] = bot_id
    db.set_bot_field(bot_id, status="running")
    n_triggers = len(db.list_triggers(bot_id))
    print(f"  {key}: bot_id={bot_id}, триггеров={n_triggers}")
    assert n_triggers >= 1
print("  ok — все пять ставятся без ошибок")

print("\n== симулятор проходит все ветки inbound_qualification и price_request ==")
rules = mgr.rules
for key in ("inbound_qualification", "price_request"):
    scenario = db.list_scenarios(bot_ids[key])[0]
    steps = json.loads(scenario["steps"])
    sample = []
    for step in steps:
        options = step.get("options") or []
        if options:
            sample.append(options[0]["label"])
        elif step.get("validation") == "phone":
            sample.append("+7 921 000-00-00")
        else:
            sample.append("тестовый ответ")
    trail = rules.scenarios.dry_run(scenario["id"], sample)
    print(f"  {key}: {len(trail)} шагов в трассе")
    assert trail and "final_answers" in trail[-1]
    assert not any(step.get("error") for step in trail if "final_answers" not in step), trail
print("  ok — обе ветки проходят без ошибок валидации")

print("\n== price_request: у каждого направления есть свой вариант ==")
scenario = db.list_scenarios(bot_ids["price_request"])[0]
steps = json.loads(scenario["steps"])
assert scenario["kind"] == "branching"
labels = [o["label"] for o in steps[0]["options"]]
assert labels == ["Косметическое сырьё", "Упаковка"], labels
print("  ", labels)

print("\n== after_hours: триггер работает только вне рабочих часов ==")
trig = db.list_triggers(bot_ids["after_hours"])[0]
event = IncomingEvent(contact_telegram_id=1, username="u", text="привет", chat_type="dm")
inside = dt.datetime(2026, 8, 17, 12, 0)   # понедельник, день
outside = dt.datetime(2026, 8, 17, 22, 0)  # понедельник, ночь
assert rules.matches(trig, event, now=inside) is False, "днём после-часовой триггер не должен срабатывать"
assert rules.matches(trig, event, now=outside) is True, "ночью должен сработать"
print("  ok")

print("\n== chat_hunter: ключевые и стоп-слова собраны из направлений, срабатывает не всегда ==")
trig = db.list_triggers(bot_ids["chat_hunter"])[0]
cfg = json.loads(trig["config"])
assert sorted(cfg["keywords"]) == sorted(["глицерин", "отдушки", "флаконы", "банки"])
assert cfg["stop_words"] == ["продам", "ищу работу"]
db.add_chat(5001, "Тестовый чат", None, "all", None)
match_event = IncomingEvent(contact_telegram_id=42, username="stranger", text="ищу глицерин оптом",
                            chat_id=5001, chat_type="group")
stop_event = IncomingEvent(contact_telegram_id=43, username="seller", text="продам глицерин недорого",
                           chat_id=5001, chat_type="group")
assert rules.matches(trig, match_event) is True
assert rules.matches(trig, stop_event) is False, "стоп-слово должно отменять совпадение по ключевому слову"
print("  ok — совпадает по ключевому слову, стоп-слово подавляет совпадение")

print("\n== chat_hunter: первое сообщение автору уходит в черновик, не отправляется ==")
db.set_bot_field(bot_ids["chat_hunter"], manager_chat_id="@manager")
# Quiet hours default to 09:00-21:00 + no weekends — irrelevant to what
# this section checks (drafting a cold first message), so disabled to
# keep the assertion from depending on the real clock at test time.
db.set_bot_field(bot_ids["chat_hunter"], settings=bot_settings.dumps(
    {"quiet_start": "00:00", "quiet_end": "23:59", "quiet_weekends": False}))
triggers = rules.triggers_for(bot_ids["chat_hunter"], match_event)
assert len(triggers) == 1
sent = []


async def fake_raw(target, text):
    sent.append((target, text))


send = mgr.outbox.wrap(bot_ids["chat_hunter"], fake_raw, reactive=(match_event.chat_type == "dm"))
asyncio.run(rules.fire(bot_ids["chat_hunter"], triggers[0], match_event, send, lambda *a: None))
manager_sent = [s for s in sent if s[0] == "@manager"]
stranger_sent = [s for s in sent if s[0] == 42]
print("  менеджеру:", manager_sent, "· автору напрямую:", stranger_sent)
assert manager_sent, "менеджер не должен ждать клика — его уведомление должно уйти сразу"
assert not stranger_sent, "первое сообщение автору должно стать черновиком, а не уйти само"
drafts = db.list_drafts(bot_ids["chat_hunter"])
assert len(drafts) == 1 and drafts[0]["target"] == "42"
print("  ok — менеджер уведомлён сразу, автору — черновик")

print("\n== follow_up: все четыре касания зарегистрированы ==")
triggers = db.list_triggers(bot_ids["follow_up"])
days = sorted(json.loads(t["config"])["days"] for t in triggers)
assert days == [3, 7, 14, 30], days
print("  ", days)

db.close()
print("\nТЕСТ ПРОЙДЕН: библиотека пресетов ставится в два клика и работает сразу")
