"""Code-review pass over П1–П10 (the whole mail feature): three real bugs
found and fixed, each covered here.

1. IMAP servers are free to treat a range's two endpoints as interchangeable
   (RFC 3501 §9 — "2:4" and "4:2" are the same range). Once a mailbox is
   fully caught up, "UID FETCH <last_seen+1>:*" is a range whose low end
   doesn't exist and whose high end ("*") resolves to last_seen itself —
   several real servers respond by returning the message *at* last_seen
   instead of nothing, and imap_client.fetch_new_headers() used to trust
   that blindly, making an already-seen message look new on every tick
   forever (re-notifying, and resetting the П9 "мы не ответили" reminder).
2. leads_tab.py's status-chip filter called list_leads(status=...) without
   funnel_id — harmless before П9, but С10's migration 020 seeds the mail
   funnel with stage codes ("new"/"qualified"/…) that collide with the
   default funnel's own, so an email lead at "new" leaked into a chip whose
   own count (leads_status_counts(), already funnel-scoped) didn't include it.
3. Mail retention compared `date < cutoff` directly — NULL for any message
   whose Date header failed to parse, which SQL never treats as "less than"
   anything, so such messages were permanently exempt from a configured
   retention period.
"""
import sys
import datetime as dt
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _bootstrap import fresh_db, fresh_env
from _fake_imap import FakeImapConnection, make_client_factory
from chatgrab.core import lead as lead_domain
from chatgrab.integrations.mail import credentials as mail_credentials
from chatgrab.integrations.mail.imap_client import ImapClient
from chatgrab.services.mail_service import MailService

# ---- 1: UID range-swap defence ---------------------------------------------
print("== fetch_new_headers: сервер, переворачивающий пустой диапазон, не выдаёт письмо второй раз ==")


class _SwappedRangeConnection(FakeImapConnection):
    """Ровно то, что реальный сервер вправе сделать по RFC 3501 §9: раз
    концы диапазона взаимозаменяемы, "<since_uid+1>:*" при отсутствии
    новых писем эквивалентен "<since_uid>:<since_uid+1>" — и сервер
    отдаёт единственное существующее в этих границах письмо (last_uid),
    а не пустой результат. Общий _fake_imap.py этого не делает (там
    честное u >= start), так что для этого теста — отдельный маленький
    стаб, не трогающий остальные тесты, использующие общий фейк."""

    def _fetch(self, args):
        seq, _items = args
        info = self.folders[self._selected]
        msgs = info["messages"]
        if ":" in seq:
            start = int(seq.split(":")[0])
            max_uid = max(msgs) if msgs else 0
            if start > max_uid and max_uid in msgs:
                uids = [max_uid]  # «перевёрнутый» диапазон — старое письмо снова
            else:
                uids = sorted(u for u in msgs if u >= start)
        else:
            u = int(seq)
            uids = [u] if u in msgs else []
        data = []
        for u in uids:
            raw = msgs[u]
            meta = f"{u} (UID {u} FLAGS () BODY[HEADER] {{{len(raw)}}}".encode()
            data.append((meta, raw))
            data.append(b")")
        return "OK", (data or [None])


swap_state = {
    "imap.swap.test": {
        "INBOX": {"uidvalidity": 1, "messages": {
            1: b"From: a@b.ru\r\nSubject: one\r\nDate: Mon, 17 Aug 2026 10:00:00 +0300\r\n\r\nhi\r\n",
        }},
    },
}
client = ImapClient("imap.swap.test", 993,
                     connection_factory=lambda: _SwappedRangeConnection(swap_state["imap.swap.test"]))
client.connect("me@swap.test", "correct-password")
first = client.fetch_new_headers("INBOX", 0)
assert len(first) == 1 and first[0][0] == 1, first
print("  ok — первичный синк честно вернул письмо 1")

caught_up = client.fetch_new_headers("INBOX", 1)
assert caught_up == [], (
    "сервер «перевернул» пустой диапазон 2:* и снова отдал письмо 1 — "
    "fetch_new_headers должен был отфильтровать его как уже виденное")
print("  ok — то же письмо, отданное сервером повторно, отфильтровано как не-новое")
client.close()


