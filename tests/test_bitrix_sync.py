"""С6: очередь Bitrix24 — дубли не плодятся, повторная отправка идёт
update'ом, а не add'ом, отключённая сеть не теряет лид (растущий backoff,
запись остаётся в очереди) и разбирается сама после восстановления.
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
from chatgrab.config import AppConfig
from chatgrab.db.database import Database
from chatgrab.security import SecurityService
from chatgrab.core import lead as lead_domain
from chatgrab.integrations import bitrix
from chatgrab.integrations.bitrix import BitrixError
from chatgrab.services.bitrix_sync_service import BitrixSyncService, _backoff

base = os.path.join(tempfile.gettempdir(), "cgbitrix")
shutil.rmtree(base, ignore_errors=True)
paths = Paths(Path(base))
paths.ensure()
db = Database(paths.db_path)
config = AppConfig.load(paths)
security = SecurityService(config, paths)


class FakeClient:
    """Stands in for BitrixClient — programmable per test, records calls."""

    def __init__(self, webhook_url):
        self.webhook_url = webhook_url
        self.calls = []
        self.duplicate_id = None
        self.fail_add = False
        self.next_id = 100

    async def find_duplicate_lead(self, phone, email):
        self.calls.append(("find_duplicate", phone, email))
        return self.duplicate_id

    async def add_lead(self, fields):
        self.calls.append(("add", fields))
        if self.fail_add:
            raise BitrixError("портал недоступен")
        self.next_id += 1
        return self.next_id

    async def update_lead(self, crm_id, fields):
        self.calls.append(("update", crm_id, fields))


fake = FakeClient("https://x")
service = BitrixSyncService(db, security, client_factory=lambda url: fake)

print("== без сохранённого webhook — тик ничего не делает ==")
n = asyncio.run(service.tick())
assert n == 0
print("  ok")

bitrix.set_webhook_url(db, security, "https://portal.bitrix24.ru/rest/1/faketoken/")
print("\n== webhook сохранён и читается обратно (через шифрование при включённом мастер-пароле — тут выключен) ==")
assert bitrix.get_webhook_url(db, security) == "https://portal.bitrix24.ru/rest/1/faketoken/"
print("  ok")

direction_id = db.add_direction("Косметическое сырьё")
lead_id = db.add_lead(
    None, None, {}, status=lead_domain.NEW,
    display_name="Ирина", phone="+79210000000", direction_id=direction_id,
    product="глицерин", source_type=lead_domain.SOURCE_TYPE_MANUAL,
    event_source=lead_domain.EVENT_SOURCE_MANUAL,
)

print("\n== постановка в очередь и первая отправка — crm.lead.add, дублей не найдено ==")
service.enqueue(lead_id)
assert db.get_crm_queue_entry(lead_id) is not None
n = asyncio.run(service.tick())
assert n == 1
lead = db.get_lead(lead_id)
print("  crm_id:", lead["crm_id"], "· crm_synced_at:", lead["crm_synced_at"])
assert lead["crm_id"] == "101"
assert lead["crm_synced_at"]
assert db.get_crm_queue_entry(lead_id) is None, "очередь должна опустеть после успешной отправки"
assert fake.calls[-1][0] == "add"
events = db.list_lead_events(lead_id)
assert events[-1]["kind"] == lead_domain.EVENT_KIND_SYNC
print("  ok — заявка получила crm_id, запись в истории есть")

print("\n== повторная отправка идёт update'ом, а не add'ом (без нового id) ==")
db.set_lead_field(lead_id, product="глицерин 99,5%")
service.enqueue(lead_id)
fake.calls.clear()
n = asyncio.run(service.tick())
assert n == 1
assert fake.calls == [("update", "101", fake.calls[0][2])], fake.calls
lead = db.get_lead(lead_id)
assert lead["crm_id"] == "101", "id не должен меняться при повторной отправке"
print("  ok — update, id не изменился")

print("\n== дубль по телефону — присваивается найденный id, add не вызывается ==")
lead_id_2 = db.add_lead(
    None, None, {}, status=lead_domain.NEW,
    display_name="Пётр", phone="+79210000001",
    source_type=lead_domain.SOURCE_TYPE_MANUAL, event_source=lead_domain.EVENT_SOURCE_MANUAL,
)
fake.duplicate_id = 555
fake.calls.clear()
service.enqueue(lead_id_2)
asyncio.run(service.tick())
lead2 = db.get_lead(lead_id_2)
print("  crm_id:", lead2["crm_id"], "· вызовы:", [c[0] for c in fake.calls])
assert lead2["crm_id"] == "555"
assert "add" not in [c[0] for c in fake.calls], "не должен создавать новый лид, раз дубль найден"
fake.duplicate_id = None
print("  ok")

print("\n== сеть недоступна: лид остаётся в очереди, попытки растут, backoff увеличивается ==")
lead_id_3 = db.add_lead(
    None, None, {}, status=lead_domain.NEW, display_name="Игорь", phone="+79210000002",
    source_type=lead_domain.SOURCE_TYPE_MANUAL, event_source=lead_domain.EVENT_SOURCE_MANUAL,
)
fake.fail_add = True
service.enqueue(lead_id_3)
n = asyncio.run(service.tick())
assert n == 1, "тик должен был попытаться отправить, пусть и неудачно"
entry = db.get_crm_queue_entry(lead_id_3)
print("  осталась в очереди:", entry is not None, "· попыток:", entry["attempts"], "· ошибка:", entry["last_error"])
assert entry is not None, "неудачная отправка не должна убирать лид из очереди"
assert entry["attempts"] == 1
assert "портал недоступен" in entry["last_error"]
assert db.get_lead(lead_id_3)["crm_id"] is None
print("  ok — лид не потерян")

print("\n== повторный тик сразу же не трогает её — ещё не подошло время ==")
n = asyncio.run(service.tick())
assert n == 0, "next_attempt_at ещё не наступил, тик не должен был её взять"
print("  ok")

print("\n== после «восстановления сети» (время backoff прошло) — уходит сама ==")
fake.fail_add = False
future = dt.datetime.now().astimezone() + dt.timedelta(seconds=_backoff(1) + 5)
n = asyncio.run(service.tick(now=future))
assert n == 1
entry = db.get_crm_queue_entry(lead_id_3)
lead3 = db.get_lead(lead_id_3)
print("  осталась в очереди:", entry is not None, "· crm_id:", lead3["crm_id"])
assert entry is None
assert lead3["crm_id"] is not None
print("  ok — очередь разобралась сама")

print("\n== экспоненциальный backoff растёт и упирается в потолок ==")
vals = [_backoff(n) for n in range(6)]
print("  ", vals)
assert vals == sorted(vals), "backoff должен только расти"
assert vals[-1] <= 30 * 60, "потолок backoff не должен превышаться"
print("  ok")

db.close()
print("\nТЕСТ ПРОЙДЕН: очередь Bitrix24 не теряет лиды и не плодит дубли")
