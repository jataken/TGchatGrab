from __future__ import annotations

import datetime as dt
import json

from PySide6.QtCore import QUrl, Qt, QTimer, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QFrame, QHBoxLayout, QInputDialog, QMenu,
    QMessageBox, QScrollArea, QVBoxLayout, QWidget,
)

from ... import theme
from ...context import AppContext
from ...format import short_dt
from ...util import fire, run_blocking
from ...widgets import LeadStatusPill, button, chip, label, muted
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

_COL_DATE = 100
_COL_CONTACT = 190
_COL_SOURCE = 130
_COL_STATUS = 150
_FALLBACK_DOT = "rgba(140,140,150,140)"


class _StatusCell(QWidget):
    """The status pill, plus the click target for the row's quick-advance
    (design-brief.md §4.3's "СБОР"-column click-to-act idea, applied to a
    lead's own single stage advance instead of a toggle)."""

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


class _LeadRow(QFrame):
    """One row of the "table becomes a card" list (design-brief.md §4.3,
    same geometry as chats.py's `_ChatRow`) for leads: a 2px left stripe
    colored by the lead's own funnel-stage `color_dot` — leads have no
    fixed status enum/color table the way chats do (С10: every funnel
    defines its own stage colors) — then date/contact/source/content/
    status columns, no sparkline (nothing per-lead to chart). Single click
    on the status pill quick-advances the stage; double-click anywhere
    else opens the full card; right-click opens the context menu."""

    doubleClicked = Signal()
    statusClicked = Signal()
    contextRequested = Signal(object)

    def __init__(self, lead_id: int):
        super().__init__()
        self.lead_id = lead_id
        self.setProperty("class", "tablerow")
        self.setCursor(Qt.PointingHandCursor)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.contextRequested.emit)

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

        self.source_label = label("")
        self.source_label.setFixedWidth(_COL_SOURCE)
        self.source_label.setStyleSheet(f"font-size: 12px; color: {theme.TEXT_MUTED};")
        lay.addWidget(self.source_label)

        self.content_label = label("")
        self.content_label.setWordWrap(False)
        lay.addWidget(self.content_label, 1)

        self.status_cell = _StatusCell(None)
        self.status_cell.clicked.connect(self.statusClicked)
        lay.addWidget(self.status_cell)

    def set_data(self, date_text: str, name_text: str, manager_text: str,
                 source_text: str, content_text: str, stage) -> None:
        self.date_label.setText(date_text)
        self.name_label.setText(name_text)
        self.manager_label.setText(manager_text)
        self.source_label.setText(source_text)
        self.content_label.setText(content_text)
        self.status_cell.set_stage(stage)
        dot = stage["color_dot"] if stage is not None else _FALLBACK_DOT
        self.stripe.setStyleSheet(f"background: {dot}; border-radius: 1px;")

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.doubleClicked.emit()


