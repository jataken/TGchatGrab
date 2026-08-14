"""Наблюдение за словами: совпадения находятся, не дублируются,
история не заваливает уведомлениями."""
import os, sys
import tempfile
import shutil
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from chatgrab.paths import Paths
from chatgrab.db.database import Database
from chatgrab.services.watch_service import WatchService

base=os.path.join(tempfile.gettempdir(), "cgwatch"); shutil.rmtree(base, ignore_errors=True)
paths=Paths(Path(base)); paths.ensure(); db=Database(paths.db_path)
db.add_chat(1, "Биржа", "b", "all", None)
db.add_chat(2, "Упаковка", "u", "all", None)

notes=[]
svc = WatchService(db, on_hit=lambda rule, rec: notes.append((rule["phrase"], rec["message_id"])))

def msg(mid, text, chat=1, caption=None):
    return {"chat_id": chat, "message_id": mid, "text": text, "media_caption": caption}

print("== без правил ничего не происходит ==")
assert svc.check(msg(1, "куплю глицерин 2 тонны")) == []
print("  ok")

r1 = db.add_watch_rule("куплю глицерин")
r2 = db.add_watch_rule("флаконы", chat_id=2)
r3 = db.add_watch_rule("тихое слово", notify=False)
svc.invalidate()

print("\n== совпадение находится и уведомляет ==")
hits = svc.check(msg(10, "Здравствуйте! КУПЛЮ   ГЛИЦЕРИН 99,5%, 2 тонны"))
print("  сработало правил:", len(hits), "| уведомления:", notes)
assert len(hits) == 1 and notes, "регистр/пробелы должны игнорироваться"

print("\n== повторная проверка того же сообщения не дублирует ==")
notes.clear()
assert svc.check(msg(10, "Здравствуйте! КУПЛЮ ГЛИЦЕРИН 99,5%, 2 тонны")) == []
assert not notes
print("  ok, всего находок:", len(db.list_watch_hits()))
assert len(db.list_watch_hits()) == 1

print("\n== правило, привязанное к чату, не срабатывает в другом ==")
notes.clear()
assert svc.check(msg(20, "нужны флаконы ПЭТ", chat=1)) == []
hits = svc.check(msg(21, "нужны флаконы ПЭТ", chat=2))
print("  в чате 1:", 0, "| в чате 2:", len(hits))
assert len(hits) == 1

print("\n== notify=0: находка есть, уведомления нет ==")
notes.clear()
hits = svc.check(msg(30, "здесь тихое слово внутри"))
print("  находок:", len(hits), "| уведомлений:", len(notes))
assert len(hits) == 1 and not notes

print("\n== подпись к медиа тоже проверяется ==")
hits = svc.check(msg(40, "", caption="куплю глицерин оптом"))
assert len(hits) == 1
print("  ok")

print("\n== выключенное правило молчит ==")
db.set_watch_rule(r1, enabled=0); svc.invalidate()
assert svc.check(msg(50, "куплю глицерин ещё раз")) == []
print("  ok")
db.set_watch_rule(r1, enabled=1); svc.invalidate()

print("\n== непрочитанные считаются, отметка работает ==")
print("  непрочитано:", db.unseen_watch_count())
assert db.unseen_watch_count() == len(db.list_watch_hits())
db.mark_watch_hits_seen()
assert db.unseen_watch_count() == 0
print("  после отметки:", db.unseen_watch_count())

print("\n== rescan по истории не шлёт уведомлений ==")
for i in range(100, 110):
    db.upsert_message({"chat_id":1,"message_id":i,"chat_title":"Биржа","date":f"2026-08-01T10:00:{i%60:02d}",
        "edited_date":None,"sender_id":1,"sender_username":"u","sender_display_name":"U",
        "text":"куплю глицерин партию","reply_to_message_id":None,"forwarded_from":None,
        "media_type":None,"media_caption":None,"media_path":None,"views":None,"link":"",
        "is_hidden":0,"char_len":20,"is_reply":0,"is_forward":0})
notes.clear()
found = svc.rescan()
print(f"  найдено в истории: {found}, уведомлений: {len(notes)}")
assert found == 10, found
assert not notes, "rescan не должен слать уведомления пачкой"

print("\n== удаление правила убирает его находки ==")
before = len(db.list_watch_hits(limit=1000))
db.delete_watch_rule(r1)
after = len(db.list_watch_hits(limit=1000))
print(f"  находок было {before}, стало {after}")
assert after < before

print("\nТЕСТ ПРОЙДЕН: наблюдение за словами работает")
