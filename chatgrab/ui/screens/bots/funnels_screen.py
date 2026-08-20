"""С10: "Экран управления воронками" — funnels on the left, the selected
funnel's stages on the right: reorder (↑/↓, same pattern as
directions.py — no real drag-and-drop, see PLAN.md's С10 journal for why
that substitution is fine), kind (open/won/lost — which stage "считается
сделкой"), "причина обязательна" (requires_reason), color, delete. A new
funnel starts with one open-kind stage so it's never left with nothing to
put a lead into.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QHBoxLayout, QHeaderView, QInputDialog,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ...context import AppContext
from ...widgets import button, h1, muted
from ....core import lead as lead_domain

_KIND_LABELS = {
    lead_domain.KIND_OPEN: "в работе",
    lead_domain.KIND_WON: "сделка",
    lead_domain.KIND_LOST: "отказ",
}
_KIND_ORDER = [lead_domain.KIND_OPEN, lead_domain.KIND_WON, lead_domain.KIND_LOST]

_STAGE_COLOR_SWATCHES = [
    "#4f7cff", "#f0a63a", "#28a99e", "#e5484d",
    "#8a8f98", "#a875e8", "#2f9e44", "#d6336c",
]


class FunnelsScreen(QWidget):
    def __init__(self, ctx: AppContext, navigate):
        super().__init__()
        self.ctx = ctx
        self.navigate = navigate
        self.selected_funnel_id: int | None = None
        self._new_stage_color = _STAGE_COLOR_SWATCHES[0]

        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 28, 40, 24)
        outer.addWidget(h1("Воронки"))
        hint = muted(
            "Каждая воронка — свой набор этапов. У почты (когда она заведёт свои заявки) "
            "будет своя воронка, отдельная от телеграмной — здесь можно завести любую."
        )
        hint.setWordWrap(True)
        outer.addWidget(hint)
        outer.addSpacing(12)

        splitter = QSplitter(Qt.Horizontal)
        outer.addWidget(splitter, 1)

        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.addWidget(muted("ВОРОНКИ"))
        self.funnel_list = QListWidget()
        self.funnel_list.currentItemChanged.connect(self._on_funnel_selected)
        left_lay.addWidget(self.funnel_list, 1)
        new_funnel_row = QHBoxLayout()
        self.new_funnel_name = QLineEdit()
        self.new_funnel_name.setPlaceholderText("Название новой воронки…")
        new_funnel_row.addWidget(self.new_funnel_name, 1)
        add_funnel_btn = button("Добавить", "secondary")
        add_funnel_btn.clicked.connect(self._on_add_funnel)
        new_funnel_row.addWidget(add_funnel_btn)
        left_lay.addLayout(new_funnel_row)
        splitter.addWidget(left)

        right = QWidget()
        right_lay = QVBoxLayout(right)
        self.stages_title = muted("Выберите воронку слева.")
        right_lay.addWidget(self.stages_title)

        self.stage_table = QTableWidget(0, 6)
        self.stage_table.setHorizontalHeaderLabels(
            ["Этап", "Тип", "Причина обязательна", "Цвет", "↕", "Удалить"])
        self.stage_table.verticalHeader().setVisible(False)
        self.stage_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.stage_table.setShowGrid(False)
        self.stage_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        right_lay.addWidget(self.stage_table, 1)

        add_stage_row = QHBoxLayout()
        self.new_stage_label = QLineEdit()
        self.new_stage_label.setPlaceholderText("Название нового этапа…")
        add_stage_row.addWidget(self.new_stage_label, 1)
        self.new_stage_kind = QComboBox()
        for kind in _KIND_ORDER:
            self.new_stage_kind.addItem(_KIND_LABELS[kind], kind)
        add_stage_row.addWidget(self.new_stage_kind)
        self.swatch_group = QButtonGroup(self)
        self.swatch_group.setExclusive(True)
        for color in _STAGE_COLOR_SWATCHES:
            swatch_btn = QPushButton()
            swatch_btn.setCheckable(True)
            swatch_btn.setFixedSize(22, 22)
            swatch_btn.setCursor(Qt.PointingHandCursor)
            swatch_btn.setStyleSheet(
                f"QPushButton {{ background-color: {color}; border-radius: 11px; "
                f"border: 2px solid transparent; }}"
                f"QPushButton:checked {{ border: 2px solid white; }}")
            swatch_btn.setChecked(color == self._new_stage_color)
            swatch_btn.clicked.connect(lambda _c, col=color: self._on_pick_color(col))
            self.swatch_group.addButton(swatch_btn)
            add_stage_row.addWidget(swatch_btn)
        add_stage_btn = button("Добавить этап", "secondary")
        add_stage_btn.clicked.connect(self._on_add_stage)
        add_stage_row.addWidget(add_stage_btn)
        right_lay.addLayout(add_stage_row)
        splitter.addWidget(right)
        splitter.setSizes([220, 640])

    def on_show(self, **kwargs) -> None:
        self._refresh_funnels()

    # ---- funnels -----------------------------------------------------
    def _refresh_funnels(self) -> None:
        current = self.selected_funnel_id
        self.funnel_list.blockSignals(True)
        self.funnel_list.clear()
        restore_row = 0
        for i, funnel in enumerate(self.ctx.db.list_funnels()):
            item = QListWidgetItem(f"{funnel['name']}  ·  {funnel['channel']}")
            item.setData(Qt.UserRole, funnel["id"])
            self.funnel_list.addItem(item)
            if funnel["id"] == current:
                restore_row = i
        self.funnel_list.blockSignals(False)
        if self.funnel_list.count():
            self.funnel_list.setCurrentRow(restore_row)
            self.selected_funnel_id = self.funnel_list.item(restore_row).data(Qt.UserRole)
        else:
            self.selected_funnel_id = None
        self._refresh_stages()

    def _on_funnel_selected(self, current: QListWidgetItem, _previous) -> None:
        self.selected_funnel_id = current.data(Qt.UserRole) if current is not None else None
        self._refresh_stages()

    def _on_add_funnel(self) -> None:
        name = self.new_funnel_name.text().strip()
        if not name:
            return
        channel, ok = QInputDialog.getText(
            self, "Канал воронки", "Канал (например, telegram или email):", text="telegram")
        if not ok:
            return
        funnel_id = self.ctx.db.create_funnel(name, channel.strip() or "telegram")
        # A funnel with zero stages has nowhere to put a lead — seed one
        # open-kind stage so it's usable the moment it's created.
        self.ctx.db.create_funnel_stage(funnel_id, "new", "новый", kind=lead_domain.KIND_OPEN)
        self.new_funnel_name.clear()
        self.selected_funnel_id = funnel_id
        self._refresh_funnels()

    # ---- stages --------------------------------------------------------
    def _on_pick_color(self, color: str) -> None:
        self._new_stage_color = color

    def _refresh_stages(self) -> None:
        if self.selected_funnel_id is None:
            self.stages_title.setText("Выберите воронку слева.")
            self.stage_table.setRowCount(0)
            return
        funnel = self.ctx.db.get_funnel(self.selected_funnel_id)
        self.stages_title.setText(f"Этапы воронки «{funnel['name']}»" if funnel else "")
        stages = self.ctx.db.list_funnel_stages(self.selected_funnel_id)
        self.stage_table.setRowCount(len(stages))
        for i, stage in enumerate(stages):
            name_item = QTableWidgetItem(stage["label"])
            self.stage_table.setItem(i, 0, name_item)

            kind_combo = QComboBox()
            for kind in _KIND_ORDER:
                kind_combo.addItem(_KIND_LABELS[kind], kind)
            kind_combo.setCurrentIndex(_KIND_ORDER.index(stage["kind"]))
            kind_combo.currentIndexChanged.connect(
                lambda _idx, sid=stage["id"], combo=kind_combo: self._on_kind_changed(sid, combo))
            self.stage_table.setCellWidget(i, 1, kind_combo)

            reason_holder = QWidget()
            reason_lay = QHBoxLayout(reason_holder)
            reason_lay.setContentsMargins(8, 0, 0, 0)
            reason_cb = QCheckBox()
            reason_cb.setChecked(bool(stage["requires_reason"]))
            reason_cb.toggled.connect(lambda on, sid=stage["id"]: self._on_toggle_requires_reason(sid, on))
            reason_lay.addWidget(reason_cb)
            reason_lay.addStretch(1)
            self.stage_table.setCellWidget(i, 2, reason_holder)

            swatch_holder = QWidget()
            swatch_lay = QHBoxLayout(swatch_holder)
            swatch_lay.setContentsMargins(8, 0, 0, 0)
            swatch = QWidget()
            swatch.setFixedSize(16, 16)
            swatch.setStyleSheet(f"QWidget {{ background-color: {stage['color_dot']}; border-radius: 8px; }}")
            swatch_lay.addWidget(swatch)
            swatch_lay.addStretch(1)
            self.stage_table.setCellWidget(i, 3, swatch_holder)

            move_holder = QWidget()
            move_lay = QHBoxLayout(move_holder)
            move_lay.setContentsMargins(0, 0, 0, 0)
            move_lay.setSpacing(2)
            up_btn = button("↑", "ghost")
            up_btn.setFixedWidth(26)
            up_btn.setEnabled(i > 0)
            up_btn.clicked.connect(lambda _c, idx=i: self._on_move_stage(idx, -1))
            move_lay.addWidget(up_btn)
            down_btn = button("↓", "ghost")
            down_btn.setFixedWidth(26)
            down_btn.setEnabled(i < len(stages) - 1)
            down_btn.clicked.connect(lambda _c, idx=i: self._on_move_stage(idx, 1))
            move_lay.addWidget(down_btn)
            self.stage_table.setCellWidget(i, 4, move_holder)

            del_btn = button("Удалить", "ghost")
            del_btn.clicked.connect(lambda _c, sid=stage["id"], lbl=stage["label"]: self._on_delete_stage(sid, lbl))
            self.stage_table.setCellWidget(i, 5, del_btn)

            self.stage_table.setRowHeight(i, 40)
        self.stage_table.resizeColumnsToContents()
        header = self.stage_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for col, width in ((1, 110), (2, 150), (3, 60), (4, 64), (5, 96)):
            header.setSectionResizeMode(col, QHeaderView.Fixed)
            self.stage_table.setColumnWidth(col, width)

    def _on_kind_changed(self, stage_id: int, combo: QComboBox) -> None:
        self.ctx.db.update_funnel_stage(stage_id, kind=combo.currentData())

    def _on_toggle_requires_reason(self, stage_id: int, on: bool) -> None:
        self.ctx.db.update_funnel_stage(stage_id, requires_reason=on)

    def _on_move_stage(self, index: int, delta: int) -> None:
        stages = self.ctx.db.list_funnel_stages(self.selected_funnel_id)
        new_index = index + delta
        if not (0 <= new_index < len(stages)):
            return
        ids = [s["id"] for s in stages]
        ids[index], ids[new_index] = ids[new_index], ids[index]
        self.ctx.db.reorder_funnel_stages(self.selected_funnel_id, ids)
        self._refresh_stages()

    def _on_delete_stage(self, stage_id: int, label: str) -> None:
        stages = self.ctx.db.list_funnel_stages(self.selected_funnel_id)
        if len(stages) <= 1:
            QMessageBox.information(
                self, "Нельзя удалить",
                "В воронке должен остаться хотя бы один этап — иначе заявкам некуда деваться.")
            return
        if QMessageBox.question(
            self, "Удалить этап",
            f"Удалить этап «{label}»? Заявки, стоящие на нём сейчас, останутся с этим "
            "кодом статуса, но он перестанет отображаться в списке этапов воронки."
        ) != QMessageBox.Yes:
            return
        self.ctx.db.delete_funnel_stage(stage_id)
        self._refresh_stages()

    def _on_add_stage(self) -> None:
        if self.selected_funnel_id is None:
            return
        label_text = self.new_stage_label.text().strip()
        if not label_text:
            return
        code = _slugify(label_text)
        existing = self.ctx.db.get_funnel_stage_by_code(self.selected_funnel_id, code)
        if existing is not None:
            QMessageBox.information(self, "Уже есть", "Этап с таким названием уже есть в этой воронке.")
            return
        self.ctx.db.create_funnel_stage(
            self.selected_funnel_id, code, label_text,
            kind=self.new_stage_kind.currentData(), color_bg=_bg_for(self._new_stage_color),
            color_fg="#ffffff", color_dot=self._new_stage_color)
        self.new_stage_label.clear()
        self._refresh_stages()


def _slugify(text: str) -> str:
    """Label -> a stable code: lowercase, non-alnum runs collapsed to a
    single underscore. Doesn't need to be pretty — only stored, never
    shown (the label is what's shown)."""
    out = []
    prev_underscore = False
    for ch in text.strip().lower():
        if ch.isalnum():
            out.append(ch)
            prev_underscore = False
        elif not prev_underscore:
            out.append("_")
            prev_underscore = True
    return "".join(out).strip("_") or "stage"


def _bg_for(dot_color: str) -> str:
    """A translucent background derived from the chosen swatch's own hex
    — matches the low-alpha rgba() look every seeded default stage
    already uses, without asking the user to pick two colors per stage."""
    hex_color = dot_color.lstrip("#")
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},40)"
