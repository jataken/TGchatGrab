"""Бэкап перед миграцией и откат отдельного шага.

Плановое решение (PLAN.md, С1): вместо полного набора down-скриптов —
обязательный бэкап файла перед тем, как migrate() тронет уже существующую
базу, плюс down() там, где он однострочный. Здесь проверяется само это
решение: бэкап появляется только когда есть что защищать, не дублируется
на пустом месте, и down() снимает ровно то, что добавил up().
"""
import os, sys
import sqlite3
import shutil
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatgrab.paths import Paths
from chatgrab.db.database import Database
from chatgrab.db import migrations

base = os.path.join(tempfile.gettempdir(), "cgmigback")
shutil.rmtree(base, ignore_errors=True)
paths = Paths(Path(base))
paths.ensure()


def backup_dir() -> Path:
    return paths.data_dir / "backups" / "pre_migration"


print("== свежая установка: бэкапить нечего ==")
db = Database(paths.db_path)
db.close()
assert not backup_dir().exists() or not list(backup_dir().glob("*.db")), \
    "новый файл без предыдущей схемы не должен порождать бэкап"
print("  ok, бэкапов нет")

print("\n== существующая база: бэкап делается один раз, перед первым применением ==")
shutil.rmtree(base, ignore_errors=True)
paths.ensure()

# База «версии 2» — как в test_migration.py, только на диске, руками.
raw = sqlite3.connect(str(paths.db_path))
raw.execute("CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT);")
raw.execute("CREATE TABLE chats (chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL, "
            "username TEXT, is_tracked INTEGER NOT NULL DEFAULT 1, enabled INTEGER NOT NULL DEFAULT 1, "
            "depth_mode TEXT NOT NULL DEFAULT 'all', depth_from_date TEXT, status TEXT NOT NULL DEFAULT 'idle', "
            "last_error TEXT, history_done INTEGER NOT NULL DEFAULT 0, oldest_loaded_id INTEGER, "
            "newest_loaded_id INTEGER, approx_total INTEGER, created_at TEXT NOT NULL, queue_order INTEGER);")
raw.execute("INSERT INTO app_meta VALUES ('schema_version','2')")
raw.execute("INSERT INTO chats(chat_id,title,created_at) VALUES (1,'Биржа','2026-01-01')")
raw.commit()
raw.close()

db = Database(paths.db_path)
backups = list(backup_dir().glob("*.db"))
print("  файлов бэкапа после первого открытия:", len(backups))
assert len(backups) == 1, backups
assert "before_006" in backups[0].name, backups[0].name

# Существующая строка должна быть видна и в бэкапе (это же файл до миграции)
backup_conn = sqlite3.connect(str(backups[0]))
title = backup_conn.execute("SELECT title FROM chats WHERE chat_id=1").fetchone()[0]
backup_conn.close()
print("  в бэкапе видна старая запись:", title)
assert title == "Биржа"

# И в живой базе она тоже осталась — миграция не потеряла данные.
row = db.query_one("SELECT title FROM chats WHERE chat_id=1")
assert row and row["title"] == "Биржа"
db.close()

print("\n== повторное открытие уже мигрированной базы бэкап не дублирует ==")
db = Database(paths.db_path)
db.close()
backups_after = list(backup_dir().glob("*.db"))
print("  файлов бэкапа:", len(backups_after))
assert len(backups_after) == 1, "нечего мигрировать — новый бэкап не должен появляться"

print("\n== откат снимает последнюю миграцию, у которой вообще есть down() ==")
copy_path = Path(base) / "rollback_copy.db"
shutil.copy2(paths.db_path, copy_path)
conn = sqlite3.connect(str(copy_path))
tables_before = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
assert "direction" in tables_before and "outbox_sends" in tables_before and "mailbox" in tables_before

# 017 (личности и черновики) — теперь самая свежая миграция вообще, и у
# неё есть down(): mail_identity/mail_draft/mail_draft_attachment —
# новые, самостоятельные таблицы, ничего существующего не ALTER'ит,
# откатывать нечего терять, тот же случай, что 007/010/014.
undone_newest = migrations.rollback_last(conn)
print("  откачена миграция:", undone_newest)
assert undone_newest == "017", "017 — самая свежая обратимая миграция, должна откатиться первой"
tables_after_newest = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
assert not {"mail_identity", "mail_draft", "mail_draft_attachment"} & tables_after_newest, \
    "все три таблицы 017 должны исчезнуть"
