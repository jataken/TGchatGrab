from __future__ import annotations

import json

from PySide6.QtCore import QUrl, Qt, QTimer
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QHBoxLayout, QHeaderView, QInputDialog, QMenu,
    QMessageBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ...context import AppContext
from ...util import fire, run_blocking
from ...widgets import button, chip, muted
from ....bots.export import export_leads_xlsx

_STATUS_LABELS = {"new": "новая", "in_progress": "в работе", "closed": "закрыта"}
_STATUS_STYLE = {
    "new": ("rgba(145,132,217,46)", "#d2cefd", "#b5abfc"),
    "in_progress": ("rgba(220,150,90,46)", "#f0c6a0", "#f0c6a0"),
    "closed": ("rgba(233,233,237,13)", "#6c6c78", "#3f424d"),
}
_NEXT_STATUS = {"new": "in_progress", "in_progress": "closed", "closed": "new"}


def _status_pill(status: str) -> QWidget:
    bg, fg, dot = _STATUS_STYLE.get(status, _STATUS_STYLE["closed"])
    host = QWidget()
    lay = QHBoxLayout(host)
    lay.setContentsMargins(8, 0, 8, 0)
    pill = muted(f"●  {_STATUS_LABELS.get(status, status)}")
    pill.setStyleSheet(
        f"color: {fg}; background: {bg}; border-radius: 6px; padding: 3px 10px; font-size: 11.5px;"
    )
    lay.addWidget(pill)
    lay.addStretch(1)
    return host


