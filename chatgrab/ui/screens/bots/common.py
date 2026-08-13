from __future__ import annotations

from PySide6.QtWidgets import QComboBox

from ...context import AppContext


def make_bot_picker(ctx: AppContext, on_change) -> QComboBox:
    combo = QComboBox()
    combo.currentIndexChanged.connect(lambda _: on_change(combo.currentData()))
    return combo


def populate_bot_picker(ctx: AppContext, combo: QComboBox) -> None:
    current = combo.currentData()
    combo.blockSignals(True)
    combo.clear()
    for bot in ctx.db.list_bots():
        combo.addItem(f"{bot['name']} ({bot['type']})", bot["id"])
    idx = combo.findData(current)
    combo.setCurrentIndex(idx if idx >= 0 else (0 if combo.count() else -1))
    combo.blockSignals(False)
