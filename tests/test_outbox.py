"""С4: outbox — обойти лимиты нельзя ни одним путём отправки, dry-run не
пропускает ни одного сообщения, а холодное первое сообщение без явного
разрешения уходит в черновики и не отправляется без клика.
"""
import asyncio
import datetime as dt
import os
import shutil
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatgrab.paths import Paths
from chatgrab.db.database import Database
from chatgrab.bots import settings as bot_settings
from chatgrab.bots.outbox import Outbox

base = os.path.join(tempfile.gettempdir(), "cgoutbox")
shutil.rmtree(base, ignore_errors=True)
paths = Paths(Path(base))
paths.ensure()
db = Database(paths.db_path)
outbox = Outbox(db)


def make_bot(name: str, **limits) -> int:
    # Quiet hours default to 09:00-21:00 + no weekends — real enough to be
    # the shipped default, but it means "now" at whatever moment this test
    # happens to run could fall outside it and block sends that have
    # nothing to do with quiet hours. Every bot here disables the window
    # unless a test is specifically about it (that one overrides back).
    values = {"quiet_start": "00:00", "quiet_end": "23:59", "quiet_weekends": False, **limits}
    bot_id = db.add_bot(name, "userbot", None, "custom", None)
    db.set_bot_field(bot_id, settings=bot_settings.dumps(values))
    return bot_id


def sender():
    calls = []

    async def raw_send(target, text):
        calls.append((target, text))
    return raw_send, calls


print("== настройки: новые ключи по умолчанию и клампинг ==")
s = bot_settings.load(None)
assert s["max_per_hour"] == 20 and s["max_per_day"] == 100
assert s["max_first_messages_per_day"] == 10 and s["contact_cooldown_days"] == 3
assert s["quiet_start"] == "09:00" and s["quiet_end"] == "21:00"
assert s["quiet_weekends"] is True and s["dry_run"] is False and s["auto_send_cold"] is False
s = bot_settings.normalize({"max_per_hour": -5, "quiet_weekends": 0, "dry_run": "yes", "quiet_start": "bad"})
assert s["max_per_hour"] == 1, "нижняя граница не сработала"
assert s["quiet_weekends"] is False and s["dry_run"] is True, "булевы значения не приводятся"
assert s["quiet_start"] == "09:00", "мусорная строка должна остаться значением по умолчанию"
print("  ok")

print("\n== dry-run не пропускает ни одного сообщения ==")
bot_id = make_bot("Драй-ран", dry_run=True, auto_send_cold=True)
raw_send, calls = sender()
send = outbox.wrap(bot_id, raw_send, reactive=True)
asyncio.run(send(1, "первое сообщение"))
asyncio.run(send(1, "второе сообщение"))
assert calls == [], "в режиме dry-run raw_send не должен вызываться вообще"
rows = db.query("SELECT status FROM outbox_sends WHERE bot_id = ?", (bot_id,))
assert [r["status"] for r in rows] == ["dry_run", "dry_run"]
print("  ok — ни одно сообщение не ушло")

print("\n== реактивная отправка — первое сообщение уходит сразу, не в черновик ==")
bot_id = make_bot("Реактивный")
raw_send, calls = sender()
send = outbox.wrap(bot_id, raw_send, reactive=True)
asyncio.run(send(100, "ответ на входящее"))
assert calls == [(100, "ответ на входящее")]
assert db.list_drafts(bot_id) == [], "реактивная отправка не должна создавать черновик"
print("  ok")

print("\n== проактивная первая отправка становится черновиком, raw_send не вызывается ==")
bot_id = make_bot("Проактивный")  # auto_send_cold=False по умолчанию
raw_send, calls = sender()
send = outbox.wrap(bot_id, raw_send, reactive=False)
asyncio.run(send(200, "холодное предложение"))
assert calls == [], "холодное первое сообщение не должно уходить само"
drafts = db.list_drafts(bot_id)
assert len(drafts) == 1 and drafts[0]["target"] == "200"
print("  ok — черновик создан:", drafts[0]["reason"])