class LeadsTab(QWidget):
    """Minimal ticket-tracker: no full CRM, just enough that a manager
    doesn't lose an inbound lead.

    Status advances (новая → в работе → закрыта) on a click in the status
    column only — not anywhere on the row. This screen is the one a
    manager lives in all day, and a whole-row hit target turns every
    mis-aimed click into a silent state change on someone else's lead.
    The last change is undoable for the same reason; reassigning to a
    named manager is rarer, so it sits in the context menu."""

    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.status_filter = "all"
        # (lead_id, previous_status) of the last status change, so it can
        # be put back — cheaper for the user than a confirmation on every
        # click, and this is a reversible action.
        self._undo: tuple[int, str] | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        head = QHBoxLayout()
        self.summary_label = muted("")
        head.addWidget(self.summary_label)
        head.addStretch(1)
        self.undo_btn = button("Отменить изменение", "ghost")
        self.undo_btn.clicked.connect(self._on_undo)
        self.undo_btn.setVisible(False)
        head.addWidget(self.undo_btn)
        export_btn = button("Выгрузить в Excel", "secondary")
        export_btn.clicked.connect(self._on_export)
        head.addWidget(export_btn)
        outer.addLayout(head)
        outer.addSpacing(12)

        chip_row = QHBoxLayout()
        chip_row.setSpacing(6)
        self.chip_group = QButtonGroup(self)
        self.chip_group.setExclusive(True)
        for key, lbl in [("all", "Все"), ("new", "Новые"),
                         ("in_progress", "В работе"), ("closed", "Закрытые")]:
            btn = chip(lbl)
            btn.setChecked(key == "all")
            btn.clicked.connect(lambda _c, k=key: self._set_filter(k))
            self.chip_group.addButton(btn)
            chip_row.addWidget(btn)
        chip_row.addStretch(1)
        outer.addLayout(chip_row)
        outer.addSpacing(12)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Дата", "Контакт", "Откуда", "Содержание", "Статус"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.cellClicked.connect(self._on_cell_clicked)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        outer.addWidget(self.table, 1)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(4000)

    def on_show(self) -> None:
        self.refresh()

    def _set_filter(self, key: str) -> None:
        self.status_filter = key
        self.refresh()

    def refresh(self) -> None:
        db = self.ctx.db
        all_leads = db.list_leads()
        status = None if self.status_filter == "all" else self.status_filter
        leads = db.list_leads(status=status)

        n_new = len([l for l in all_leads if l["status"] == "new"])
        n_prog = len([l for l in all_leads if l["status"] == "in_progress"])
        self.summary_label.setText(
            f"{n_new} новых · {n_prog} в работе · {len(all_leads)} заявок всего"
            if all_leads else "заявок пока нет — они появятся, когда сработает правило бота"
        )
        self.undo_btn.setVisible(self._undo is not None)

        self.table.setRowCount(len(leads))
        for row, lead in enumerate(leads):
            date_item = QTableWidgetItem(str(lead["created_at"])[:16].replace("T", " "))
            date_item.setData(Qt.UserRole, lead["id"])
            self.table.setItem(row, 0, date_item)

            contact = db.get_contact(lead["contact_id"])
            handle = f"@{contact['username']}" if contact and contact["username"] else \
                (str(contact["telegram_id"]) if contact else "—")
            manager = lead["manager"] or "не назначена"
            self.table.setItem(row, 1, QTableWidgetItem(f"{handle}\n{manager}"))

            bot = db.get_bot(lead["bot_id"])
            self.table.setItem(row, 2, QTableWidgetItem(bot["name"] if bot else f"бот {lead['bot_id']}"))

            try:
                content = json.loads(lead["content"])
                summary = "; ".join(f"{k}: {v}" for k, v in content.items()) if content else "—"
            except (json.JSONDecodeError, TypeError):
                summary = "—"
            self.table.setItem(row, 3, QTableWidgetItem(summary))

            self.table.setCellWidget(row, 4, _status_pill(lead["status"]))
            self.table.setRowHeight(row, 44)

        self.table.resizeColumnsToContents()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        # resizeColumnsToContents measures the item text, not a cell widget,
        # so the status pills would come out clipped without an explicit
        # floor; same for the two-line contact cell.
        header.setSectionResizeMode(4, QHeaderView.Fixed)
        self.table.setColumnWidth(4, 140)
        if self.table.columnWidth(1) < 170:
            self.table.setColumnWidth(1, 170)

    def _lead_id_at(self, row: int) -> int | None:
        item = self.table.item(row, 0)
        return item.data(Qt.UserRole) if item else None

    def _on_cell_clicked(self, row: int, col: int) -> None:
        if col != 4:  # status column only — see the class docstring
            return
        lead_id = self._lead_id_at(row)
        if lead_id is None:
            return
        lead = self.ctx.db.get_lead(lead_id)
        if not lead:
            return
        self._undo = (lead_id, lead["status"])
        self.ctx.db.set_lead_field(lead_id, status=_NEXT_STATUS.get(lead["status"], "new"))
        self.refresh()

    def _on_undo(self) -> None:
        if not self._undo:
            return
        lead_id, previous = self._undo
        self._undo = None
        self.ctx.db.set_lead_field(lead_id, status=previous)
        self.refresh()

    def _on_context_menu(self, pos) -> None:
        row = self.table.rowAt(pos.y())
        lead_id = self._lead_id_at(row) if row >= 0 else None
        if lead_id is None:
            return
        menu = QMenu(self)
        reassign = menu.addAction("Переназначить менеджера…")
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen == reassign:
            self._reassign(lead_id)

    def _on_export(self) -> None:
        def on_error(e):
            QMessageBox.warning(self, "Не получилось выгрузить", str(e))

        # Off the shared qasync loop (see export_screen.py's _run_export
        # for why) — openpyxl on a large leads table would otherwise
        # freeze the UI and every running bot until it finished.
        task = fire(run_blocking(export_leads_xlsx, self.ctx.db, self.ctx.paths, None),
                    parent=self, on_error=on_error)

        def _apply(t):
            if t.cancelled() or t.exception() is not None:
                return
            path = t.result()
            msg = QMessageBox(self)
            msg.setWindowTitle("Готово")
            msg.setText(f"Заявки выгружены в {path.name}")
            open_btn = msg.addButton("Открыть папку", QMessageBox.ActionRole)
            msg.addButton(QMessageBox.Ok)
            msg.exec()
            if msg.clickedButton() == open_btn:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))

        task.add_done_callback(_apply)

    def _reassign(self, lead_id: int) -> None:
        current = self.ctx.db.get_lead(lead_id)
        name, ok = QInputDialog.getText(self, "Переназначить заявку", "Имя менеджера:",
                                         text=current["manager"] or "")
        if ok:
            self.ctx.db.set_lead_field(lead_id, manager=name.strip(), status="in_progress")
            self.refresh()
