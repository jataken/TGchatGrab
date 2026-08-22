from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QHBoxLayout, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

from ... import theme
from ...context import AppContext
from ...widgets import muted


class LogTab(QWidget):
    """Same log-panel pattern as the parser's Сбор данных screen, driven
    by BotManager.log_event instead of Collector.log_event."""

    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 16, 0, 16)

        header = QHBoxLayout()
        header.addWidget(muted("СОБЫТИЯ БОТОВ — сработавшие триггеры, ошибки, отправленные сообщения"))
        header.addStretch(1)
        self.count_label = muted("")
        header.addWidget(self.count_label)
        outer.addLayout(header)

        self.log_list = QListWidget()
        self.log_list.setStyleSheet(
            f"QListWidget {{ border: none; background: transparent; "
            f"font-family: {theme.FONT_MONO}; font-size: 12px; }}"
            "QListWidget::item { padding: 4px 12px; }"
        )
        outer.addWidget(self.log_list, 1)

        ctx.bot_manager.log_event.connect(self._on_log_event)

    def on_show(self) -> None:
        self._reload()

    def _reload(self) -> None:
        self.log_list.clear()
        for entry in self.ctx.bot_manager.log_entries[:200]:
            self._add_item(entry)
        self.count_label.setText(f"{len(self.ctx.bot_manager.log_entries)} записей")

    def _on_log_event(self, entry: dict) -> None:
        self._add_item(entry, prepend=True)
        if self.log_list.count() > 300:
            self.log_list.takeItem(self.log_list.count() - 1)
        self.count_label.setText(f"{len(self.ctx.bot_manager.log_entries)} записей")

    def _add_item(self, entry: dict, prepend: bool = False) -> None:
        color = {"warn": theme.WARN, "ok": theme.GOOD_FG}.get(entry.get("tone"), theme.TEXT_LOG)
        text = f"{entry['time']}   {entry['bot']}   —   {entry['text']}"
        item = QListWidgetItem(text)
        item.setForeground(QColor(color))
        if prepend:
            self.log_list.insertItem(0, item)
        else:
            self.log_list.addItem(item)
