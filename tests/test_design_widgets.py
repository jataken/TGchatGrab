"""Д2 («Плотный рефреш», design/design-brief.md §3): офскрин-стенд для
новой библиотеки общих компонентов в chatgrab/ui/widgets.py. Каждый виджет
строится с примерными данными и не падает; там, где есть публичное
поведение (StatusPill — пульсация, TabletCheckBox/ToggleSwitch — клик,
Sparkline/MetricsBar/LogPanel — данные), оно тоже проверяется напрямую, а
не только фактом успешной постройки.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(sys.argv)
from chatgrab.ui.theme import apply_theme

apply_theme(app)

from chatgrab.ui import widgets as w

print("== StatusPill: отдельная точка, пульсация только на активных статусах ==")
pill = w.StatusPill("idle")
assert pill._text.text() == "простаивает"
assert pill._dot.is_pulsing() is False
pill.set_status("loading")
assert pill._text.text() == "грузит историю"
assert pill._dot.is_pulsing() is True
pill.set_status("stopped")
assert pill._dot.is_pulsing() is False
pill.set_status("error")
assert pill._dot.is_pulsing() is True
print("  ok")

print("\n== icon_button: 24×24, класс icon ==")
btn = w.icon_button("✕", tooltip="Удалить")
assert btn.size().width() == 24 and btn.size().height() == 24
assert btn.property("class") == "icon"
assert btn.toolTip() == "Удалить"
print("  ok")

print("\n== TabletCheckBox: строится, клик по всей строке переключает состояние ==")
cb = w.TabletCheckBox("Только с фото")
assert cb.isChecked() is False
cb.click()
assert cb.isChecked() is True
cb.click()
assert cb.isChecked() is False
sh = cb.sizeHint()
assert sh.width() > 0 and sh.height() > 0
print("  ok")

print("\n== ToggleSwitch: логическое состояние меняется сразу, анимация — только отрисовка ==")
toggle = w.ToggleSwitch(False)
seen = []
toggle.toggled.connect(seen.append)
toggle.set_checked(True, emit=True)
assert toggle.is_checked() is True
assert seen == [True]
toggle.set_checked(False, emit=True)
assert toggle.is_checked() is False
assert seen == [True, False]
print("  ok")

print("\n== Sparkline: 30 столбиков максимум, строится и не падает при показе ==")
spark = w.Sparkline([i for i in range(40)], height=34)
assert len(spark._values) == 30, "должно быть обрезано до 30 последних значений"
spark.show()
spark.resize(120, 34)
app.processEvents()
spark.repaint()
spark.set_values([1, 5, 3])
assert spark._values == [1, 5, 3]
spark.hide()
print("  ok")

print("\n== MetricsBar: N ячеек через разделитель, обновление отдельной ячейки ==")
bar = w.MetricsBar([
    ("СООБЩЕНИЙ В БАЗЕ", "12 480", ""),
    ("МЕДИАФАЙЛОВ", "312", ""),
    ("РАЗМЕР БАЗЫ", "613.4", "МБ"),
])
assert len(bar._value_labels) == 3
bar.set_cell(2, "700.1", "МБ")
assert bar._value_labels[2].text() == "700.1"
assert bar._unit_labels[2].text() == "МБ"
print("  ok")

print("\n== Card: строится с полосой статуса и без ==")
plain_card = w.Card()
assert plain_card.property("class") == "card"
stripe_card = w.Card(stripe_color="#7FC79B")
stripe_card.resize(200, 80)
stripe_card.show()
app.processEvents()
stripe_card.repaint()  # тело paintEvent (блик + полоса) выполняется без падения
stripe_card.set_stripe_color(None)
stripe_card.hide()
print("  ok")

print("\n== AnimatedProgressBar: определённый и неопределённый режимы ==")
pbar = w.AnimatedProgressBar()
pbar.set_progress(42.5)
pbar.resize(160, 8)
pbar.show()
app.processEvents()
pbar.repaint()
pbar.set_progress(None)
pbar.set_active(True)
app.processEvents()
pbar.repaint()
pbar.set_active(False)
pbar.hide()
print("  ok")

print("\n== LogPanel: набор записей, добавление одной с анимацией ==")
log = w.LogPanel(kicker="ЖУРНАЛ СБОРА")
log.set_entries([
    {"time": "21.08 11:24", "chat": "Биржа", "text": "получено 40 сообщений", "tone": ""},
    {"time": "21.08 11:20", "chat": None, "text": "пауза 12с (FloodWait)", "tone": "warn"},
])
assert log._count_label.text().startswith("2 ")
log.add_entry({"time": "21.08 11:30", "chat": "Биржа", "text": "готово", "tone": "ok"})
assert log._count_label.text().startswith("3 ")
assert len(log._entries) == 3 and log._entries[0]["text"] == "готово"
print("  ok")

print("\n== PulseDot: халo и пульсация переключаются без падения ==")
dot = w.PulseDot(color="#9184D9", diameter=8, halo=True)
dot.show()
app.processEvents()
dot.set_pulsing(True)
app.processEvents()
dot.repaint()
dot.set_pulsing(False)
dot.hide()
print("  ok")

print("\nТЕСТ ПРОЙДЕН: библиотека общих компонентов Д2 строится и не падает")
