"""С7: статус/источник маппинг сохраняется и применяется при отправке в
Bitrix24; политика автопостановки в очередь фильтрует по стадии лида, а
немаппленное или отсутствующее направление не роняет отправку.
"""
import asyncio
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
from chatgrab.services.bitrix_sync_service import BitrixSyncService

base = os.path.join(tempfile.gettempdir(), "cgbitrixmap")
shutil.rmtree(base, ignore_errors=True)
paths = Paths(Path(base))
paths.ensure()
db = Database(paths.db_path)
config = AppConfig.load(paths)
security = SecurityService(config, paths)


class FakeClient:
    def __init__(self, webhook_url):
        self.webhook_url = webhook_url
        self.calls = []
        self.next_id = 200

    async def find_duplicate_lead(self, phone, email):
        return None

    async def add_lead(self, fields):
        self.calls.append(("add", fields))
        self.next_id += 1
        return self.next_id

    async def update_lead(self, crm_id, fields):
        self.calls.append(("update", crm_id, fields))


print("== маппинг статусов: сохраняется и читается обратно, лишние значения не хранятся ==")
bitrix.set_status_map(db, {
    lead_domain.NEW: "NEW",
    lead_domain.QUALIFIED: "",  # пустое значение — не должно сохраниться
    lead_domain.WON: "CONVERTED",
})
saved = bitrix.get_status_map(db)
assert saved == {lead_domain.NEW: "NEW", lead_domain.WON: "CONVERTED"}, saved
print("  ok")

print("\n== lead_fields(): STATUS_ID подставляется из маппинга, если статус в нём есть ==")
direction_id = db.add_direction("Косметическое сырьё")
lead_id = db.add_lead(
    None, None, {}, status=lead_domain.WON,
    display_name="Ирина", direction_id=direction_id,
    source_type=lead_domain.SOURCE_TYPE_MANUAL, event_source=lead_domain.EVENT_SOURCE_MANUAL,
)
lead = db.get_lead(lead_id)
direction = db.get_direction(direction_id)
fields = bitrix.lead_fields(lead, direction, bitrix.get_status_map(db))
assert fields["STATUS_ID"] == "CONVERTED", fields
print("  ok")

print("\n== немаппленный статус — STATUS_ID просто не передаётся, а не падает ==")
lead2_id = db.add_lead(
    None, None, {}, status=lead_domain.NEGOTIATION,
    display_name="Пётр", source_type=lead_domain.SOURCE_TYPE_MANUAL,
    event_source=lead_domain.EVENT_SOURCE_MANUAL,
)
lead2 = db.get_lead(lead2_id)
fields2 = bitrix.lead_fields(lead2, None, bitrix.get_status_map(db))
assert "STATUS_ID" not in fields2, fields2
print("  ok")

print("\n== маппинг направлений: SOURCE_ID из direction.crm_source_id, применяется при отправке ==")
db.update_direction(direction_id, crm_source_id="CALL")
direction = db.get_direction(direction_id)
fields3 = bitrix.lead_fields(lead, direction, {})
assert fields3["SOURCE_ID"] == "CALL", fields3
print("  ok")

print("\n== немаппленное направление и отсутствующее направление (None) — SOURCE_ID = OTHER, не падает ==")
unmapped_direction_id = db.add_direction("Батарейки")
unmapped_direction = db.get_direction(unmapped_direction_id)
fields4 = bitrix.lead_fields(lead, unmapped_direction, {})
assert fields4["SOURCE_ID"] == "OTHER", fields4
fields5 = bitrix.lead_fields(lead, None, {})
assert fields5["SOURCE_ID"] == "OTHER", fields5
print("  ok — неизвестное направление не роняет отправку")

print("\n== политика автоотправки по умолчанию — «только по кнопке», авто-постановка выключена ==")
assert bitrix.get_auto_send_policy(db) == bitrix.AUTO_SEND_MANUAL
bitrix.set_webhook_url(db, security, "https://portal.bitrix24.ru/rest/1/faketoken/")
fake = FakeClient("https://x")
service = BitrixSyncService(db, security, client_factory=lambda url: fake)
n = asyncio.run(service.tick())
assert n == 0, "manual-политика не должна ставить лиды в очередь сама"
print("  ok")

print("\n== политика «qualified» — в очередь попадают только лиды на реальной стадии воронки ==")
bitrix.set_auto_send_policy(db, bitrix.AUTO_SEND_QUALIFIED)
new_lead_id = db.add_lead(
    None, None, {}, status=lead_domain.NEW, display_name="Новый",
    source_type=lead_domain.SOURCE_TYPE_MANUAL, event_source=lead_domain.EVENT_SOURCE_MANUAL,
)
n = asyncio.run(service.tick())
assert db.get_crm_queue_entry(new_lead_id) is None, "новый лид не должен ставиться в очередь при qualified-политике"
assert db.get_crm_queue_entry(lead2_id) is not None or db.get_lead(lead2_id)["crm_id"] is not None, \
    "лид на стадии «переговоры» должен был попасть в очередь/уйти"
print("  ok")

print("\n== политика «all» — уходят все ещё не синхронизированные лиды, включая «новый» ==")
import datetime as _dt
bitrix.set_auto_send_policy(db, bitrix.AUTO_SEND_ALL)
asyncio.run(service.tick())
# Second tick, forced due, in case the same-second auto-enqueue above
# missed this tick's own due_crm_queue cutoff — deterministic either way.
future = _dt.datetime.now().astimezone() + _dt.timedelta(seconds=5)
asyncio.run(service.tick(now=future))
assert db.get_lead(new_lead_id)["crm_id"] is not None, "все — значит все, включая новый"
print("  ok")

print("\n== журнал синхронизации: показывает и ушедшие, и то, что ещё в очереди ==")
journal = db.crm_sync_journal(50)
outcomes = {r["outcome"] for r in journal}
assert "ok" in outcomes, journal
print("  ok —", len(journal), "запись(ей)")

db.close()
print("\nТЕСТ ПРОЙДЕН: маппинг Bitrix24 сохраняется, применяется, и не роняет отправку")