print("\n== черновик уходит только по явному клику (повторная отправка через reactive) ==")
draft = drafts[0]
send_draft = outbox.wrap(bot_id, raw_send, reactive=True)  # тот же путь, что и ручной клик
asyncio.run(send_draft(draft["target"], draft["text"]))
db.mark_draft_sent(draft["id"])
assert calls == [("200", "холодное предложение")]
d = db.get_draft(draft["id"])
assert d["sent_at"] is not None
print("  ok — ушло только после «клика»")

print("\n== auto_send_cold отменяет черновик — первое сообщение уходит сразу ==")
bot_id = make_bot("Разрешённая рассылка", auto_send_cold=True)
raw_send, calls = sender()
send = outbox.wrap(bot_id, raw_send, reactive=False)
asyncio.run(send(300, "холодное, но разрешённое"))
assert calls == [(300, "холодное, но разрешённое")]
assert db.list_drafts(bot_id) == []
print("  ok")

print("\n== чёрный список блокирует любой путь отправки ==")
bot_id = make_bot("Чёрный список", auto_send_cold=True)
db.add_to_blacklist(bot_id, "400", "жаловался на спам")
for reactive in (True, False):
    raw_send, calls = sender()
    send = outbox.wrap(bot_id, raw_send, reactive=reactive)
    asyncio.run(send(400, "не должно уйти"))
    assert calls == [], f"чёрный список не сработал при reactive={reactive}"
print("  ok — заблокировано и реактивно, и проактивно")

print("\n== лимит в час обходит нельзя ни одним путём ==")
bot_id = make_bot("Часовой лимит", max_per_hour=2, auto_send_cold=True)
raw_send, calls = sender()
send = outbox.wrap(bot_id, raw_send, reactive=True)
for i in range(4):
    asyncio.run(send(500 + i, f"сообщение {i}"))
assert len(calls) == 2, f"лимит в час не соблюдён: {calls}"
blocked = db.query("SELECT count(*) AS c FROM outbox_sends WHERE bot_id = ? AND status = 'blocked'", (bot_id,))
assert blocked[0]["c"] == 2
print("  ok — прошло ровно 2 из 4")

print("\n== лимит первых сообщений в сутки — общий для reactive и proactive ==")
bot_id = make_bot("Лимит первых", max_first_messages_per_day=2, auto_send_cold=True, max_per_hour=1000)
raw_send, calls = sender()
send_reactive = outbox.wrap(bot_id, raw_send, reactive=True)
send_proactive = outbox.wrap(bot_id, raw_send, reactive=False)
asyncio.run(send_reactive(600, "первому"))
asyncio.run(send_proactive(601, "второму"))
asyncio.run(send_reactive(602, "третьему — должен быть отклонён"))
assert len(calls) == 2, f"лимит первых сообщений превышен: {calls}"
print("  ok — третий новый контакт не получил сообщения")

print("\n== тихие часы: проактивная отправка блокируется, реактивная — нет ==")
# Outbox.wrap reads dt.datetime.now() directly rather than taking a `now`
# parameter (unlike TriggerScheduler.tick, which does) — the predicate it
# calls is what's worth testing directly here, at fixed timestamps;
# blacklist/limits/dry-run above already proved the wrapper actually
# blocks a send end-to-end, so this doesn't need to re-prove that part.
from chatgrab.bots.outbox import _within_send_window
bot_id = make_bot("Тихие часы", quiet_start="09:00", quiet_end="21:00",
                   quiet_weekends=True, auto_send_cold=True)
