"""Справочник направлений: CRUD, порядок, экспорт/импорт.

Плоский список без номенклатуры (PLAN.md, С1) — пять направлений на
одного человека не требуют позиций и единиц измерения.
"""
import os, sys
import shutil
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatgrab.paths import Paths
from chatgrab.db.database import Database

base = os.path.join(tempfile.gettempdir(), "cgdirections")
shutil.rmtree(base, ignore_errors=True)
paths = Paths(Path(base))
paths.ensure()
db = Database(paths.db_path)

print("== пусто по умолчанию ==")
assert db.list_directions() == []
print("  ok")

print("\n== добавление и чтение ==")
d1 = db.add_direction("Косметическое сырьё", keywords=["глицерин", "отдушки"],
                      stop_words=["б/у"], price_file="prices/cosmetics.xlsx", note="основное")
d2 = db.add_direction("АКБ", keywords=["аккумулятор"])
d3 = db.add_direction("Упаковка для молочки")
rows = db.list_directions()
print("  порядок по умолчанию:", [r["name"] for r in rows])
assert [r["id"] for r in rows] == [d1, d2, d3], "новые направления должны идти по порядку добавления"

row = db.get_direction(d1)
import json
assert json.loads(row["keywords"]) == ["глицерин", "отдушки"]
assert json.loads(row["stop_words"]) == ["б/у"]
assert row["price_file"] == "prices/cosmetics.xlsx"
assert row["note"] == "основное"
assert row["enabled"] == 1
print("  поля сохранились как заданы")

print("\n== редактирование ==")
db.update_direction(d2, keywords=["аккумулятор", "батарея"], enabled=0)
row = db.get_direction(d2)
assert json.loads(row["keywords"]) == ["аккумулятор", "батарея"]
assert row["enabled"] == 0
print("  ok")
assert [r["id"] for r in db.list_directions(enabled_only=True)] == [d1, d3], \
    "выключенное направление не должно попадать в enabled_only"

print("\n== порядок: перестановка ==")
db.reorder_directions([d3, d1, d2])
order = [r["id"] for r in db.list_directions()]
print("  ", order)
assert order == [d3, d1, d2]

print("\n== удаление ==")
db.delete_direction(d2)
assert db.get_direction(d2) is None
assert [r["id"] for r in db.list_directions()] == [d3, d1]
print("  ok")

print("\n== экспорт -> импорт в чистую базу ==")
exported = db.export_directions()
print("  ", exported)
assert len(exported["directions"]) == 2
assert "id" not in exported["directions"][0], "экспорт не должен содержать id — импорт создаёт новые строки"

base2 = os.path.join(tempfile.gettempdir(), "cgdirections2")
shutil.rmtree(base2, ignore_errors=True)
paths2 = Paths(Path(base2))
paths2.ensure()
db2 = Database(paths2.db_path)
added = db2.import_directions(exported)
print("  импортировано:", added)
assert added == 2
rows2 = db2.list_directions()
assert {r["name"] for r in rows2} == {"Упаковка для молочки", "Косметическое сырьё"}
cosmetics = next(r for r in rows2 if r["name"] == "Косметическое сырьё")
assert json.loads(cosmetics["keywords"]) == ["глицерин", "отдушки"]
assert cosmetics["price_file"] == "prices/cosmetics.xlsx"
print("  поля переехали без потерь")

print("\n== повторный импорт без replace добавляет, а не перезаписывает ==")
db2.import_directions(exported)
assert len(db2.list_directions()) == 4, "второй импорт должен добавить ещё две строки"
print("  ok, теперь", len(db2.list_directions()))

print("\n== импорт с replace=True сначала чистит список ==")
added = db2.import_directions(exported, replace=True)
assert added == 2
assert len(db2.list_directions()) == 2
print("  ok, осталось", len(db2.list_directions()))

print("\n== битые данные не роняют импорт ==")
assert db2.import_directions({"not": "a valid shape"}) == 0
assert db2.import_directions([1, 2, 3]) == 0
assert db2.import_directions({"directions": "not a list"}) == 0
messy = {"directions": [
    {"name": "Годная запись", "keywords": ["ок"]},
    {"name": ""},                       # пустое имя — пропускается
    {"no_name_field": True},            # нет имени вовсе — пропускается
    "просто строка",                    # не словарь — пропускается
    {"name": "Без списков", "keywords": "не список", "stop_words": 42},
]}
before = len(db2.list_directions())
added = db2.import_directions(messy)
print("  добавлено из вперемешку годных и битых записей:", added)
assert added == 2, "должны пройти только «Годная запись» и «Без списков»"
assert len(db2.list_directions()) == before + 2
without_lists = next(r for r in db2.list_directions() if r["name"] == "Без списков")
assert json.loads(without_lists["keywords"]) == [], "строка вместо списка должна стать пустым списком, не упасть"

db.close()
db2.close()
print("\nТЕСТ ПРОЙДЕН: справочник направлений работает")
