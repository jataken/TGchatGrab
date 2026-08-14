"""С3: лид заводится тремя способами (вручную, из найденного сообщения,
из сценария — с сопоставлением полей), фильтры/воронка по новой
вокабуляре, и напоминание срабатывает один раз, не повторяясь после
«перезапуска» (новое соединение с базой, новый экземпляр сервиса).
"""
import asyncio
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatgrab.paths import Paths
from chatgrab.db.database import Database
from chatgrab.core import lead as lead_domain
from chatgrab.bots.rules_engine import IncomingEvent, RulesEngine
from chatgrab.services.lead_reminder_service import LeadReminderService

base = os.path.join(tempfile.gettempdir(), "cgleadlife")
shutil.rmtree(base, ignore_errors=True)
paths = Paths(Path(base))
paths.ensure()
db = Database(paths.db_path)

direction_a = db.add_direction("Косметическое сырьё", keywords=["глицерин"])
db.add_direction("Упаковка", keywords=["флакон"])
db.add_chat(1001, "Биржа", "cosmo", "all", None)

print("== источник 1: вручную — без contact_id и без bot_id ==")
manual_id = db.add_lead(
    None, None, {}, status=lead_domain.NEW,
    display_name="Игорь", phone="+7 900 000-00-00",
    source_type=lead_domain.SOURCE_TYPE_MANUAL, direction_id=direction_a,
    event_source=lead_domain.EVENT_SOURCE_MANUAL,
)
lead = db.get_lead(manual_id)
assert lead["contact_id"] is None and lead["bot_id"] is None
assert lead["source_type"] == lead_domain.SOURCE_TYPE_MANUAL
print("  ok, лид создан без бота и без контакта")

print("\n== источник 2: из найденного сообщения — тоже без contact_id/bot_id ==")
db.upsert_message({
    "chat_id": 1001, "message_id": 1, "chat_title": "Биржа", "date": "2026-08-10T10:00:00",
    "edited_date": None, "sender_id": 777, "sender_username": "supplier",
    "sender_display_name": "Пётр", "text": "куплю глицерин", "reply_to_message_id": None,
    "forwarded_from": None, "media_type": None, "media_caption": None, "media_path": None,
    "views": None, "link": "", "is_hidden": 0, "char_len": 0, "is_reply": 0, "is_forward": 0,
})
message_lead_id = db.add_lead(
    None, None, {"text": "куплю глицерин"}, status=lead_domain.NEW,
    tg_user_id=777, username="supplier", display_name="Пётр",
    source_chat_id=1001, source_type=lead_domain.SOURCE_TYPE_CHAT,
    event_source=lead_domain.EVENT_SOURCE_MANUAL,
)
lead = db.get_lead(message_lead_id)
assert lead["contact_id"] is None
assert lead["tg_user_id"] == 777
print("  ok")

print("\n== источник 3: из сценария — сопоставленные поля идут в реальные колонки ==")
bot_id = db.add_bot("Приёмная", "userbot", None, "custom", "@manager")
scenario_id = db.add_scenario(bot_id, "Заявка", [
    {"question": "Что нужно?", "field": "что", "lead_field": "product"},
    {"question": "Какой город?", "field": "город", "lead_field": "city"},
    {"question": "Комментарий?", "field": "мусор"},  # намеренно не сопоставлено
])
trig = db.add_trigger(bot_id, "incoming_dm", {})
db.add_action(trig, "run_scenario", {"scenario_id": scenario_id}, 0)

rules = RulesEngine(db)
loop = asyncio.new_event_loop()
CONTACT = 999
sent: list[tuple] = []


async def send_dm(target, text):
    sent.append((target, text))


def event(text):
    return IncomingEvent(contact_telegram_id=CONTACT, username="new_client", text=text,
                         chat_id=None, chat_type="dm")


triggers = rules.triggers_for(bot_id, event("Здравствуйте"))
assert len(triggers) == 1
loop.run_until_complete(rules.fire(bot_id, triggers[0], event("Здравствуйте"), send_dm, lambda *a: None))
for answer in ["глицерин", "Москва", "неважно"]:
    loop.run_until_complete(rules.continue_scenario(bot_id, event(answer), send_dm, lambda *a: None))

leads = db.list_leads(bot_id)
assert len(leads) == 1, leads
scenario_lead = leads[0]
print("  product =", scenario_lead["product"], "· city =", scenario_lead["city"])
assert scenario_lead["product"] == "глицерин"
assert scenario_lead["city"] == "Москва"
content = json.loads(scenario_lead["content"])
assert content == {"что": "глицерин", "город": "Москва", "мусор": "неважно"}, \
    "несопоставленное поле должно остаться в content, а не пропасть"
print("  ok — сопоставленные поля в колонках, остальное сохранено в content")

print("\n== фильтры: направление / источник / период ==")
by_direction = [l["id"] for l in db.list_leads(direction_id=direction_a)]
assert by_direction == [manual_id], by_direction

by_source = [l["id"] for l in db.list_leads(source_type=lead_domain.SOURCE_TYPE_MANUAL)]
assert by_source == [manual_id], by_source

far_future = (dt.datetime.now() + dt.timedelta(days=1)).astimezone().isoformat(timespec="seconds")
assert db.list_leads(since=far_future) == [], "ничего не должно быть создано в будущем"
print("  ok")

print("\n== воронка по статусам сходится со списком ==")
counts = db.leads_status_counts()
print("  ", counts)
assert counts[lead_domain.NEW] == 3, counts
assert sum(counts.values()) == len(db.list_leads())
print("  ok")

print("\n== напоминание срабатывает один раз ==")
past = (dt.datetime.now() - dt.timedelta(minutes=5)).astimezone().isoformat(timespec="seconds")
db.set_lead_field(manual_id, next_action_at=past, next_action_text="перезвонить")
now_iso_str = dt.datetime.now().astimezone().isoformat(timespec="seconds")
assert [r["id"] for r in db.due_lead_reminders(now_iso_str)] == [manual_id]

fired: list[int] = []
service = LeadReminderService(db, on_fire=lambda lead: fired.append(lead["id"]))
n = service.tick()
assert n == 1 and fired == [manual_id], (n, fired)
lead = db.get_lead(manual_id)
assert lead["next_action_at"] is None and lead["next_action_text"] is None, \
    "поле должно очищаться сразу после срабатывания"
events = db.list_lead_events(manual_id)
assert events[-1]["kind"] == lead_domain.EVENT_KIND_REMINDER
assert events[-1]["text"] == "перезвонить"
print("  сработало, поле очищено, запись в истории есть")

print("\n== после «перезапуска» повторно не срабатывает ==")
db2 = Database(paths.db_path)  # новое соединение — как после рестарта приложения
service2 = LeadReminderService(db2, on_fire=lambda lead: fired.append(lead["id"]))
n2 = service2.tick()
assert n2 == 0, "уже сработавшее напоминание не должно сработать снова"
assert fired == [manual_id], "новых срабатываний быть не должно"
db2.close()
print("  ok")

db.close()
print("\nТЕСТ ПРОЙДЕН: лид заводится тремя способами, напоминание срабатывает ровно один раз")