limits = bot_settings.load(db.get_bot(bot_id))
assert _within_send_window(limits, dt.datetime(2026, 8, 17, 12, 0)) is True  # понедельник, день
assert _within_send_window(limits, dt.datetime(2026, 8, 17, 23, 0)) is False  # понедельник, ночь
assert _within_send_window(limits, dt.datetime(2026, 8, 15, 12, 0)) is False  # суббота — тихие выходные
limits_no_weekend = dict(limits, quiet_weekends=False)
assert _within_send_window(limits_no_weekend, dt.datetime(2026, 8, 15, 12, 0)) is True
print("  ok — предикат тихих часов и выходных верный")

print("\n== персистентный кулдаун на контакт переживает «перезапуск» ==")
bot_id = make_bot("Кулдаун", contact_cooldown_days=3, auto_send_cold=True, quiet_weekends=False)
raw_send, calls = sender()
send_proactive = outbox.wrap(bot_id, raw_send, reactive=False)
asyncio.run(send_proactive(700, "первое"))
assert calls == [(700, "первое")]
# «перезапуск» — новое соединение и новый Outbox, как после рестарта приложения
db2 = Database(paths.db_path)
outbox2 = Outbox(db2)
send_proactive2 = outbox2.wrap(bot_id, raw_send, reactive=False)
asyncio.run(send_proactive2(700, "второе — должно быть отклонено"))
assert calls == [(700, "первое")], "персистентный кулдаун должен блокировать после рестарта"
send_reactive2 = outbox2.wrap(bot_id, raw_send, reactive=True)
asyncio.run(send_reactive2(700, "ответ — не блокируется кулдауном"))
assert calls == [(700, "первое"), (700, "ответ — не блокируется кулдауном")]
db2.close()
print("  ok")

print("\n== BotManager.scheduler реально проходит через outbox, не в обход ==")
# Не изолированный Outbox.wrap(), а настоящий BotManager — доказывает, что
# _send_for_bot (единственный путь, которым TriggerScheduler отправляет
# reminder/schedule-триггеры) действительно обёрнут, а не просто мог бы
# быть обёрнут.
from chatgrab.security import SecurityService
from chatgrab.telegram.service import TelegramService
from chatgrab.config import AppConfig
from chatgrab.bots.manager import BotManager

config = AppConfig.load(paths)
mgr = BotManager(db, TelegramService(config), SecurityService(config, paths))
sched_bot_id = mgr.create_bot("Напоминалка", "userbot", None, "custom", None)
db.set_bot_field(sched_bot_id, status="running", settings=bot_settings.dumps(
    {"quiet_weekends": False, "quiet_start": "00:00", "quiet_end": "23:59"}))
tpl = db.add_template(sched_bot_id, "T", "Вы ещё здесь?", [])
trig = db.add_trigger(sched_bot_id, "inactivity", {"days": 7})
db.add_action(trig, "send_dm", {"template_id": tpl}, 0)

now = dt.datetime(2026, 8, 13, 12, 0)
c = db.upsert_contact(9001, "u1", "U1")
db.execute("UPDATE bot_contacts SET last_active = ? WHERE id = ?",
           ((now - dt.timedelta(days=30)).isoformat(), c))
db.log_activity(c, sched_bot_id, None, None, "dm", kind="message")
db.execute("UPDATE bot_activity_log SET timestamp = ? WHERE contact_id = ? AND kind='message'",
           ((now - dt.timedelta(days=30)).isoformat(), c))

n = asyncio.run(mgr.scheduler.tick(now=now))
assert n == 1, "правило должно сработать — обёртка решает судьбу самой отправки, не срабатывания триггера"
sched_drafts = db.list_drafts(sched_bot_id)
assert len(sched_drafts) == 1, "первое проактивное сообщение молчащему контакту должно уйти в черновик"
sent_rows = db.query("SELECT * FROM outbox_sends WHERE bot_id = ? AND status = 'sent'", (sched_bot_id,))
assert not sent_rows, "ничего не должно было реально уйти"
print("  ok — планировщик не может обойти outbox")

db.close()
print("\nТЕСТ ПРОЙДЕН: outbox — единственный путь наружу, обойти нельзя")
