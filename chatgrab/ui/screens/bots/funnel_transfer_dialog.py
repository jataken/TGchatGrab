"""С10: "Перенос заявки между воронками вручную, с сопоставлением
этапов" — a small dialog off the lead card. Picking a target funnel
auto-suggests the stage of the same `kind` (open/won/lost) in that
funnel, since that's the closest honest guess at "where does this lead's
current progress map to over there" — the user can still pick any other
stage before confirming.
"""
from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QMessageBox, QVBoxLayout

from ...context import AppContext
from ...widgets import button, muted
from ....core import lead as lead_domain


class FunnelTransferDialog(QDialog):
    def __init__(self, ctx: AppContext, lead, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.lead = lead
        self.setWindowTitle("Перенести в другую воронку")
        self.resize(420, 180)

        current_stages = ctx.db.list_funnel_stages(lead["funnel_id"]) if lead["funnel_id"] else []
        current_stage = lead_domain.stage_for_code(current_stages, lead["status"])
        current_kind = current_stage["kind"] if current_stage is not None else lead_domain.KIND_OPEN

        outer = QVBoxLayout(self)
        outer.addWidget(muted(
            f"Сейчас: «{lead_domain.label_for_stage(current_stages, lead['status'])}» "
            f"в воронке «{self._funnel_name(lead['funnel_id'])}»."))

        funnel_row = QHBoxLayout()
        funnel_row.addWidget(muted("Новая воронка"))
        self.funnel_combo = QComboBox()
        for funnel in ctx.db.list_funnels():
            self.funnel_combo.addItem(funnel["name"], funnel["id"])
        funnel_row.addWidget(self.funnel_combo, 1)
        outer.addLayout(funnel_row)

        stage_row = QHBoxLayout()
        stage_row.addWidget(muted("Новый этап"))
        self.stage_combo = QComboBox()
        stage_row.addWidget(self.stage_combo, 1)
        outer.addLayout(stage_row)

        self._current_kind = current_kind
        self.funnel_combo.currentIndexChanged.connect(self._on_funnel_changed)
        idx = self.funnel_combo.findData(lead["funnel_id"])
        self.funnel_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._on_funnel_changed(self.funnel_combo.currentIndex())

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel_btn = button("Отмена", "secondary")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        confirm_btn = button("Перенести", "primary")
        confirm_btn.clicked.connect(self._on_confirm)
        btn_row.addWidget(confirm_btn)
        outer.addLayout(btn_row)

    def _funnel_name(self, funnel_id: int | None) -> str:
        if funnel_id is None:
            return "—"
        funnel = self.ctx.db.get_funnel(funnel_id)
        return funnel["name"] if funnel is not None else "—"

    def _on_funnel_changed(self, _index: int) -> None:
        funnel_id = self.funnel_combo.currentData()
        stages = self.ctx.db.list_funnel_stages(funnel_id) if funnel_id is not None else []
        self.stage_combo.clear()
        suggested_index = 0
        for i, stage in enumerate(stages):
            self.stage_combo.addItem(stage["label"], stage["code"])
            if stage["kind"] == self._current_kind and suggested_index == 0:
                suggested_index = i
        self.stage_combo.setCurrentIndex(suggested_index)

    def _on_confirm(self) -> None:
        funnel_id = self.funnel_combo.currentData()
        stage_code = self.stage_combo.currentData()
        if funnel_id is None or stage_code is None:
            return
        try:
            self.ctx.db.transfer_lead_funnel(self.lead["id"], funnel_id, stage_code)
        except ValueError as e:
            QMessageBox.information(self, "Не получилось", str(e))
            return
        self.accept()
