from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QListWidget, QListWidgetItem,
    QPlainTextEdit, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ...context import AppContext
from ...widgets import KeyValue, muted


class AnalyticsTab(QWidget):
    """Contact ranking by recency+frequency, a lead-status funnel, and a
    per-contact activity history — the "кто из клиентов наиболее активен"
    requirement, plus enough drill-down to see why."""

    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 16, 0, 16)

        funnel_row = QHBoxLayout()
        self.kv_new = KeyValue("Новые заявки")
        self.kv_progress = KeyValue("В работе")
        self.kv_closed = KeyValue("Закрыты")
        for kv in (self.kv_new, self.kv_progress, self.kv_closed):
            funnel_row.addWidget(kv)
        funnel_row.addStretch(1)
        outer.addLayout(funnel_row)
        outer.addSpacing(16)

        split = QSplitter()
        outer.addWidget(split, 1)

        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 8, 0)
        left_lay.addWidget(muted("Рейтинг контактов по активности (частота + давность)"))
        self.ranking_table = QTableWidget(0, 4)
        self.ranking_table.setHorizontalHeaderLabels(["Контакт", "Балл", "Событий", "Последняя активность"])
        self.ranking_table.verticalHeader().setVisible(False)
        self.ranking_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.ranking_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.ranking_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.ranking_table.itemSelectionChanged.connect(self._on_select_contact)
        left_lay.addWidget(self.ranking_table, 1)
        split.addWidget(left)

        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(8, 0, 0, 0)
        right_lay.addWidget(muted("История выбранного контакта"))
        self.history_list = QListWidget()
        right_lay.addWidget(self.history_list, 1)
        split.addWidget(right)
        split.setSizes([560, 320])

        self._ranking: list[dict] = []
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(5000)

    def on_show(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        db = self.ctx.db
        funnel = db.leads_funnel()
        self.kv_new.set_value(str(funnel["new"]))
        self.kv_progress.set_value(str(funnel["in_progress"]))
        self.kv_closed.set_value(str(funnel["closed"]))

        self._ranking = db.contact_ranking()
        self.ranking_table.setRowCount(len(self._ranking))
        for row, r in enumerate(self._ranking):
            handle = f"@{r['username']}" if r["username"] else str(r["telegram_id"])
            item = QTableWidgetItem(handle)
            item.setData(Qt.UserRole, r["contact_id"])
            self.ranking_table.setItem(row, 0, item)
            self.ranking_table.setItem(row, 1, QTableWidgetItem(str(r["score"])))
            self.ranking_table.setItem(row, 2, QTableWidgetItem(str(r["activity_count"])))
            self.ranking_table.setItem(row, 3, QTableWidgetItem(str(r["last_active"])[:16].replace("T", " ")))
        self.ranking_table.resizeColumnsToContents()
        self.ranking_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)

    def _on_select_contact(self) -> None:
        items = self.ranking_table.selectedItems()
        self.history_list.clear()
        if not items:
            return
        contact_id = items[0].data(Qt.UserRole)
        db = self.ctx.db
        for entry in db.activity_for_contact(contact_id, limit=100):
            kind_label = {"message": "сообщение", "trigger_fired": "сработал триггер", "error": "ошибка"}.get(
                entry["kind"], entry["kind"])
            self.history_list.addItem(f"{str(entry['timestamp'])[:16].replace('T', ' ')}  ·  {kind_label}")
        leads = [l for l in db.list_leads() if l["contact_id"] == contact_id]
        if leads:
            self.history_list.addItem(f"— {len(leads)} заявок от этого контакта —")
            for lead in leads:
                self.history_list.addItem(f"  заявка #{lead['id']}: {lead['status']}")
