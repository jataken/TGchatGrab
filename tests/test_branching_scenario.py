"""Ветвление в сценарии — и то, что линейный вариант от этого не изменился.

Оба вида нужны одновременно: под разные задачи планируются разные боты.
Поэтому здесь проверяются рядом и ветвящийся сценарий, и линейный на той
же базе, тем же движком.
"""
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _bootstrap import fresh_env
from chatgrab.db.database import Database
from chatgrab.bots.scenario_engine import BRANCHING, END, LINEAR, ScenarioEngine

paths, db, config, security = fresh_env("cgbranch")

from chatgrab.bots.manager import BotManager
from chatgrab.telegram.service import TelegramService

mgr = BotManager(db, TelegramService(config), security)
bot_id = mgr.create_bot("Приёмная", "userbot", None, "custom", "@manager")

# Развилка: сырьё спрашивают об одном, упаковку — о другом, а «просто
# смотрю» заканчивается сразу, не отнимая у человека три вопроса.
branching_id = db.add_scenario(bot_id, "Что нужно", [
    {"id": "start", "question": "Что вас интересует?", "field": "направление",
     "options": [
         {"label": "Сырьё", "next": "syrye"},
         {"label": "Упаковка", "next": "upak"},
         {"label": "Просто смотрю", "next": END},
     ]},
    {"id": "syrye", "question": "Какое сырьё и сколько в месяц?", "field": "сырьё",
     "next": "phone"},
    {"id": "upak", "question": "Какая упаковка и тираж?", "field": "упаковка",
     "next": "phone"},
    {"id": "phone", "question": "Телефон для связи?", "field": "телефон",
     "validation": "phone"},
])
db.update_scenario(branching_id, kind=BRANCHING)

linear_id = db.add_scenario(bot_id, "Простая анкета", [
    {"question": "Что нужно?", "field": "что"},
    {"question": "Телефон?", "field": "телефон", "validation": "phone"},
])

engine = ScenarioEngine(db)
CONTACT = 777


def run(answers, scenario_id, contact=CONTACT):
    trail = [engine.start(bot_id, scenario_id, contact)]
    for answer in answers:
        trail.append(engine.submit_answer(bot_id, contact, answer))
    return trail


print("== вопрос показывает варианты, пронумерованными ==")
first = engine.start(bot_id, branching_id, CONTACT)
print(first.question.replace("\n", " | "))
assert "1. Сырьё" in first.question and "3. Просто смотрю" in first.question

print("\n== ответ номером ведёт в свою ветку ==")
r = engine.submit_answer(bot_id, CONTACT, "1")
print("  ->", r.question)
assert r.question == "Какое сырьё и сколько в месяц?", r
r = engine.submit_answer(bot_id, CONTACT, "глицерин, 2 тонны")
assert r.question == "Телефон для связи?", r
r = engine.submit_answer(bot_id, CONTACT, "+7 921 000-00-00")
print("  ответы:", r.answers)
assert r.done and r.answers == {
    "направление": "Сырьё", "сырьё": "глицерин, 2 тонны", "телефон": "+7 921 000-00-00"}
assert "упаковка" not in r.answers, "чужая ветка не должна спрашиваться"

print("\n== ответ словами ведёт в другую ветку ==")
trail = run(["упаковка", "флаконы 50 мл, 10 000 шт", "+7 921 111-11-11"], branching_id, contact=778)
print("  ответы:", trail[-1].answers)
assert trail[1].question == "Какая упаковка и тираж?", trail[1]
assert trail[-1].done and trail[-1].answers["упаковка"] == "флаконы 50 мл, 10 000 шт"
assert "сырьё" not in trail[-1].answers

print("\n== ветка может закончить разговор сразу ==")
engine.start(bot_id, branching_id, 779)
r = engine.submit_answer(bot_id, branching_id and 779, "просто смотрю")
print("  ", r.done, r.answers)
assert r.done and r.answers == {"направление": "Просто смотрю"}
assert not db.get_active_scenario_session(bot_id, 779), "сессия должна закрыться"

print("\n== непонятный ответ переспрашивается, а не уводит наугад ==")
engine.start(bot_id, branching_id, 780)
r = engine.submit_answer(bot_id, 780, "ну не знаю")
print("  ", r.error)
assert r.error and not r.done
assert "1. Сырьё" in r.question, "вопрос показывается заново вместе с вариантами"
r = engine.submit_answer(bot_id, 780, "2")
assert r.question == "Какая упаковка и тираж?", r

print("\n== ответ, содержащий подпись варианта, засчитывается ==")
engine.start(bot_id, branching_id, 781)
r = engine.submit_answer(bot_id, 781, "мне нужна упаковка, флаконы")
assert r.question == "Какая упаковка и тираж?", r
print("  ok")

print("\n== разговор переживает перезапуск на середине ветки ==")
engine.start(bot_id, branching_id, 782)
engine.submit_answer(bot_id, 782, "сырьё")
fresh = ScenarioEngine(Database(paths.db_path))
assert fresh.resume_question(bot_id, 782) == "Какое сырьё и сколько в месяц?", \
    "после перезапуска должен продолжиться тот же шаг ветки"
r = fresh.submit_answer(bot_id, 782, "отдушки, 50 кг")
assert r.question == "Телефон для связи?", r
print("  ok")

print("\n== линейный сценарий работает как работал ==")
trail = run(["глицерин", "+7 921 222-22-22"], linear_id, contact=790)
print("  ", [t.question for t in trail[:-1]], "->", trail[-1].answers)
assert trail[0].question == "Что нужно?"
assert trail[1].question == "Телефон?"
assert trail[-1].done and trail[-1].answers == {"что": "глицерин", "телефон": "+7 921 222-22-22"}
assert ScenarioEngine._kind(db.get_scenario(linear_id)) == LINEAR

print("\n== проверка «вживую» идёт по тем же правилам ==")
trail = engine.dry_run(branching_id, ["Упаковка", "банки", "+7 921 333-33-33"])
for row in trail:
    print("  ", row)
assert trail[1]["question"] == "Какая упаковка и тираж?"
assert trail[-1]["final_answers"]["упаковка"] == "банки"

print("\n== переход в никуда завершает разговор, а не уводит не туда ==")
broken_id = db.add_scenario(bot_id, "Сломанная", [
    {"id": "a", "question": "Первый?", "field": "a", "next": "удалённый-шаг"},
])
db.update_scenario(broken_id, kind=BRANCHING)
engine.start(bot_id, broken_id, 800)
r = engine.submit_answer(bot_id, 800, "ответ")
print("  ", r.done, r.answers)
assert r.done and r.answers == {"a": "ответ"}

print("\n== зацикленная ветка не вешает проверку ==")
loop_id = db.add_scenario(bot_id, "Кольцо", [
    {"id": "a", "question": "По кругу?", "field": "a", "next": "a"},
])
db.update_scenario(loop_id, kind=BRANCHING)
trail = engine.dry_run(loop_id, ["раз", "два", "три"])
print("   шагов в проверке:", len(trail))
assert len(trail) <= 4

print("\nТЕСТ ПРОЙДЕН: ветвление работает, линейный сценарий не тронут")
