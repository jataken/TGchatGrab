"""Несколько аккаунтов Telegram: маршрутизация чатов и ботов.

Проверяется без сети — вместо Telethon подставлены заглушки клиентов.
Важно здесь ровно одно: сообщение, пришедшее на один аккаунт, не должно
обрабатываться ботом другого, а сущность чата не должна утекать между
клиентами (в ней лежит access_hash, выданный конкретному аккаунту).
"""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _bootstrap import fresh_env
from chatgrab.telegram.service import TelegramService
from chatgrab.telegram.accounts import AccountRegistry, session_file_for

paths, db, config, _ = fresh_env("cgacc")

print("== имя файла сессии из имени аккаунта ==")
taken = set()
for name in ["Основной", "для ботов", "для ботов", "  ??? "]:
    f = session_file_for(name, taken)
    taken.add(f)
    print(f"  {name!r:<14} -> {f}")
assert len(taken) == 4, "имена файлов должны быть уникальными"
assert all(f.endswith(".session") and f.isascii() and "/" not in f for f in taken), \
    "имя файла должно быть латиницей: путь уходит в SQLite и в бэкапы"

print("\n== пока аккаунт один, всё как раньше ==")
primary = TelegramService(config)
reg = AccountRegistry(db, config, paths, primary)
assert reg.service_for(None) is primary
assert not db.list_accounts()
Path(config.session_path).write_bytes(b"x")   # «уже вошли» со старой версии
acc_main = reg.ensure_primary_row()
assert acc_main is not None and len(db.list_accounts()) == 1
assert reg.service_for(acc_main) is primary, "основной аккаунт обслуживает тот же сервис"
print("  основной аккаунт заведён из существующей сессии, id =", acc_main)

print("\n== второй аккаунт получает свой файл сессии ==")
acc_bot = db.add_account("Для ботов", session_file_for("Для ботов", {"worker.session"}))
svc_bot = reg.service_for(acc_bot)
assert svc_bot is not primary
assert svc_bot.session_path_override and svc_bot.session_path_override != config.session_path
print("  ", Path(svc_bot.session_path_override).name)
assert reg.service_for(acc_bot) is svc_bot, "сервис должен переиспользоваться"

print("\n== чат закреплён за аккаунтом ==")
db.add_chat(1001, "Биржа", "b", "all", None)
db.add_chat(1002, "Упаковка", "u", "all", None)
db.set_chat_field(1002, account_id=acc_bot)
assert reg.for_chat(db.get_chat(1001)) is primary, "без account_id — основной"
assert reg.for_chat(db.get_chat(1002)) is svc_bot
print("  ok")

# ---- collector: entity не течёт между аккаунтами --------------------
print("\n== сущность чата кэшируется отдельно для каждого аккаунта ==")
from chatgrab.telegram.collector import Collector


class FakeClient:
    def __init__(self, tag): self.tag = tag; self.calls = 0
    async def get_entity(self, peer):
        self.calls += 1
        return f"{self.tag}:{peer}"
    def is_connected(self): return True
    def add_event_handler(self, *a, **k): pass


async def noop_connect(self): pass
TelegramService.connect = noop_connect          # без сети
primary.client = FakeClient("A")
svc_bot.client = FakeClient("B")

collector = Collector(db, primary, config, paths)
collector.accounts = reg
loop = asyncio.new_event_loop()
e1 = loop.run_until_complete(collector._get_entity(db.get_chat(1001)))
e2 = loop.run_until_complete(collector._get_entity(db.get_chat(1002)))
print("  чат 1001 ->", e1, "| чат 1002 ->", e2)
assert e1 == "A:1001" and e2 == "B:1002", "каждый чат читается своим аккаунтом"
loop.run_until_complete(collector._get_entity(db.get_chat(1001)))
assert primary.client.calls == 1, "второй запрос должен браться из кэша"

print("\n== боты: сообщение видит только бот своего аккаунта ==")
from chatgrab.bots.manager import BotManager
from chatgrab.security import SecurityService

sec = SecurityService(config, paths)
mgr = BotManager(db, primary, sec)
mgr.userbot_runner.accounts = reg
bot_main = mgr.create_bot("На основном", "userbot", None, "custom", None)
bot_alt = mgr.create_bot("На втором", "userbot", None, "custom", None)
db.set_bot_field(bot_alt, account_id=acc_bot)
db.set_bot_field(bot_main, status="running")
db.set_bot_field(bot_alt, status="running")

seen_a = [b["name"] for b in mgr.userbot_runner._running_bots(primary.client)]
seen_b = [b["name"] for b in mgr.userbot_runner._running_bots(svc_bot.client)]
print("  сообщение на аккаунт A видят:", seen_a)
print("  сообщение на аккаунт B видят:", seen_b)
assert seen_a == ["На основном"], seen_a
assert seen_b == ["На втором"], seen_b

print("\n== удаление аккаунта возвращает чаты и ботов на основной ==")
usage = db.account_usage(acc_bot)
print("  до удаления:", usage)
assert usage == {"chats": 1, "bots": 1}
db.delete_account(acc_bot)
reg.forget(acc_bot)
assert db.get_chat(1002)["account_id"] is None
assert db.get_bot(bot_alt)["account_id"] is None
assert reg.for_chat(db.get_chat(1002)) is primary
print("  ok — ничего не осталось указывать в никуда")

print("\n== мастер-пароль шифрует все файлы сессий, а не только основной ==")
paths.session_dir.mkdir(parents=True, exist_ok=True)
(paths.session_dir / "dlya-botov.session").write_bytes(b"second-account")
Path(config.session_path).write_bytes(b"primary-account")
config.api_hash = "0123456789abcdef0123456789abcdef"
sec2 = SecurityService(config, paths)
sec2.enable("парольпарольпароль")
left = sorted(p.name for p in paths.session_dir.glob("*.session"))
enc = sorted(p.name for p in paths.session_dir.glob("*.session.enc"))
print("  открытым текстом:", left, "| зашифровано:", enc)
assert left == [], "ни один файл входа не должен остаться в открытом виде"
assert len(enc) == 2
sec2.unlock("парольпарольпароль")
back = sorted(p.name for p in paths.session_dir.glob("*.session"))
assert len(back) == 2, back
assert (paths.session_dir / "dlya-botov.session").read_bytes() == b"second-account"
print("  расшифровано обратно:", back)

print("\nТЕСТ ПРОЙДЕН: аккаунты не смешиваются")
