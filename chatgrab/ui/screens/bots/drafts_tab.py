"""Черновики — cold first messages an outbox.py proactive send held back
instead of sending (invariant 6). This is the click those messages were
waiting on: nothing here goes out until a manager picks it.

Sits under the bot list on the «Боты» screen, next to contact-ranking
analytics — another cross-bot panel, not its own navigation destination,
since drafts are rare enough that a whole screen for them would be empty
most of the time."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QHBoxLayout, QHeaderView, QMessageBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ...context import AppContext
from ...util import fire
from ...widgets import button, muted


class DraftsPanel(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.empty_label = muted(
            "Черновиков нет — они появляются, когда бот хочет написать первым "
            "тому, кому ещё не писал."
        )
        self.empty_label.setWordWrap(True)
        outer.addWidget(self.empty_label)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Бот", "Кому", "Текст", ""])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        outer.addWidget(self.table)

    def on_show(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        drafts = self.ctx.db.list_drafts()
        self.empty_label.setVisible(not drafts)
        self.table.setVisible(bool(drafts))
        self.table.setRowCount(len(drafts))
        for row, draft in enumerate(drafts):
            bot = self.ctx.db.get_bot(draft["bot_id"])
            self.table.setItem(row, 0, QTableWidgetItem(bot["name"] if bot else f"бот {draft['bot_id']}"))
            self.table.setItem(row, 1, QTableWidgetItem(draft["target"]))
            self.table.setItem(row, 2, QTableWidgetItem(draft["text"]))

            actions = QWidget()
            al = QHBoxLayout(actions)
            al.setContentsMargins(0, 0, 0, 0)
            send_btn = button("Отправить", "primary")
            send_btn.clicked.connect(lambda _c, d=draft["id"]: self._on_send(d))
            al.addWidget(send_btn)
            dismiss_btn = button("Отклонить", "ghost")
            dismiss_btn.clicked.connect(lambda _c, d=draft["id"]: self._on_dismiss(d))
            al.addWidget(dismiss_btn)
            self.table.setCellWidget(row, 3, actions)
            self.table.setRowHeight(row, 40)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setColumnWidth(3, 180)

    def _on_send(self, draft_id: int) -> None:
        def on_error(e):
            QMessageBox.warning(self, "Не получилось отправить", str(e))

        fire(self.ctx.bot_manager.send_draft(draft_id), parent=self,
             on_error=on_error, on_done=self.refresh)

    def _on_dismiss(self, draft_id: int) -> None:
        self.ctx.bot_manager.dismiss_draft(draft_id)
        self.refresh()
