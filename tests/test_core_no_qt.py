"""core/ — домен без фреймворка. Проверяется буквально то, что обещано в
docstring пакета: ничего внутри не тянет PySide6/telethon/aiogram/sqlite3,
и ключевые правила (переход в «отказ» требует причины) работают без
запуска Qt и без файла базы.

Импорт-тест читает исходники статически (ast), а не просто "импортировал
и не упало" — модуль без прямого импорта Qt всё равно мог бы случайно
завести побочную зависимость через транзитивный импорт чего-то из
db/ui/telegram/bots; ast-проверка ловит это на уровне текста модуля,
а не только на уровне того, что уже случилось попасть в sys.modules
у кого-то другого.
"""
import ast
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FORBIDDEN = ("PySide6", "telethon", "aiogram", "sqlite3")

print("== core/*.py не импортирует Qt, Telethon, aiogram, sqlite3 ==")
core_dir = Path(__file__).resolve().parent.parent / "chatgrab" / "core"
py_files = sorted(core_dir.glob("*.py"))
assert py_files, "chatgrab/core пуст или не найден"

offenders = []
for path in py_files:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            if any(name == f or name.startswith(f + ".") for f in FORBIDDEN):
                offenders.append((path.name, name))

for name, imp in offenders:
    print(f"  ⚠ {name} импортирует {imp}")
print(f"  проверено файлов: {[p.name for p in py_files]}")
assert not offenders, f"core/ не должен зависеть от фреймворка: {offenders}"
print("  ok")

from chatgrab.core import lead as lead_domain

print("\n== импорт модуля не тянет за собой ничего из перечисленного ==")
before = set(sys.modules)
import importlib
importlib.reload(lead_domain)
leaked = {m for m in set(sys.modules) - before
          if any(m == f or m.startswith(f + ".") for f in FORBIDDEN)}
print("  ", leaked or "ничего")
assert not leaked, leaked

# С10: the funnel is DB rows now (funnel/funnel_stage), not module
# constants — core/lead.py's functions take an explicit stage list
# instead, so this file plugs in DEFAULT_FUNNEL_STAGES (the exact set
# migration 013 seeds) to keep testing the same rules without a database.
stages = lead_domain.DEFAULT_FUNNEL_STAGES

print("\n== единственное настоящее правило: этап с requires_reason требует причины ==")
assert lead_domain.validate_transition(stages, lead_domain.LOST, None) is not None
assert lead_domain.validate_transition(stages, lead_domain.LOST, "  ") is not None
assert lead_domain.validate_transition(stages, lead_domain.LOST, "нашёл другого поставщика") is None
assert lead_domain.validate_transition(stages, lead_domain.WON, None) is None
assert lead_domain.validate_transition(stages, lead_domain.QUALIFIED, None) is None
assert lead_domain.validate_transition(stages, "выдуманный статус", None) is not None
print("  ok")

print("\n== next_stage идёт по открытым+won этапам воронки и зацикливается с won/lost на первый ==")
advanceable_count = len([s for s in stages if s["kind"] in (lead_domain.KIND_OPEN, lead_domain.KIND_WON)])
seq = [lead_domain.NEW]
for _ in range(advanceable_count):
    seq.append(lead_domain.next_stage(stages, seq[-1]))
print("  ", seq)
assert seq == [lead_domain.NEW, lead_domain.QUALIFIED, lead_domain.QUOTE_SENT,
               lead_domain.NEGOTIATION, lead_domain.WON, lead_domain.NEW]
assert lead_domain.next_stage(stages, lead_domain.LOST) == lead_domain.NEW
print("  ok")

print("\n== bucket_for_stage/bucket_counts: первый открытый этап — «new», won+lost — «closed» ==")
assert lead_domain.bucket_for_stage(stages, lead_domain.NEW) == "new"
assert lead_domain.bucket_for_stage(stages, lead_domain.QUALIFIED) == "in_progress"
assert lead_domain.bucket_for_stage(stages, lead_domain.WON) == "closed"
assert lead_domain.bucket_for_stage(stages, lead_domain.LOST) == "closed"
assert lead_domain.bucket_for_stage(stages, "выдуманный статус") is None
counts = {lead_domain.NEW: 3, lead_domain.QUALIFIED: 2, lead_domain.QUOTE_SENT: 1,
          lead_domain.WON: 4, lead_domain.LOST: 1}
assert lead_domain.bucket_counts(stages, counts) == {"new": 3, "in_progress": 3, "closed": 5}
print("  ok")

print("\n== remap_legacy_status ==")
assert lead_domain.remap_legacy_status("new") == lead_domain.NEW
assert lead_domain.remap_legacy_status("in_progress") == lead_domain.NEGOTIATION
assert lead_domain.remap_legacy_status("closed") == lead_domain.WON
assert lead_domain.remap_legacy_status("что-то незнакомое") == lead_domain.NEW
print("  ok")

print("\n== метки для каждого этапа и типа источника заданы ==")
for stage in stages:
    assert lead_domain.label_for_stage(stages, stage["code"]), stage["code"]
for source_type in (lead_domain.SOURCE_TYPE_CHAT, lead_domain.SOURCE_TYPE_DM,
                    lead_domain.SOURCE_TYPE_BOT, lead_domain.SOURCE_TYPE_MANUAL):
    assert lead_domain.label_for_source_type(source_type), source_type
print("  ok")

print("\nТЕСТ ПРОЙДЕН: core/ изолирован и его правила работают сами по себе")
