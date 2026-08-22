"""П9: «Почта → Заявки» — свой список, свои фильтры, своя воронка сверху.
Телеграмные заявки сюда не попадают и наоборот (П-2), потому что этот
экран, в отличие от leads_tab.py, всегда фильтрует по конкретному
funnel_id (почтовой воронке из migration 020), а не читает все заявки
подряд — тот же приём, что и leads_tab.py, только развёрнутый в другую
сторону: там воронка одна на весь экран по умолчанию, здесь — жёстко
одна и только эта.

Планировка и приёмы (таблица-карточка §4.3, чипы-фильтры по статусу, клик
по колонке статуса = один шаг воронки) намеренно скопированы с
leads_tab.py (включая геометрию его Д7-переверстки — `_ChatRow`-стиль
строк-виджетов), а не вынесены в общий модуль — два экрана расходятся
ровно настолько, чтобы общая база всё ещё была бы лишним слоем ради
содержимого в одну функцию.
"""
from __future__ import annotations

import datetime as dt
import json

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QFrame, QHBoxLayout, QScrollArea,
    QVBoxLayout, QWidget,
)

from ... import theme
from ...context import AppContext
from ...format import short_dt
from ...widgets import LeadStatusPill, chip, h1, label, muted
from ....core import lead as lead_domain
from ..bots.lead_card import LeadCardDialog

_DATE_RANGES = [
    ("all", "Всё время", None),
    ("today", "Сегодня", 0),
    ("7d", "7 дней", 7),
    ("30d", "30 дней", 30),
]

_COL_DATE = 100
_COL_CONTACT = 190
_COL_SUBJECT = 160
_COL_STATUS = 150
_FALLBACK_DOT = "rgba(140,140,150,140)"


class _StatusCell(QWidget):
    """Тот же клик-квант продвижения по воронке, что и в leads_tab.py's
    (Д7) одноимённом классе."""

    clicked = Signal()

    def __init__(self, stage):
        super().__init__()
        self.setFixedWidth(_COL_STATUS)
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setAlignment(Qt.AlignCenter)
        self.pill = LeadStatusPill(stage, font_size="11.5px")
        lay.addWidget(self.pill)

    def set_stage(self, stage) -> None:
        self.pill.set_stage(stage)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.clicked.emit()
        event.accept()


class _MailLeadRow(QFrame):
    """Строка списка (design-brief.md §4.3, та же геометрия, что у
    `_LeadRow` в bots/leads_tab.py, Д7): 2px левая полоса цветом этапа
    воронки, дата/контакт/тема/содержание/статус. Клик по статусу
    продвигает этап, двойной клик открывает карточку."""

    doubleClicked = Signal()
    statusClicked = Signal()

    def __init__(self, lead_id: int):
        super().__init__()
        self.lead_id = lead_id
        self.setProperty("class", "tablerow")
        self.setCursor(Qt.PointingHandCursor)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 11, 16, 11)
        lay.setSpacing(14)

        self.stripe = QWidget()
        self.stripe.setFixedWidth(2)
        lay.addWidget(self.stripe)
        lay.addSpacing(18)

        self.date_label = label("")
        self.date_label.setFixedWidth(_COL_DATE)
        self.date_label.setStyleSheet(
            f"font-family: {theme.FONT_MONO}; font-size: 11px; color: {theme.TEXT_MUTED};"
        )
        lay.addWidget(self.date_label)

        contact_col = QVBoxLayout()
        contact_col.setSpacing(2)
        self.name_label = label("")
        self.name_label.setStyleSheet("font-size: 13px;")
        contact_col.addWidget(self.name_label)
        self.manager_label = label("")
        self.manager_label.setStyleSheet(f"font-size: 10.5px; color: {theme.TEXT_FAINT};")
        contact_col.addWidget(self.manager_label)
        contact_wrap = QWidget()
        contact_wrap.setFixedWidth(_COL_CONTACT)
        contact_wrap.setLayout(contact_col)
        lay.addWidget(contact_wrap)

        self.subject_label = label("")
        self.subject_label.setFixedWidth(_COL_SUBJECT)
        self.subject_label.setStyleSheet(f"font-size: 12px; color: {theme.TEXT_MUTED};")
        lay.addWidget(self.subject_label)

        self.content_label = label("")
        self.content_label.setWordWrap(False)
        lay.addWidget(self.content_label, 1)

        self.status_cell = _StatusCell(None)
        self.status_cell.clicked.connect(self.statusClicked)
        lay.addWidget(self.status_cell)

    def set_data(self, date_text: str, name_text: str, manager_text: str,
                 subject_text: str, content_text: str, stage) -> None:
        self.date_label.setText(date_text)
        self.name_label.setText(name_text)
        self.manager_label.setText(manager_text)
        self.subject_label.setText(subject_text)
        self.content_label.setText(content_text)
        self.status_cell.set_stage(stage)
        dot = stage["color_dot"] if stage is not None else _FALLBACK_DOT
        self.stripe.setStyleSheet(f"background: {dot}; border-radius: 1px;")

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.doubleClicked.emit()