# ---- 2: leads_tab.py — фильтр по статусу теперь учитывает funnel_id -------
print("\n== list_leads(status=..., funnel_id=...): чипы не текут между воронками с совпадающими кодами ==")
paths, db = fresh_db("cgreviewfix")
tg_lead = db.add_lead(None, None, {}, status="new", source_type=lead_domain.SOURCE_TYPE_MANUAL)
mail_funnel = db.get_funnel_by_channel(lead_domain.ORIGIN_CHANNEL_EMAIL)
email_lead = db.add_lead(None, None, {}, status="new", source_type=lead_domain.SOURCE_TYPE_EMAIL,
                          funnel_id=mail_funnel["id"], origin_channel=lead_domain.ORIGIN_CHANNEL_EMAIL)
# Оба лида на статусе "new" — код совпадает в обеих воронках (миграция 020).
same_status_leads = db.list_leads(status="new")
assert {l["id"] for l in same_status_leads} == {tg_lead, email_lead}, \
    "без funnel_id list_leads() закономерно видит оба — так и было задумано у самого метода"

default_funnel_id = db.default_funnel_id()
scoped = db.list_leads(status="new", funnel_id=default_funnel_id)
assert [l["id"] for l in scoped] == [tg_lead], \
    "с funnel_id почтовый лид на том же коде статуса не должен утекать в телеграмную воронку"
counts = db.leads_status_counts()  # default_funnel_id() по умолчанию — то же самое, чем фильтруются чипы
assert counts.get("new") == len(scoped), \
    "строки, которые видит чип, и число на самом чипе должны совпадать"
print("  ok — отфильтрованный список теперь совпадает со счётчиком чипа:", counts.get("new"))

print("\n-- LeadsTab.refresh(): офскрин-проверка, что экран использует именно этот путь --")
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication(sys.argv)


class _StubCtx:
    def __init__(self, database):
        self.db = database


from chatgrab.ui.screens.bots.leads_tab import LeadsTab
tab = LeadsTab(_StubCtx(db))
tab._set_filter("new")
assert len(tab._rows) == len(scoped) == 1, len(tab._rows)
print("  ok — на экране ровно одна строка на чипе «new», как и должно быть")


# ---- 3: ретеншн почты не пропускает письма без даты навсегда --------------
print("\n== ретеншн: письмо с NULL date (не распарсился заголовок) больше не исключено навсегда ==")
paths2, db2, config2, security2 = fresh_env("cgreviewfix2")
mailbox_id = db2.add_mailbox("sales@x.ru", "imap.x.test", 993,
                              password_enc=mail_credentials.encrypt_password(security2, "correct-password"))
old_created = (dt.datetime.now().astimezone() - dt.timedelta(days=400)).isoformat(timespec="seconds")
msg_id = db2.upsert_mail_message(mailbox_id, "INBOX", 1, subject="битая дата", sender_address="a@b.ru",
                                  to_addresses="[]", date=None)
# upsert_mail_message пишет created_at сама (now_iso()) — подменяем на
# «старую», как будто письмо реально было собрано давно, иначе тест
# зависел бы от системного времени раннера.
db2.execute("UPDATE mail_message SET created_at = ? WHERE id = ?", (old_created, msg_id))
assert db2.get_mail_message(msg_id)["date"] is None

from chatgrab.services.mail_retention_service import MailRetentionService, cutoff_for
ret_svc = MailRetentionService(db2, paths2)
ret_svc.set_months(6)
cutoff = cutoff_for(6)
assert db2.count_mail_messages_older_than(cutoff) == 1, \
    "письмо без date, но со старым created_at, должно было засчитаться по резервной дате"
preview = ret_svc.preview()
assert preview["messages"] == 1
result = ret_svc.archive_and_prune()
assert result["deleted"] == 1
assert db2.get_mail_message(msg_id) is None
print("  ok — письмо без даты в заголовке попало под ретеншн по created_at и было удалено")

print("\n== письмо без даты, но недавнее (created_at внутри срока хранения) — не трогается ==")
msg_id2 = db2.upsert_mail_message(mailbox_id, "INBOX", 2, subject="тоже без даты, но свежее",
                                   sender_address="a@b.ru", to_addresses="[]", date=None)
assert db2.count_mail_messages_older_than(cutoff_for(6)) == 0, \
    "свежее письмо без даты не должно попадать под ретеншн раньше срока"
print("  ok")

print("\n== всё сошлось ==")