assert tables_before - tables_after_newest == {"mail_identity", "mail_draft", "mail_draft_attachment"}, \
    "откат 017 не должен трогать ничего, кроме своих таблиц"

# 014 (почта) — следующая по свежести с down(): 015/016 новее, но у них
# down() нет (015 ALTER'ит mail_message и пересобирает mail_fts, 016
# ALTER'ит mail_folder/mail_message и добавляет mail_action_queue — ни
# то ни другое не только добавляет новое, тот же случай, что
# 008/009/011/012 ниже), так что откат пропускает обе и берёт 014 —
# таблицы mail_* (кроме них) добавлены с нуля, откатить нечего терять.
undone0 = migrations.rollback_last(conn)
print("  откачена миграция:", undone0)
assert undone0 == "014", "014 — следующая по свежести обратимая миграция после 017"
tables_after0 = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
removed0 = tables_after_newest - tables_after0
# mail_action_queue — исключение: её создала 016 (сама без down(), тот же
# случай, что 015/012 ниже), а не 014, так что откат 014 её не трогает —
# ровно то же самое "более поздняя ALTER/ADD-таблица переживает откат
# своего основания", что ниже проверяется явно для 015/012.
mail_tables_after0 = {n for n in tables_after0 if n.startswith("mail")}
assert mail_tables_after0 == {"mail_action_queue"}, \
    f"все таблицы почты 014 (включая теневые mail_fts) должны исчезнуть, кроме mail_action_queue: {mail_tables_after0}"
assert all(name.startswith("mail") for name in removed0), \
    "откат 014 не должен трогать ничего, кроме своих (и своих теневых FTS5) таблиц"

# 010 (outbox) — следующая по свежести с down() (011/012 его не имеют).
undone = migrations.rollback_last(conn)
print("  откачена миграция:", undone)
assert undone == "010", "010 — следующая обратимая миграция после 014"
tables_after = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
assert {"outbox_sends", "outbox_drafts", "outbox_blacklist"} - tables_after == \
    {"outbox_sends", "outbox_drafts", "outbox_blacklist"}, "все три таблицы outbox должны исчезнуть"
assert tables_after == tables_after0 - {"outbox_sends", "outbox_drafts", "outbox_blacklist"}, \
    "откат не должен трогать ничего кроме своих таблиц"

# 008 (лид) и 009 (жизненный цикл лида) — следующие по свежести, но у них
# нет down() — обе трогают данные/ограничения таблицы, а не только
# добавляют что-то новое, см. их докстринги. Следующий откат должен
# пропустить обе и найти 007, а не упасть и не откатить не то.
undone2 = migrations.rollback_last(conn)
print("  откачена миграция:", undone2)
assert undone2 == "007", "008/009 без down() должны быть пропущены, а не откачены"
tables_after2 = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
assert "direction" not in tables_after2, "direction должна исчезнуть после отката"
assert tables_after2 == tables_after - {"direction"}, "откат не должен трогать ничего кроме своей таблицы"

applied = {r[0] for r in conn.execute("SELECT id FROM schema_migrations")}
print("  осталось применённых:", sorted(applied))
assert applied == {"006", "008", "009", "011", "012", "015", "016"}, \
    "008/009/011/012/015/016 без down() не откатывались — должны остаться применёнными " \
    "(015 и 016 в этом смысле не отличаются от 012: все ALTER'ят или пристраиваются к " \
    "таблице, чей down() уже откатился раньше — 014 для 015 и 016, 007 для 012 — " \
    "числящаяся-применённой запись переживает откат основания одинаково во всех случаях)"
conn.close()

# Оригинальный файл (не копия) не тронут — direction по-прежнему на месте.
live = Database(paths.db_path)
assert live.get_direction is not None  # метод существует
assert "direction" in {r[0] for r in live.query(
    "SELECT name FROM sqlite_master WHERE type='table'")}
live.close()
print("  оригинальная база не задета откатом на копии")

print("\nТЕСТ ПРОЙДЕН: бэкап и откат ведут себя так, как задумано")