class _LeadTableHeader(QWidget):
    """Кикеры над списком строк — те же колонки, что и у `_LeadRow`."""

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
        source_k = kicker("ОТКУДА")
        source_k.setFixedWidth(_COL_SOURCE)
        lay.addWidget(source_k)
        content_k = kicker("СОДЕРЖАНИЕ")
        lay.addWidget(content_k, 1)
        status_k = kicker("СТАТУС")
        status_k.setFixedWidth(_COL_STATUS)
        status_k.setAlignment(Qt.AlignCenter)
        lay.addWidget(status_k)
        self.setStyleSheet(f"QWidget {{ border-bottom: 1px solid {theme.DIVIDER}; }}")


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
        # A per-row lead's own pill (see `_LeadRow`/`_StatusCell` above)
        # still resolves against *that lead's own* funnel regardless, so a
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

        # "Таблица становится карточкой" (design-brief.md §4.3, та же
        # геометрия строки, что у «Чаты» — chats.py's _ChatRow/_TableHeader
        # — только без спарклайна: у заявки нет своей 30-дневной активности
        # для графика).
        self._rows: dict[int, _LeadRow] = {}
        table_card = QFrame()
        table_card.setProperty("class", "card")
        table_lay = QVBoxLayout(table_card)
        table_lay.setContentsMargins(0, 0, 0, 0)
        table_lay.setSpacing(0)
        table_lay.addWidget(_LeadTableHeader())

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
        # П9's mail funnel seeds stage codes ("new"/"qualified"/…) that
        # collide with the default funnel's own — a status filter without
        # funnel_id would leak an email lead sitting at, say, "new" into
        # this screen's Telegram-funnel-scoped chips, showing rows the
        # chip's own count (from leads_status_counts() below, already
        # funnel-scoped) doesn't account for.
        leads = db.list_leads(
            status=status, direction_id=self.direction_combo.currentData(),
            source_type=self.source_combo.currentData(), since=self._since(),
            funnel_id=self.ctx.db.default_funnel_id() if status is not None else None,
        )
        counts = db.leads_status_counts()
        for key, btn in self.status_chips.items():
            chip_label = "Все" if key == "all" else lead_domain.label_for_stage(self._default_stages, key)
            n = len(all_leads) if key == "all" else counts.get(key, 0)
            btn.setText(f"{chip_label} ({n})" if n else chip_label)

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

        seen = set()
        for lead in leads:
            lead_id = lead["id"]
            seen.add(lead_id)

            contact = db.get_contact(lead["contact_id"]) if lead["contact_id"] else None
            handle = lead["display_name"] or \
                (f"@{lead['username']}" if lead["username"] else None) or \
                (f"@{contact['username']}" if contact and contact["username"] else None) or \
                (str(contact["telegram_id"]) if contact else "—")
            manager_text = lead["manager"] or "не назначена"

            # Not every lead has a bot behind it any more (С3: manual and
            # message-based creation) — fall back to the source label
            # rather than a bare "бот None".
            bot = db.get_bot(lead["bot_id"]) if lead["bot_id"] else None
            source_text = bot["name"] if bot else lead_domain.label_for_source_type(lead["source_type"])

            try:
                content = json.loads(lead["content"])
                summary = "; ".join(f"{k}: {v}" for k, v in content.items()) if content else "—"
            except (json.JSONDecodeError, TypeError):
                summary = "—"

            stage = lead_domain.stage_for_code(_stages_for(lead["funnel_id"]), lead["status"])

            row = self._rows.get(lead_id)
            if row is None:
                row = _LeadRow(lead_id)
                row.doubleClicked.connect(lambda lid=lead_id: self._open_card(lid))
                row.statusClicked.connect(lambda lid=lead_id: self._quick_advance(lid))
                row.contextRequested.connect(lambda pos, lid=lead_id, r=row: self._on_row_context_menu(lid, r, pos))
                self._rows[lead_id] = row
            row.set_data(short_dt(lead["created_at"]), handle, manager_text, source_text, summary, stage)

        for lead_id in list(self._rows):
            if lead_id not in seen:
                gone = self._rows.pop(lead_id)
                gone.setParent(None)
                gone.deleteLater()

        # Detach every remaining row from the layout (widgets survive —
        # just get reparented out) so the current filtered/sorted order
        # can be rebuilt cleanly each tick — same technique as Д6's bot
        # grid in `list_tab.py`.
        while self.rows_lay.count():
            item = self.rows_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
        for lead in leads:
            self.rows_lay.addWidget(self._rows[lead["id"]])
        self.rows_lay.addStretch(1)

    def _quick_advance(self, lead_id: int) -> None:
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

    def _on_row_context_menu(self, lead_id: int, row: _LeadRow, pos) -> None:
        menu = QMenu(self)
        open_card = menu.addAction("Открыть карточку")
        reassign = menu.addAction("Переназначить менеджера…")
        chosen = menu.exec(row.mapToGlobal(pos))
        if chosen == open_card:
            self._open_card(lead_id)
        elif chosen == reassign:
            self._reassign(lead_id)

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
