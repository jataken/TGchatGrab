from __future__ import annotations

import json

from PySide6.QtCore import QUrl, Qt, QTimer
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLineEdit, QMessageBox,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ...context import AppContext
from ...widgets import button, muted
from ....bots.export import export_leads_xlsx

_STATUS_LABELS = {"new": "новая", "in_progress": "в работе", "closed": "закрыта"}


class LeadsTab(QWidget):
    """Minimal ticket-tracker: no full CRM, just enough that a manager
    doesn't lose an inbound lead — status, reassignment, and a close
    button, matching the spec's "чтобы менеджер не терял входящие"."""

    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 16, 0, 16)

        filter_row = QHBoxLayout()
        filter_row.addWidget(muted("Бот"))
        self.bot_filter = QComboBox()
        self.bot_filter.addItem("Все боты", None)
        self.bot_filter.currentIndexChanged.connect(self.refresh)
        filter_row.addWidget(self.bot_filter)
        filter_row.addWidget(muted("Статус"))
        self.status_filter = QComboBox()
        self.status_filter.addItem("Все", None)
        for key, label in _STATUS_LABELS.items():
            self.status_filter.addItem(label, key)
        self.status_filter.currentIndexChanged.connect(self.refresh)
        filter_row.addWidget(self.status_filter)
        filter_row.addStretch(1)
        self.summary_label = muted("")
        filter_row.addWidget(self.summary_label)
        export_btn = button("Выгрузить в Excel", "secondary")
        export_btn.clicked.connect(self._on_export)
        filter_row.addWidget(export_btn)
        outer.addLayout(filter_row)
        outer.addSpacing(10)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Контакт", "Бот", "Создана", "Содержание", "Менеджер", "Статус"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.setShowGrid(False)
        outer.addWidget(self.table, 1)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(4000)

    def on_show(self) -> None:
        self._populate_bot_filter()
        self.refresh()

    def _populate_bot_filter(self) -> None:
        current = self.bot_filter.currentData()
        self.bot_filter.blockSignals(True)
        self.bot_filter.clear()
        self.bot_filter.addItem("Все боты", None)
        for bot in self.ctx.db.list_bots():
            self.bot_filter.addItem(bot["name"], bot["id"])
        idx = self.bot_filter.findData(current)
        self.bot_filter.setCurrentIndex(idx if idx >= 0 else 0)
        self.bot_filter.blockSignals(False)

    def refresh(self) -> None:
        db = self.ctx.db
        bot_id = self.bot_filter.currentData()
        status = self.status_filter.currentData()
        leads = db.list_leads(bot_id=bot_id, status=status)
        self.summary_label.setText(f"{len(leads)} заявок")

        self.table.setRowCount(len(leads))
        for row, lead in enumerate(leads):
            contact = db.get_contact(lead["contact_id"])
            handle = f"@{contact['username']}" if contact and contact["username"] else \
                (str(contact["telegram_id"]) if contact else "—")
            self.table.setItem(row, 0, QTableWidgetItem(handle))

            bot = db.get_bot(lead["bot_id"])
            self.table.setItem(row, 1, QTableWidgetItem(bot["name"] if bot else f"бот {lead['bot_id']}"))

            self.table.setItem(row, 2, QTableWidgetItem(str(lead["created_at"])[:16].replace("T", " ")))

            try:
                content = json.loads(lead["content"])
                summary = "; ".join(f"{k}: {v}" for k, v in content.items()) if content else "—"
            except (json.JSONDecodeError, TypeError):
                summary = "—"
            self.table.setItem(row, 3, QTableWidgetItem(summary))

            manager_item = QTableWidgetItem(lead["manager"] or "")
            manager_item.setData(Qt.UserRole, lead["id"])
            self.table.setItem(row, 4, manager_item)

            actions = QWidget()
            a_lay = QHBoxLayout(actions)
            a_lay.setContentsMargins(4, 2, 4, 2)
            status_combo = QComboBox()
            for key, label in _STATUS_LABELS.items():
                status_combo.addItem(label, key)
            idx = status_combo.findData(lead["status"])
            status_combo.setCurrentIndex(idx if idx >= 0 else 0)
            status_combo.currentIndexChanged.connect(
                lambda _, lid=lead["id"], combo=status_combo: self._set_status(lid, combo.currentData())
            )
            a_lay.addWidget(status_combo)
            reassign_btn = button("Переназначить", "ghost")
            reassign_btn.clicked.connect(lambda _, lid=lead["id"]: self._reassign(lid))
            a_lay.addWidget(reassign_btn)
            self.table.setCellWidget(row, 5, actions)
            self.table.setRowHeight(row, 40)

        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)

    def _set_status(self, lead_id: int, status: str) -> None:
        self.ctx.db.set_lead_field(lead_id, status=status)

    def _on_export(self) -> None:
        bot_id = self.bot_filter.currentData()
        try:
            path = export_leads_xlsx(self.ctx.db, self.ctx.paths, bot_id=bot_id)
        except Exception as e:
            QMessageBox.warning(self, "Не получилось выгрузить", str(e))
            return
        msg = QMessageBox(self)
        msg.setWindowTitle("Готово")
        msg.setText(f"Заявки выгружены в {path.name}")
        open_btn = msg.addButton("Открыть папку", QMessageBox.ActionRole)
        msg.addButton(QMessageBox.Ok)
        msg.exec()
        if msg.clickedButton() == open_btn:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))

    def _reassign(self, lead_id: int) -> None:
        from PySide6.QtWidgets import QInputDialog
        current = self.ctx.db.get_lead(lead_id)
        name, ok = QInputDialog.getText(self, "Переназначить заявку", "Имя менеджера:",
                                         text=current["manager"] or "")
        if ok:
            self.ctx.db.set_lead_field(lead_id, manager=name.strip(), status="in_progress")
            self.refresh()
