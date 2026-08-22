import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _bootstrap import fresh_db

paths, db = fresh_db("cgids")
bot_id = db.add_bot("B", "userbot", None, "custom", None)

sc = db.add_scenario(bot_id, "S", [
    {"question": "A?", "field": "a", "validation": "text"},
    {"question": "B?", "field": "b", "validation": "text"},
])
steps = json.loads(db.get_scenario(sc)["steps"])
print("после создания:", [(s["id"], s["field"]) for s in steps])
ids = [s["id"] for s in steps]
assert len(set(ids)) == 2

# вставляем шаг в середину — существующие id обязаны сохраниться
steps.insert(1, {"question": "NEW?", "field": "new", "validation": "text"})
db.update_scenario(sc, steps=steps)
after = json.loads(db.get_scenario(sc)["steps"])
print("после вставки в середину:", [(s["id"], s["field"]) for s in after])
assert after[0]["id"] == ids[0], "id первого шага изменился"
assert after[2]["id"] == ids[1], "id сдвинувшегося шага изменился"
assert len({s["id"] for s in after}) == 3, "id не уникальны"

# перестановка тоже не должна менять id
after[0], after[1] = after[1], after[0]
db.update_scenario(sc, steps=after)
final = json.loads(db.get_scenario(sc)["steps"])
print("после перестановки:", [(s["id"], s["field"]) for s in final])
assert {s["id"] for s in final} == {s["id"] for s in after}
print("\nТЕСТ ПРОЙДЕН: id шагов стабильны")