class _MailLeadTableHeader(QWidget):
    """Кикеры над списком строк — те же колонки, что и у `_MailLeadRow`."""

    def __init__(self):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(20, 9, 16, 9)
        lay.setSpacing(14)
        lay.addSpacing(2 + 18)

        def kicker(text: str):
            return label(text, "kicker")

        date_k = kicker("ДАТА")
        date_k.setFixedWidth(_COL_DATE)
        lay.addWidget(date_k)
        contact_k = kicker("КОНТАКТ")
        contact_k.setFixedWidth(_COL_CONTACT)
        lay.addWidget(contact_k)
        subject_k = kicker("ТЕМА")
        subject_k.setFixedWidth(_COL_SUBJECT)
        lay.addWidget(subject_k)
        content_k = kicker("СОДЕРЖАНИЕ")
        lay.addWidget(content_k, 1)
        status_k = kicker("СТАТУС")
        status_k.setFixedWidth(_COL_STATUS)
        status_k.setAlignment(Qt.AlignCenter)
        lay.addWidget(status_k)
        self.setStyleSheet(f"QWidget {{ border-bottom: 1px solid {theme.DIVIDER}; }}")


class MailLeadsScreen(QWidget):
    def __init__(self, ctx: AppContext, navigate):
        super().__init__()
        self.ctx = ctx
        self.navigate = navigate
        self.status_filter = "all"
        self._funnel_id: int | None = None
        self._stages: list = []
        self.rows: dict[int, _MailLeadRow] = {}

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

        # "Таблица становится карточкой" (design-brief.md §4.3), та же
        # геометрия, что у bots/leads_tab.py (Д7).
        table_card = QFrame()
        table_card.setProperty("class", "card")
        table_lay = QVBoxLayout(table_card)
        table_lay.setContentsMargins(0, 0, 0, 0)
        table_lay.setSpacing(0)
        table_lay.addWidget(_MailLeadTableHeader())

        self.rows_scroll = QScrollArea()
        self.rows_scroll.setWidgetResizable(True)
        self.rows_scroll.setFrameShape(QFrame.NoFrame)
        rows_host = QWidget()
        self.rows_lay = QVBoxLayout(rows_host)
        self.rows_lay.setContentsMargins(0, 0, 0, 0)
        self.rows_lay.setSpacing(0)
        self.rows_scroll.setWidget(rows_host)
        table_lay.addWidget(self.rows_scroll, 1)
        outer.addWidget(table_card, 1)

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
            self._clear_rows()
            return

        all_leads = db.list_leads(funnel_id=funnel_id)
        status = None if self.status_filter == "all" else self.status_filter
        leads = db.list_leads(
            funnel_id=funnel_id, status=status,
            direction_id=self.direction_combo.currentData(), since=self._since(),
        )
        counts = db.leads_status_counts(funnel_id=funnel_id)
        for key, btn in self.status_chips.items():
            chip_label = "Все" if key == "all" else lead_domain.label_for_stage(self._stages, key)
            n = len(all_leads) if key == "all" else counts.get(key, 0)
            btn.setText(f"{chip_label} ({n})" if n else chip_label)

        n_new = len([l for l in all_leads if lead_domain.bucket_for_stage(self._stages, l["status"]) == "new"])
        n_active = len(
            [l for l in all_leads if lead_domain.bucket_for_stage(self._stages, l["status"]) == "in_progress"])
        self.summary_label.setText(
            f"{n_new} новых · {n_active} в работе · {len(all_leads)} заявок всего"
            if all_leads else "заявок из почты пока нет"
        )

        seen = set()
        for lead in leads:
            lead_id = lead["id"]
            seen.add(lead_id)

            handle = lead["display_name"] or lead["email"] or f"заявка №{lead['id']}"
            manager_text = lead["manager"] or "не назначена"
            subject_text = lead["product"] or "—"

            try:
                content = json.loads(lead["content"])
                summary = "; ".join(f"{k}: {v}" for k, v in content.items()) if content else "—"
            except (json.JSONDecodeError, TypeError):
                summary = "—"

            stage = lead_domain.stage_for_code(self._stages, lead["status"])

            row = self.rows.get(lead_id)
            if row is None:
                row = _MailLeadRow(lead_id)
                row.doubleClicked.connect(lambda lid=lead_id: self._open_card(lid))
                row.statusClicked.connect(lambda lid=lead_id: self._quick_advance(lid))
                self.rows[lead_id] = row
            row.set_data(short_dt(lead["created_at"]), handle, manager_text, subject_text, summary, stage)

        self._prune_and_reflow(seen, leads)

    def _clear_rows(self) -> None:
        self._prune_and_reflow(set(), [])

    def _prune_and_reflow(self, seen: set[int], leads: list) -> None:
        for lead_id in list(self.rows):
            if lead_id not in seen:
                gone = self.rows.pop(lead_id)
                gone.setParent(None)
                gone.deleteLater()

        # Detach every remaining row from the layout (widgets survive —
        # just get reparented out) so the current filtered/sorted order
        # can be rebuilt cleanly each tick — same technique as Д6's bot
        # grid and Д7's leads_tab.py.
        while self.rows_lay.count():
            item = self.rows_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
        for lead in leads:
            self.rows_lay.addWidget(self.rows[lead["id"]])
        self.rows_lay.addStretch(1)

    def _quick_advance(self, lead_id: int) -> None:
        lead = self.ctx.db.get_lead(lead_id)
        if not lead:
            return
        stages = self.ctx.db.list_funnel_stages(lead["funnel_id"]) if lead["funnel_id"] else []
        self.ctx.db.set_lead_status(lead_id, lead_domain.next_stage(stages, lead["status"]),
                                    source=lead_domain.EVENT_SOURCE_MANUAL)
        self.refresh()

    def _open_card(self, lead_id: int) -> None:
        LeadCardDialog(self.ctx, lead_id, parent=self).exec()
        self.refresh()
