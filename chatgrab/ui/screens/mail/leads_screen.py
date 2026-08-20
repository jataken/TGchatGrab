"""П9: «Почта → Заявки» — свой список, свои фильтры, своя воронка сверху.
Телеграмные заявки сюда не попадают и наоборот (П-2), потому что этот
экран, в отличие от leads_tab.py, всегда фильтрует по конкретному
funnel_id (почтовой воронке из migration 020), а не читает все заявки
подряд — тот же приём, что и leads_tab.py, только развёрнутый в другую
сторону: там воронка одна на весь экран по умолчанию, здесь — жёстко
одна и только эта.

Планировка и приёмы (таблица, чипы-фильтры по статусу, клик по колонке
статуса = один шаг воронки) намеренно скопированы с leads_tab.py, а не
вынесены в общий модуль — два экрана расходятся ровно настолько, чтобы
общая база всё ещё была бы лишним слоем ради содержимого в одну функцию.
"""
from __future__ import annotations

import datetime as dt
import json

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QComboBox, QHBoxLayout, QHeaderView,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ...context import AppContext
from ...format import short_dt
from ...widgets import LeadStatusPill, chip, h1, muted
from ....core import lead as lead_domain
from ..bots.lead_card import LeadCardDialog

_DATE_RANGES = [
    ("all", "Всё время", None),
    ("today", "Сегодня", 0),
    ("7d", "7 дней", 7),
    ("30d", "30 дней", 30),
]


def _status_pill(stage) -> QWidget:
    host = QWidget()
    lay = QHBoxLayout(host)
    lay.setContentsMargins(8, 0, 8, 0)
    lay.addWidget(LeadStatusPill(stage, font_size="11.5px"))
    lay.addStretch(1)
    return host


