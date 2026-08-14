"""Основной журнал не растёт бесконечно."""
import sys, logging
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from chatgrab.paths import Paths
from chatgrab import safety_net
import os
base="/tmp/cgrot"; os.system(f"rm -rf {base}")
paths = Paths(Path(base)); paths.ensure()
safety_net.LOG_MAX_BYTES = 4096   # уменьшаем, чтобы не писать мегабайты
safety_net.install(paths)
safety_net.install(paths)   # повторный вызов не должен дублировать обработчики
log = logging.getLogger("chatgrab")
for i in range(600):
    log.warning("строка журнала номер %d, набиваем объём для проверки ротации", i)
files = sorted(p.name for p in paths.data_dir.glob("chatgrab.log*"))
print("файлы журнала:", files)
sizes = {p.name: p.stat().st_size for p in paths.data_dir.glob("chatgrab.log*")}
print("размеры:", sizes)
assert len(files) > 1, "ротация не сработала"
assert len(files) <= safety_net.LOG_BACKUPS + 1, f"бэкапов больше лимита: {files}"
assert sizes["chatgrab.log"] <= safety_net.LOG_MAX_BYTES * 1.5, "текущий файл не ограничен"
# проверяем, что повторный install не привёл к дублям строк
text = (paths.data_dir / "chatgrab.log").read_text(encoding="utf-8")
first = text.splitlines()[0]
assert text.count(first) == 1, "строки дублируются — обработчики сложились"
print("\nТЕСТ ПРОЙДЕН: журнал ротируется, обработчики не дублируются")
