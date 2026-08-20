from __future__ import annotations

import datetime as dt
import json

from PySide6.QtCore import QUrl, Qt, QTimer
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QComboBox, QHBoxLayout, QHeaderView, QInputDialog, QMenu,
    QMessageBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ...context import AppContext
from ...format import short_dt
from ...util import fire, run_blocking
from ...widgets import LeadStatusPill, button, chip, muted
from ....bots.export import export_leads_xlsx
from ....core import lead as lead_domain
from .lead_card import LeadCardDialog
from .lead_create_dialog import NewLeadDialog

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


class LeadsTab(QWidget):
    """Minimal ticket-tracker: no full CRM, just enough that a manager
    doesn't lose an inbound lead.

    Status advances one funnel step (see core/lead.py) on a click in the
    status column only — not anywhere on the row. This screen is the one a
    manager lives in all day, and a whole-row hit target turns every
    mis-aimed click into a silent state change on someone else's lead.
    The last change is undoable for the same reason; reassigning to a
    named manager is rarer, so it sits in the context menu.

    С3: a lead here no longer has to come from a bot — the "＋ Новая
    заявка" button and browse.py/watch.py's "Создать лид" both land here
    too, and the status chips double as a funnel (each carries its own
    count) rather than a separate visualization repeating the same
    numbers."""

    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.status_filter = "all"
        # (lead_id, previous_status, previous_reject_reason) of the last
        # status change, so it can be put back — cheaper for the user than
        # a confirmation on every click, and this is a reversible action.
        self._undo: tuple[int, str, str | None] | None = None

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
        new_lead_btn = button("＋ Новая заявка", "secondary")
        new_lead_btn.clicked.connect(self._on_new_lead)
        head.addWidget(new_lead_btn)
        export_btn = button("Выгрузить в Excel", "secondary")
        export_btn.clicked.connect(self._on_export)
        head.addWidget(export_btn)
        outer.addLayout(head)
        outer.addSpacing(12)

        # Status chips double as the funnel: each label carries its own
        # count (updated on every refresh), so "how many leads at each
        # stage" and "filter the list to that stage" are the same click
        # instead of a separate bar duplicating what the chips already do.
        #
        # С10: scoped to the *default* funnel's stages — every lead lives
        # there until a second funnel (П9, mail) actually has leads in
        # it, so this keeps matching every lead on screen today exactly.
        # A per-row lead's own pill (see _status_pill above) still
        # resolves against *that lead's own* funnel regardless, so a
        # future non-default-funnel lead renders correctly even though
        # it isn't one of these filter chips.
        self._default_stages = self.ctx.db.list_funnel_stages(self.ctx.db.default_funnel_id())
        chip_row = QHBoxLayout()
        chip_row.setSpacing(6)
        self.chip_group = QButtonGroup(self)
        self.chip_group.setExclusive(True)
        self.status_chips: dict[str, object] = {}
        self._status_chip_keys = ["all"] + [s["code"] for s in self._default_stages]
        for key in self._status_chip_keys:
            btn = chip(key if key == "all" else lead_domain.label_for_stage(self._default_stages, key))
            btn.setChecked(key == "all")
            btn.clicked.connect(lambda _c, k=key: self._set_filter(k))
            self.chip_group.addButton(btn)
            chip_row.addWidget(btn)
            self.status_chips[key] = btn
        chip_row.addStretch(1)
        outer.addLayout(chip_row)
        outer.addSpacing(8)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        filter_row.addWidget(muted("Направление"))
        self.direction_combo = QComboBox()
        self.direction_combo.currentIndexChanged.connect(lambda _i: self.refresh())
        filter_row.addWidget(self.direction_combo)
        filter_row.addWidget(muted("Откуда"))
        self.source_combo = QComboBox()
        self.source_combo.addItem("Любой источник", None)
        for key in (lead_domain.SOURCE_TYPE_BOT, lead_domain.SOURCE_TYPE_CHAT,
                    lead_domain.SOURCE_TYPE_DM, lead_domain.SOURCE_TYPE_MANUAL):
            self.source_combo.addItem(lead_domain.label_for_source_type(key), key)
        self.source_combo.currentIndexChanged.connect(lambda _i: self.refresh())
        filter_row.addWidget(self.source_combo)
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
        self.table.setHorizontalHeaderLabels(["Дата", "Контакт", "Откуда", "Содержание", "Статус"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.cellClicked.connect(self._on_cell_clicked)
        # Double-click anywhere opens the full card — the status column's
        # single-click quick-advance (see the class docstring) is left
        # alone, this is purely additive.
        self.table.cellDoubleClicked.connect(self._on_open_card)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        outer.addWidget(self.table, 1)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(4000)

    def on_show(self) -> None:
        self._populate_directions()
        self.refresh()

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
        all_leads = db.list_leads()
        status = None if self.status_filter == "all" else self.status_filter
        leads = db.list_leads(
            status=status, direction_id=self.direction_combo.currentData(),
            source_type=self.source_combo.currentData(), since=self._since(),
        )
        counts = db.leads_status_counts()
        for key, btn in self.status_chips.items():
            label = "Все" if key == "all" else lead_domain.label_for_stage(self._default_stages, key)
            n = len(all_leads) if key == "all" else counts.get(key, 0)
            btn.setText(f"{label} ({n})" if n else label)

        # С10: bucketed per-lead against each lead's own funnel — see
        # today.py's identical comment (list_leads() here has no funnel
        # filter, so all_leads can in principle span more than one).
        stages_cache: dict[int | None, list] = {}

        def _stages_for(funnel_id):
            if funnel_id not in stages_cache:
                stages_cache[funnel_id] = db.list_funnel_stages(funnel_id) if funnel_id else []
            return stages_cache[funnel_id]

        def _bucket(l):
            return lead_domain.bucket_for_stage(_stages_for(l["funnel_id"]), l["status"])

        n_new = len([l for l in all_leads if _bucket(l) == "new"])
        n_active = len([l for l in all_leads if _bucket(l) == "in_progress"])
        self.summary_label.setText(
            f"{n_new} новых · {n_active} в работе · {len(all_leads)} заявок всего"
            if all_leads else "заявок пока нет — они появятся, когда сработает правило бота"
        )
        self.undo_btn.setVisible(self._undo is not None)

        self.table.setRowCount(len(leads))
        for row, lead in enumerate(leads):
            date_item = QTableWidgetItem(short_dt(lead["created_at"]))
            date_item.setData(Qt.UserRole, lead["id"])
            self.table.setItem(row, 0, date_item)

            contact = db.get_contact(lead["contact_id"]) if lead["contact_id"] else None
            handle = lead["display_name"] or \
                (f"@{lead['username']}" if lead["username"] else None) or \
                (f"@{contact['username']}" if contact and contact["username"] else None) or \
                (str(contact["telegram_id"]) if contact else "—")
            manager = lead["manager"] or "не назначена"
            self.table.setItem(row, 1, QTableWidgetItem(f"{handle}\n{manager}"))

            # Not every lead has a bot behind it any more (С3: manual and
            # message-based creation) — fall back to the source label
            # rather than a bare "бот None".
            bot = db.get_bot(lead["bot_id"]) if lead["bot_id"] else None
            source_text = bot["name"] if bot else lead_domain.label_for_source_type(lead["source_type"])
            self.table.setItem(row, 2, QTableWidgetItem(source_text))

            try:
                content = json.loads(lead["content"])
                summary = "; ".join(f"{k}: {v}" for k, v in content.items()) if content else "—"
            except (json.JSONDecodeError, TypeError):
                summary = "—"
            self.table.setItem(row, 3, QTableWidgetItem(summary))

            stage = lead_domain.stage_for_code(_stages_for(lead["funnel_id"]), lead["status"])
            self.table.setCellWidget(row, 4, _status_pill(stage))
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
        # next_stage() never advances *to* a requires_reason stage (see
        # core/lead.py), only cycles won/lost back to the funnel's first
        # open stage — so this quick-advance never needs a reject_reason.
        # Restoring it on undo is a separate story below.
        stages = self.ctx.db.list_funnel_stages(lead["funnel_id"]) if lead["funnel_id"] else []
        self._undo = (lead_id, lead["status"], lead["reject_reason"])
        self.ctx.db.set_lead_status(lead_id, lead_domain.next_stage(stages, lead["status"]),
                                    source=lead_domain.EVENT_SOURCE_MANUAL)
        self.refresh()

    def _on_undo(self) -> None:
        if not self._undo:
            return
        lead_id, previous_status, previous_reject_reason = self._undo
        self._undo = None
        try:
            self.ctx.db.set_lead_status(
                lead_id, previous_status, reject_reason=previous_reject_reason,
                source=lead_domain.EVENT_SOURCE_MANUAL, text="отменено")
        except ValueError:
            # Only reachable if the previous state was somehow LOST with
            # no reason on record — shouldn't happen since that couldn't
            # have been set in the first place, but undo failing silently
            # would be worse than undo failing loudly.
            QMessageBox.information(self, "Не получилось отменить",
                                    "Не удалось восстановить предыдущий статус.")
        self.refresh()

    def _on_context_menu(self, pos) -> None:
        row = self.table.rowAt(pos.y())
        lead_id = self._lead_id_at(row) if row >= 0 else None
        if lead_id is None:
            return
        menu = QMenu(self)
        open_card = menu.addAction("Открыть карточку")
        reassign = menu.addAction("Переназначить менеджера…")
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen == open_card:
            self._open_card(lead_id)
        elif chosen == reassign:
            self._reassign(lead_id)

    def _on_open_card(self, row: int, _col: int) -> None:
        lead_id = self._lead_id_at(row)
        if lead_id is not None:
            self._open_card(lead_id)

    def _open_card(self, lead_id: int) -> None:
        dialog = LeadCardDialog(self.ctx, lead_id, parent=self)
        dialog.exec()
        self.refresh()

    def _on_new_lead(self) -> None:
        dialog = NewLeadDialog(self.ctx, parent=self)
        if dialog.exec() == dialog.Accepted and dialog.lead_id is not None:
            self._open_card(dialog.lead_id)
        self.refresh()

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
            # Reassigning who's handling a lead says nothing about where it
            # is in the funnel — the old code forced status="in_progress"
            # here, a value that no longer exists in the new vocabulary and
            # would have bypassed set_lead_status's history logging anyway.
            self.ctx.db.set_lead_field(lead_id, manager=name.strip())
            self.refresh()