class MailLeadsScreen(QWidget):
    def __init__(self, ctx: AppContext, navigate):
        super().__init__()
        self.ctx = ctx
        self.navigate = navigate
        self.status_filter = "all"
        self._funnel_id: int | None = None
        self._stages: list = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 28, 40, 32)

        outer.addWidget(h1("Заявки из почты"))
        self.summary_label = muted("")
        outer.addWidget(self.summary_label)
        outer.addSpacing(14)

        chip_row = QHBoxLayout()
        chip_row.setSpacing(6)
        self.chip_group = QButtonGroup(self)
        self.chip_group.setExclusive(True)
        self.status_chips: dict[str, object] = {}
        self._chip_row = chip_row
        outer.addLayout(chip_row)
        outer.addSpacing(8)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        filter_row.addWidget(muted("Направление"))
        self.direction_combo = QComboBox()
        self.direction_combo.currentIndexChanged.connect(lambda _i: self.refresh())
        filter_row.addWidget(self.direction_combo)
        filter_row.addWidget(muted("За период"))
        self.date_combo = QComboBox()
        for key, lbl, _days in _DATE_RANGES:
            self.date_combo.addItem(lbl, key)
        self.date_combo.currentIndexChanged.connect(lambda _i: self.refresh())
        filter_row.addWidget(self.date_combo)
        filter_row.addStretch(1)
        outer.addLayout(filter_row)
        outer.addSpacing(12)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Дата", "Контакт", "Тема", "Содержание", "Статус"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.cellClicked.connect(self._on_cell_clicked)
        self.table.cellDoubleClicked.connect(self._on_open_card)
        outer.addWidget(self.table, 1)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(4000)

    def on_show(self, **kwargs) -> None:
        self._populate_directions()
        self._populate_chips()
        self.refresh()

    def _mail_funnel(self):
        return self.ctx.db.get_funnel_by_channel(lead_domain.ORIGIN_CHANNEL_EMAIL)

    def _populate_directions(self) -> None:
        current = self.direction_combo.currentData()
        self.direction_combo.blockSignals(True)
        self.direction_combo.clear()
        self.direction_combo.addItem("Любое направление", None)
        for direction in self.ctx.db.list_directions():
            self.direction_combo.addItem(direction["name"], direction["id"])
        idx = self.direction_combo.findData(current)
        self.direction_combo.setCurrentIndex(max(0, idx))
        self.direction_combo.blockSignals(False)

    def _populate_chips(self) -> None:
        funnel = self._mail_funnel()
        self._funnel_id = funnel["id"] if funnel else None
        self._stages = self.ctx.db.list_funnel_stages(self._funnel_id) if self._funnel_id else []

        while self._chip_row.count():
            item = self._chip_row.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self.status_chips.clear()
        keys = ["all"] + [s["code"] for s in self._stages]
        if self.status_filter not in keys:
            self.status_filter = "all"
        for key in keys:
            btn = chip(key if key == "all" else lead_domain.label_for_stage(self._stages, key))
            btn.setChecked(key == self.status_filter)
            btn.clicked.connect(lambda _c, k=key: self._set_filter(k))
            self.chip_group.addButton(btn)
            self._chip_row.addWidget(btn)
            self.status_chips[key] = btn
        self._chip_row.addStretch(1)

    def _set_filter(self, key: str) -> None:
        self.status_filter = key
        self.refresh()

    def _since(self) -> str | None:
        key = self.date_combo.currentData()
        days = next((d for k, _l, d in _DATE_RANGES if k == key), None)
        if days is None:
            return None
        start = dt.datetime.now() - dt.timedelta(days=days)
        start = start.replace(hour=0, minute=0, second=0, microsecond=0) if days == 0 else start
        return start.astimezone().isoformat(timespec="seconds")

    def refresh(self) -> None:
        db = self.ctx.db
        if self._funnel_id is None:
            self._populate_chips()
        funnel_id = self._funnel_id
        if funnel_id is None:
            # Миграция 020 всегда сеет почтовую воронку — сюда попадаем
            # только на базе, где её кто-то удалил вручную из
            # «Воронки» (С10), что UI не мешает сделать.
            self.summary_label.setText("Почтовая воронка не найдена — создайте её на экране «Воронки».")
            self.table.setRowCount(0)
            return

        all_leads = db.list_leads(funnel_id=funnel_id)
        status = None if self.status_filter == "all" else self.status_filter
        leads = db.list_leads(
            funnel_id=funnel_id, status=status,
            direction_id=self.direction_combo.currentData(), since=self._since(),
        )
        counts = db.leads_status_counts(funnel_id=funnel_id)
        for key, btn in self.status_chips.items():
            label = "Все" if key == "all" else lead_domain.label_for_stage(self._stages, key)
            n = len(all_leads) if key == "all" else counts.get(key, 0)
            btn.setText(f"{label} ({n})" if n else label)

        n_new = len([l for l in all_leads if lead_domain.bucket_for_stage(self._stages, l["status"]) == "new"])
        n_active = len(
            [l for l in all_leads if lead_domain.bucket_for_stage(self._stages, l["status"]) == "in_progress"])
        self.summary_label.setText(
            f"{n_new} новых · {n_active} в работе · {len(all_leads)} заявок всего"
            if all_leads else "заявок из почты пока нет"
        )

        self.table.setRowCount(len(leads))
        for row, lead in enumerate(leads):
            date_item = QTableWidgetItem(short_dt(lead["created_at"]))
            date_item.setData(Qt.UserRole, lead["id"])
            self.table.setItem(row, 0, date_item)

            handle = lead["display_name"] or lead["email"] or f"заявка №{lead['id']}"
            manager = lead["manager"] or "не назначена"
            self.table.setItem(row, 1, QTableWidgetItem(f"{handle}\n{manager}"))

            self.table.setItem(row, 2, QTableWidgetItem(lead["product"] or "—"))

            try:
                content = json.loads(lead["content"])
                summary = "; ".join(f"{k}: {v}" for k, v in content.items()) if content else "—"
            except (json.JSONDecodeError, TypeError):
                summary = "—"
            self.table.setItem(row, 3, QTableWidgetItem(summary))

            stage = lead_domain.stage_for_code(self._stages, lead["status"])
            self.table.setCellWidget(row, 4, _status_pill(stage))
            self.table.setRowHeight(row, 44)

        self.table.resizeColumnsToContents()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        header.setSectionResizeMode(4, QHeaderView.Fixed)
        self.table.setColumnWidth(4, 140)
        if self.table.columnWidth(1) < 170:
            self.table.setColumnWidth(1, 170)

    def _lead_id_at(self, row: int) -> int | None:
        item = self.table.item(row, 0)
        return item.data(Qt.UserRole) if item else None

    def _on_cell_clicked(self, row: int, col: int) -> None:
        if col != 4:
            return
        lead_id = self._lead_id_at(row)
        if lead_id is None:
            return
        lead = self.ctx.db.get_lead(lead_id)
        if not lead:
            return
        stages = self.ctx.db.list_funnel_stages(lead["funnel_id"]) if lead["funnel_id"] else []
        self.ctx.db.set_lead_status(lead_id, lead_domain.next_stage(stages, lead["status"]),
                                    source=lead_domain.EVENT_SOURCE_MANUAL)
        self.refresh()

    def _on_open_card(self, row: int, _col: int) -> None:
        lead_id = self._lead_id_at(row)
        if lead_id is None:
            return
        LeadCardDialog(self.ctx, lead_id, parent=self).exec()
        self.refresh()
