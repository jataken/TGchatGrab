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

print("\n== единственное настоящее правило: «отказ» требует причины ==")
assert lead_domain.validate_transition(lead_domain.LOST, None) is not None
assert lead_domain.validate_transition(lead_domain.LOST, "  ") is not None
assert lead_domain.validate_transition(lead_domain.LOST, "нашёл другого поставщика") is None
assert lead_domain.validate_transition(lead_domain.WON, None) is None
assert lead_domain.validate_transition(lead_domain.QUALIFIED, None) is None
assert lead_domain.validate_transition("выдуманный статус", None) is not None
print("  ok")

print("\n== next_status идёт по воронке и зацикливается с won/lost на новый ==")
seq = [lead_domain.NEW]
for _ in range(len(lead_domain.STATUS_ORDER)):
    seq.append(lead_domain.next_status(seq[-1]))
print("  ", seq)
assert seq == [lead_domain.NEW, lead_domain.QUALIFIED, lead_domain.QUOTE_SENT,
               lead_domain.NEGOTIATION, lead_domain.WON, lead_domain.NEW]
assert lead_domain.next_status(lead_domain.LOST) == lead_domain.NEW
print("  ok")

print("\n== remap_legacy_status ==")
assert lead_domain.remap_legacy_status("new") == lead_domain.NEW
assert lead_domain.remap_legacy_status("in_progress") == lead_domain.NEGOTIATION
assert lead_domain.remap_legacy_status("closed") == lead_domain.WON
assert lead_domain.remap_legacy_status("что-то незнакомое") == lead_domain.NEW
print("  ok")

print("\n== метки для каждого статуса и типа источника заданы ==")
for status in lead_domain.ALL_STATUSES:
    assert lead_domain.label_for_status(status), status
for source_type in (lead_domain.SOURCE_TYPE_CHAT, lead_domain.SOURCE_TYPE_DM,
                    lead_domain.SOURCE_TYPE_BOT, lead_domain.SOURCE_TYPE_MANUAL):
    assert lead_domain.label_for_source_type(source_type), source_type
print("  ok")

print("\nТЕСТ ПРОЙДЕН: core/ изолирован и его правила работают сами по себе")
