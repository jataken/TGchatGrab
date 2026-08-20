"""П10: «Почта → Отчёты» — the main answer to the question that
motivated splitting the funnels in the first place: which channel
brings clients, which converts, and how fast the mailbox answers.
Same layout/date-range pattern as ui/screens/reports_screen.py (the
Telegram-lead version); reuses core/lead_report.conversion() for the
won/lost/in_progress math, since a channel row is exactly the same
shape a source/direction row already is.

Retention lives here too, not on «Ящики» — two independent knobs
(whole messages, attachments only), same preview-before-prune UX as
settings.py's Telegram retention card, over MailRetentionService.
"""
from __future__ import annotations

import datetime as dt

from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QHeaderView, QLineEdit, QMessageBox, QScrollArea,
    QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ...context import AppContext
from ...format import human_size
from ...widgets import button, card, h1, label, muted, plural
from ....core import lead as lead_domain
from ....core import lead_report

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
    table.setMaximumHeight(200)
    table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
    return table


def _channel_label(channel: str | None) -> str:
    if not channel:
        return "без канала"
    return lead_domain.label_for_origin_channel(channel)


class MailReportsScreen(QWidget):
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
        outer.addWidget(h1("Отчёты"))
        outer.addWidget(muted(
            "По каким каналам приходят заявки, какой конвертируется лучше, и за сколько "
            "часов мы в среднем отвечаем."))
        outer.addSpacing(14)

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

        outer.addWidget(muted("СВОДНЫЙ ОТЧЁТ ПО КАНАЛАМ"))
        self.channel_table = _report_table(
            ["Канал", "Всего", "Сделки", "Отказы", "В работе", "Конверсия, %", "Средний срок, дн."])
        outer.addWidget(self.channel_table)
        outer.addSpacing(16)

        outer.addWidget(muted("КАНАЛ × НАПРАВЛЕНИЕ"))
        self.cross_table = _report_table(["Канал", "Направление", "Заявок"])
        outer.addWidget(self.cross_table)
        outer.addSpacing(16)

        resp_row = QHBoxLayout()
        by_mailbox_card = card()
        bm_lay = QVBoxLayout(by_mailbox_card)
        bm_lay.setContentsMargins(16, 14, 16, 14)
        bm_lay.addWidget(muted("СКОРОСТЬ ОТВЕТА ПО ЯЩИКАМ"))
        self.mailbox_resp_table = _report_table(["Ящик", "Среднее, ч", "Худшее, ч", "Писем"])
        self.mailbox_resp_table.setMaximumHeight(180)
        bm_lay.addWidget(self.mailbox_resp_table)
        resp_row.addWidget(by_mailbox_card, 1)

        by_direction_card = card()
        bd_lay = QVBoxLayout(by_direction_card)
        bd_lay.setContentsMargins(16, 14, 16, 14)
        bd_lay.addWidget(muted("СКОРОСТЬ ОТВЕТА ПО НАПРАВЛЕНИЯМ"))
        self.direction_resp_table = _report_table(["Направление", "Среднее, ч", "Худшее, ч", "Писем"])
        self.direction_resp_table.setMaximumHeight(180)
        bd_lay.addWidget(self.direction_resp_table)
        resp_row.addWidget(by_direction_card, 1)
        outer.addLayout(resp_row)
        outer.addSpacing(20)

        outer.addWidget(self._build_retention_card())
        outer.addStretch(1)

        self.range_combo.setCurrentIndex(1)

    # ---- lifecycle -----------------------------------------------------
    def on_show(self, **kwargs) -> None:
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

    def _current_range(self) -> tuple[str | None, str | None]:
        return (self.date_from.text().strip() or None, self.date_to.text().strip() or None)

    def refresh(self) -> None:
        db = self.ctx.db
        date_from, date_to = self._current_range()

        by_channel = db.leads_report_by_channel(date_from, date_to)
        avg_by_channel = db.avg_days_to_win_by_channel(date_from, date_to)
        self.channel_table.setRowCount(len(by_channel))
        for row_idx, r in enumerate(by_channel):
            conv = lead_report.conversion(r["total"], r["won"], r["lost"])
            self.channel_table.setItem(row_idx, 0, QTableWidgetItem(_channel_label(r["channel"])))
            self.channel_table.setItem(row_idx, 1, QTableWidgetItem(str(conv["total"])))
            self.channel_table.setItem(row_idx, 2, QTableWidgetItem(str(conv["won"])))
            self.channel_table.setItem(row_idx, 3, QTableWidgetItem(str(conv["lost"])))
            self.channel_table.setItem(row_idx, 4, QTableWidgetItem(str(conv["in_progress"])))
            self.channel_table.setItem(row_idx, 5, QTableWidgetItem(f"{conv['conversion_pct']:.1f}"))
            avg_days = avg_by_channel.get(r["channel"] or "")
            self.channel_table.setItem(
                row_idx, 6, QTableWidgetItem(f"{avg_days:.1f}" if avg_days is not None else "—"))
        self.channel_table.resizeColumnsToContents()
        self.channel_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)

        cross = db.leads_report_by_channel_and_direction(date_from, date_to)
        self.cross_table.setRowCount(len(cross))
        for row_idx, r in enumerate(cross):
            self.cross_table.setItem(row_idx, 0, QTableWidgetItem(_channel_label(r["channel"])))
            self.cross_table.setItem(row_idx, 1, QTableWidgetItem(r["direction_name"] or "без направления"))
            self.cross_table.setItem(row_idx, 2, QTableWidgetItem(str(r["total"])))
        self.cross_table.resizeColumnsToContents()
        self.cross_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)

        by_mailbox = db.mail_response_time_by_mailbox(date_from, date_to)
        self._fill_response_table(self.mailbox_resp_table, by_mailbox, "mailbox_address")

        by_direction = db.mail_response_time_by_direction(date_from, date_to)
        self._fill_response_table(self.direction_resp_table, by_direction, "direction_name")

        self._refresh_retention_preview()
        self._refresh_attachment_retention_preview()

    @staticmethod
    def _fill_response_table(table: QTableWidget, rows, label_key: str) -> None:
        table.setRowCount(len(rows))
        for row_idx, r in enumerate(rows):
            table.setItem(row_idx, 0, QTableWidgetItem(r[label_key] or "—"))
            table.setItem(row_idx, 1, QTableWidgetItem(f"{r['avg_hours']:.1f}"))
            table.setItem(row_idx, 2, QTableWidgetItem(f"{r['worst_hours']:.1f}"))
            table.setItem(row_idx, 3, QTableWidgetItem(str(r["n"])))
        table.resizeColumnsToContents()
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)

    # ---- retention (П10) -------------------------------------------------
    def _build_retention_card(self) -> QWidget:
        c = card()
        lay = QVBoxLayout(c)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.addWidget(label("РЕТЕНШН ПОЧТЫ", "kicker"))
        hint = muted(
            "Старые письма сначала выписываются в архивный JSONL рядом с обычными "
            "выгрузками и только потом удаляются — само по себе ничего не удаляется, "
            "только по кнопке. Срок для вложений отдельный: письмо и текст остаются, "
            "уходят только присланные файлы.")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        msg_row = QHBoxLayout()
        msg_row.addWidget(muted("Хранить письма за последние"))
        self.mail_retention_spin = QSpinBox()
        self.mail_retention_spin.setRange(0, 120)
        self.mail_retention_spin.setSuffix(" мес.")
        self.mail_retention_spin.setSpecialValueText("всё время")
        self.mail_retention_spin.setValue(self.ctx.mail_retention_service.months)
        self.mail_retention_spin.valueChanged.connect(self._refresh_retention_preview)
        msg_row.addWidget(self.mail_retention_spin)
        msg_row.addStretch(1)
        lay.addLayout(msg_row)
        self.retention_preview = muted("")
        self.retention_preview.setWordWrap(True)
        lay.addWidget(self.retention_preview)
        msg_btn_row = QHBoxLayout()
        save_ret_btn = button("Сохранить", "primary")
        save_ret_btn.clicked.connect(self._save_retention)
        msg_btn_row.addWidget(save_ret_btn)
        self.prune_btn = button("Заархивировать и удалить старое", "secondary")
        self.prune_btn.clicked.connect(self._on_prune)
        msg_btn_row.addWidget(self.prune_btn)
        msg_btn_row.addStretch(1)
        lay.addLayout(msg_btn_row)

        lay.addSpacing(10)
        att_row = QHBoxLayout()
        att_row.addWidget(muted("Хранить вложения за последние"))
        self.attachment_retention_spin = QSpinBox()
        self.attachment_retention_spin.setRange(0, 120)
        self.attachment_retention_spin.setSuffix(" мес.")
        self.attachment_retention_spin.setSpecialValueText("всё время")
        self.attachment_retention_spin.setValue(self.ctx.mail_retention_service.attachment_months)
        self.attachment_retention_spin.valueChanged.connect(self._refresh_attachment_retention_preview)
        att_row.addWidget(self.attachment_retention_spin)
        att_row.addStretch(1)
        lay.addLayout(att_row)
        self.attachment_retention_preview = muted("")
        self.attachment_retention_preview.setWordWrap(True)
        lay.addWidget(self.attachment_retention_preview)
        att_btn_row = QHBoxLayout()
        save_att_btn = button("Сохранить", "primary")
        save_att_btn.clicked.connect(self._save_attachment_retention)
        att_btn_row.addWidget(save_att_btn)
        self.prune_attachments_btn = button("Удалить старые вложения", "secondary")
        self.prune_attachments_btn.clicked.connect(self._on_prune_attachments)
        att_btn_row.addWidget(self.prune_attachments_btn)
        att_btn_row.addStretch(1)
        lay.addLayout(att_btn_row)
        return c

    def _refresh_retention_preview(self) -> None:
        months = self.mail_retention_spin.value()
        if months <= 0:
            self.retention_preview.setText("Сейчас хранится всё. Ничего не удаляется и не архивируется.")
            self.prune_btn.setEnabled(False)
            return
        info = self.ctx.mail_retention_service.preview(months)
        n = info["messages"]
        if not n:
            self.retention_preview.setText(f"Старше {info['cutoff'][:10]} ничего нет — удалять нечего.")
            self.prune_btn.setEnabled(False)
            return
        self.retention_preview.setText(
            f"Старше {info['cutoff'][:10]} — {n} " + plural(n, "письмо", "письма", "писем") + ".")
        self.prune_btn.setEnabled(True)

    def _save_retention(self) -> None:
        self.ctx.mail_retention_service.set_months(self.mail_retention_spin.value())
        self._refresh_retention_preview()
        QMessageBox.information(
            self, "Сохранено",
            "Срок хранения записан. Само по себе ничего не удалится — старое уйдёт "
            "только по кнопке «Заархивировать и удалить старое».")

    def _on_prune(self) -> None:
        months = self.mail_retention_spin.value()
        if months <= 0:
            return
        info = self.ctx.mail_retention_service.preview(months)
        n = info["messages"]
        if not n:
            QMessageBox.information(self, "Нечего удалять", "Писем старше указанного срока нет.")
            return
        if QMessageBox.question(
            self, "Заархивировать и удалить",
            f"{n} " + plural(n, "письмо", "письма", "писем") + f" старше {info['cutoff'][:10]} "
            "будут выписаны в архивный файл и удалены из базы вместе со своими вложениями. Продолжить?"
        ) != QMessageBox.Yes:
            return
        self.ctx.mail_retention_service.archive_and_prune(months)
        self._refresh_retention_preview()
        self._refresh_attachment_retention_preview()

    def _refresh_attachment_retention_preview(self) -> None:
        months = self.attachment_retention_spin.value()
        if months <= 0:
            self.attachment_retention_preview.setText("Сейчас хранятся все вложения.")
            self.prune_attachments_btn.setEnabled(False)
            return
        info = self.ctx.mail_retention_service.preview_attachments(months)
        n = info["count"]
        if not n:
            self.attachment_retention_preview.setText(
                f"Вложений старше {info['cutoff'][:10]} нет — удалять нечего.")
            self.prune_attachments_btn.setEnabled(False)
            return
        self.attachment_retention_preview.setText(
            f"Старше {info['cutoff'][:10]} — {n} " + plural(n, "вложение", "вложения", "вложений")
            + f" ({human_size(info['bytes'])}).")
        self.prune_attachments_btn.setEnabled(True)

    def _save_attachment_retention(self) -> None:
        self.ctx.mail_retention_service.set_attachment_months(self.attachment_retention_spin.value())
        self._refresh_attachment_retention_preview()
        QMessageBox.information(self, "Сохранено", "Срок хранения вложений записан.")

    def _on_prune_attachments(self) -> None:
        months = self.attachment_retention_spin.value()
        if months <= 0:
            return
        info = self.ctx.mail_retention_service.preview_attachments(months)
        n = info["count"]
        if not n:
            QMessageBox.information(self, "Нечего удалять", "Вложений старше указанного срока нет.")
            return
        if QMessageBox.question(
            self, "Удалить старые вложения",
            f"{n} " + plural(n, "вложение", "вложения", "вложений") + " будет удалено с диска. "
            "Письма и их текст останутся — уйдут только присланные файлы. Продолжить?"
        ) != QMessageBox.Yes:
            return
        self.ctx.mail_retention_service.prune_attachments(months)
        self._refresh_attachment_retention_preview()
