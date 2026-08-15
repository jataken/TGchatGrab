"""С9: LLM-помощник выключен по умолчанию, ключ хранится через мастер-
пароль тем же путём, что и остальные интеграции, извлечение полей не
доверяет ответу модели вслепую, и — самое важное — выключенный (или
недонастроенный) помощник не делает ни одного сетевого запроса.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp

from chatgrab.paths import Paths
from chatgrab.config import AppConfig
from chatgrab.db.database import Database
from chatgrab.security import SecurityService
from chatgrab.integrations import llm

base = os.path.join(tempfile.gettempdir(), "cgllm")
shutil.rmtree(base, ignore_errors=True)
paths = Paths(Path(base))
paths.ensure()
db = Database(paths.db_path)
config = AppConfig.load(paths)
security = SecurityService(config, paths)

print("== parse_field_json: JSON модели не доверяется вслепую ==")
assert llm.parse_field_json('{"product": "глицерин", "volume": "2000"}') == {
    "product": "глицерин", "volume": "2000"}
# markdown-ограда вокруг JSON — модель иногда всё равно её добавляет,
# несмотря на просьбу в системном промпте.
assert llm.parse_field_json('```json\n{"city": "Москва"}\n```') == {"city": "Москва"}
# ключ вне SCENARIO_LEAD_FIELDS — отбрасывается, не протаскивается как
# попало в set_lead_field.
assert llm.parse_field_json('{"product": "x", "price": "100000"}') == {"product": "x"}
# пустые значения — не считаются найденными.
assert llm.parse_field_json('{"product": "", "city": "Москва"}') == {"city": "Москва"}
# не-JSON, не-объект — пустой результат, не исключение.
assert llm.parse_field_json("не json вообще") == {}
assert llm.parse_field_json('["product", "city"]') == {}
assert llm.parse_field_json("{}") == {}
print("  ok")

print("\n== выключен по умолчанию: и флаг, и ключ должны быть на месте ==")
assert llm.is_enabled(db, security) is False
assert llm.build_client(db, security) is None
print("  ok")

print("\n== ключ без включённого флага — всё ещё выключен ==")
llm.set_api_key(db, security, "sk-ant-test-key")
assert llm.get_api_key(db, security) == "sk-ant-test-key"
assert llm.is_enabled(db, security) is False, "ключ сам по себе не должен включать помощника"
assert llm.build_client(db, security) is None
print("  ok")

print("\n== включённый флаг без ключа — тоже выключен ==")
llm.set_api_key(db, security, None)
llm.set_enabled(db, True)
assert llm.get_api_key(db, security) is None
assert llm.is_enabled(db, security) is False, "флаг сам по себе не должен включать помощника без ключа"
assert llm.build_client(db, security) is None
print("  ok")

print("\n== оба условия выполнены — помощник включён, клиент собирается ==")
llm.set_api_key(db, security, "sk-ant-test-key")
assert llm.is_enabled(db, security) is True
client = llm.build_client(db, security)
assert client is not None and isinstance(client, llm.LLMClient)
assert client.model == llm.DEFAULT_MODEL
print("  ok")

print("\n== своя модель сохраняется и читается обратно ==")
llm.set_model(db, "  claude-sonnet-5  ")
assert llm.get_model(db) == "claude-sonnet-5"
client2 = llm.build_client(db, security)
assert client2.model == "claude-sonnet-5"
llm.set_model(db, "")
assert llm.get_model(db) == llm.DEFAULT_MODEL, "пустое значение должно откатываться на модель по умолчанию"
print("  ok")

print("\n== выключаем обратно — и это самый частый путь пользователя ==")
llm.set_enabled(db, False)
assert llm.is_enabled(db, security) is False
assert llm.build_client(db, security) is None
print("  ok")

print("\n== ни один из вызовов выше не тронул сеть ==")
_original_init = aiohttp.ClientSession.__init__
touched = {"session_created": False}


def _guard(self, *a, **kw):
    touched["session_created"] = True
    raise AssertionError("aiohttp.ClientSession не должен создаваться без явного вызова LLM-клиента")


aiohttp.ClientSession.__init__ = _guard
try:
    # Повторяем ровно тот путь, которым идёт кнопка на карточке лида —
    # is_enabled()/build_client() и ничего больше, раз помощник выключен.
    assert llm.build_client(db, security) is None
    llm.set_enabled(db, True)
    client3 = llm.build_client(db, security)
    assert client3 is not None
    # Клиент собран (помощник включён), но ни один его метод не вызван —
    # ровно так ведёт себя приложение, пока пользователь не нажал кнопку.
finally:
    aiohttp.ClientSession.__init__ = _original_init

assert touched["session_created"] is False, \
    "с выключенным (или не нажатым) помощником не должно быть ни одного сетевого вызова"
print("  ok — aiohttp.ClientSession ни разу не создавался")

db.close()
print("\nТЕСТ ПРОЙДЕН: LLM-помощник выключен по умолчанию и не трогает сеть, пока его явно не попросили")
