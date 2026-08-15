"""С8: конверсия по источникам/направлениям, срок до КП, причины отказов —
всё посчитано по `lead_events`/`bot_leads`, ничего не кэшируется отдельно.
Экспорт в Excel идёт через тот же `ExportParams`/пресеты/расписание, что и
обычная выгрузка сообщений (см. export_service.py, ExportParams.kind).
"""
from __future__ import annotations

import datetime as dt

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QHeaderView, QInputDialog, QLineEdit, QMessageBox,
    QScrollArea, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..context import AppContext
from ..util import fire, run_blocking
from ..widgets import button, card, h1, muted
from ...core import lead_report
from ...services.export_service import ExportParams

_DATE_RANGES = [
    ("all", "Всё время", None),
    ("30d", "30 дней", 30),
    ("90d", "90 дней", 90),
    ("365d", "365 дней", 365),
]


def _report_table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QTableWidget.NoEditTriggers)
    table.setShowGrid(False)
    table.setMaximumHeight(220)
    table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
    return table


class ReportsScreen(QWidget):
    def __init__(self, ctx: AppContext, navigate):
        super().__init__()
        self.ctx = ctx
        self.navigate = navigate

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        scroll.setWidget(container)
        wrap = QVBoxLayout(self)
        wrap.setContentsMargins(0, 0, 0, 0)
        wrap.addWidget(scroll)

        outer = QVBoxLayout(container)
        outer.setContentsMargins(40, 28, 40, 32)
        outer.addWidget(h1("Отчёты по воронке и источникам"))
        outer.addWidget(muted(
            "Считается по заявкам, созданным за выбранный период, и их истории — "
            "какой чат и какое направление приносят сделки, и почему отказывают."
        ))
        outer.addSpacing(14)

        # ---- period ------------------------------------------------------
        period_row = QHBoxLayout()
        period_row.addWidget(muted("За период"))
        self.range_combo = QComboBox()
        for key, label_, _days in _DATE_RANGES:
            self.range_combo.addItem(label_, key)
        self.range_combo.currentIndexChanged.connect(self._on_range_changed)
        period_row.addWidget(self.range_combo)
        period_row.addWidget(muted("с"))
        self.date_from = QLineEdit()
        self.date_from.setPlaceholderText("ГГГГ-ММ-ДД")
        self.date_from.setMaximumWidth(120)
        period_row.addWidget(self.date_from)
        period_row.addWidget(muted("по"))
        self.date_to = QLineEdit()
        self.date_to.setPlaceholderText("ГГГГ-ММ-ДД")
        self.date_to.setMaximumWidth(120)
        period_row.addWidget(self.date_to)
        refresh_btn = button("Обновить", "secondary")
        refresh_btn.clicked.connect(self.refresh)
        period_row.addWidget(refresh_btn)
        period_row.addStretch(1)
        outer.addLayout(period_row)
        outer.addSpacing(16)

        # ---- sources -----------------------------------------------------
        outer.addWidget(muted("КОНВЕРСИЯ ПО ИСТОЧНИКАМ — КАКОЙ ЧАТ ДАЛ СКОЛЬКО ЗАЯВОК"))
        self.source_table = _report_table(
            ["Чат", "Всего", "Сделки", "Отказы", "В работе", "Конверсия, %"])
        outer.addWidget(self.source_table)
        outer.addSpacing(16)

        # ---- directions --------------------------------------------------
        outer.addWidget(muted("КОНВЕРСИЯ ПО НАПРАВЛЕНИЯМ"))
        self.direction_table = _report_table(
            ["Направление", "Всего", "Сделки", "Отказы", "В работе", "Конверсия, %"])
        outer.addWidget(self.direction_table)
        outer.addSpacing(16)

        # ---- avg time to quote + reject reasons ----------------------------
        mid_row = QHBoxLayout()

        avg_card = card()
        avg_lay = QVBoxLayout(avg_card)
        avg_lay.setContentsMargins(16, 14, 16, 14)
        avg_lay.addWidget(muted("СРЕДНИЙ СРОК ОТ ПЕРВОГО КАСАНИЯ ДО КП"))
        self.avg_days_label = muted("—")
        self.avg_days_label.setStyleSheet("font-size: 22px; color: #e9e9ed;")
        avg_lay.addWidget(self.avg_days_label)
        mid_row.addWidget(avg_card, 1)

        reasons_card = card()
        reasons_lay = QVBoxLayout(reasons_card)
        reasons_lay.setContentsMargins(16, 14, 16, 14)
        reasons_lay.addWidget(muted("ПРИЧИНЫ ОТКАЗОВ"))
        self.reasons_table = _report_table(["Причина", "Количество"])
        self.reasons_table.setMaximumHeight(180)
        reasons_lay.addWidget(self.reasons_table)
        mid_row.addWidget(reasons_card, 2)

        outer.addLayout(mid_row)
        outer.addSpacing(20)

        # ---- export --------------------------------------------------------
        export_row = QHBoxLayout()
        export_btn = button("Экспортировать в Excel", "primary")
        export_btn.clicked.connect(self._on_export)
        export_row.addWidget(export_btn)
        save_preset_btn = button("Сохранить как пресет для расписания", "secondary")
        save_preset_btn.clicked.connect(self._on_save_preset)
        export_row.addWidget(save_preset_btn)
        self.export_status = muted("")
        export_row.addWidget(self.export_status)
        export_row.addStretch(1)
        outer.addLayout(export_row)
        export_hint = muted(
            "Сохранённый пресет запускается по расписанию на экране «Настройки», "
            "в том же разделе «Выгрузка по расписанию», что и обычные выгрузки сообщений."
        )
        export_hint.setWordWrap(True)
        outer.addWidget(export_hint)

        self.range_combo.setCurrentIndex(1)  # 30 дней — разумное значение по умолчанию

    # ---- lifecycle -----------------------------------------------------
    def on_show(self) -> None:
        self.refresh()

    def _on_range_changed(self, _index: int) -> None:
        key = self.range_combo.currentData()
        days = next((d for k, _l, d in _DATE_RANGES if k == key), None)
        if days is None:
            self.date_from.setText("")
        else:
            start = dt.datetime.now().astimezone() - dt.timedelta(days=days)
            self.date_from.setText(start.date().isoformat())
        self.date_to.setText("")
        self.refresh()

    # ---- data ------------------------------------------------------------
    def _current_range(self) -> tuple[str | None, str | None]:
        return (self.date_from.text().strip() or None, self.date_to.text().strip() or None)

    def build_params(self) -> ExportParams:
        date_from, date_to = self._current_range()
        return ExportParams(
            chat_ids=[], kind="leads_report", date_from=date_from, date_to=date_to,
            folder=str(self.ctx.paths.exports_dir),
        )

    def refresh(self) -> None:
        db = self.ctx.db
        date_from, date_to = self._current_range()

        by_source = db.leads_report_by_source(date_from, date_to)
        self._fill_conversion_table(self.source_table, by_source, "chat_title", "без чата / от бота")

        by_direction = db.leads_report_by_direction(date_from, date_to)
        self._fill_conversion_table(self.direction_table, by_direction, "direction_name", "без направления")

        avg_days = db.avg_days_to_quote(date_from, date_to)
        self.avg_days_label.setText(f"{avg_days:.1f} дн." if avg_days is not None else "нет данных")

        reasons = db.reject_reasons_report(date_from, date_to)
        self.reasons_table.setRowCount(len(reasons))
        for row, r in enumerate(reasons):
            self.reasons_table.setItem(row, 0, QTableWidgetItem(r["reject_reason"] or "не указана"))
            self.reasons_table.setItem(row, 1, QTableWidgetItem(str(r["c"])))
        self.reasons_table.resizeColumnsToContents()
        self.reasons_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)

    @staticmethod
    def _fill_conversion_table(table: QTableWidget, rows: list, label_key: str, empty_label: str) -> None:
        table.setRowCount(len(rows))
        for row_idx, r in enumerate(rows):
            conv = lead_report.conversion(r["total"], r["won"], r["lost"])
            label = r[label_key] or empty_label
            table.setItem(row_idx, 0, QTableWidgetItem(label))
            table.setItem(row_idx, 1, QTableWidgetItem(str(conv["total"])))
            table.setItem(row_idx, 2, QTableWidgetItem(str(conv["won"])))
            table.setItem(row_idx, 3, QTableWidgetItem(str(conv["lost"])))
            table.setItem(row_idx, 4, QTableWidgetItem(str(conv["in_progress"])))
            table.setItem(row_idx, 5, QTableWidgetItem(f"{conv['conversion_pct']:.1f}"))
        table.resizeColumnsToContents()
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)

    # ---- export --------------------------------------------------------
    def _on_export(self) -> None:
        params = self.build_params()

        def on_error(e):
            QMessageBox.warning(self, "Не получилось", str(e))

        # Off the shared qasync loop — same reasoning as export_screen.py's
        # own _run_export: openpyxl writing several sheets shouldn't freeze
        # the UI or any running bot.
        task = fire(run_blocking(self.ctx.export_service.run, params), parent=self, on_error=on_error)

        def _apply(t):
            if t.cancelled() or t.exception() is not None:
                return
            result = t.result()
            path = result.output_paths[0] if result.output_paths else ""
            self.export_status.setText(f"Готово: {path}")
            if path:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.ctx.paths.exports_dir)))

        task.add_done_callback(_apply)

    def _on_save_preset(self) -> None:
        name, ok = QInputDialog.getText(self, "Сохранить пресет", "Имя пресета:")
        if ok and name.strip():
            params = self.build_params()
            params.preset_name = name.strip()
            self.ctx.export_service.save_preset(name.strip(), params)
            QMessageBox.information(
                self, "Готово",
                f"Пресет «{name.strip()}» сохранён — теперь его можно поставить на расписание "
                "в Настройках, в разделе «Выгрузка по расписанию»."
            )
