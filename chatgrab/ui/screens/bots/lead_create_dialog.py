"""Заводит лид вручную — «Новая заявка» на экране «Заявки», для контакта,
который никогда не писал ни через бота, ни в отслеживаемый чат (звонок,
визитка, письмо). Собирает только то, что не спросишь позже иначе:
дальше заявка открывается как обычная карточка (lead_card.py), и всё
остальное — статус, история, вложения — редактируется там же, что и у
любого другого лида.
"""
from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QDialog, QFormLayout, QHBoxLayout, QLineEdit, QVBoxLayout

from ...context import AppContext
from ...widgets import button
from ....core import lead as lead_domain


class NewLeadDialog(QDialog):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle("Новая заявка")
        self.setMinimumWidth(360)
        self.lead_id: int | None = None

        outer = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(6)

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("имя или как обращаться")
        form.addRow("Контакт", self.name_input)
        self.phone_input = QLineEdit()
        form.addRow("Телефон", self.phone_input)
        self.direction_combo = QComboBox()
        self.direction_combo.addItem("— не указано —", None)
        for direction in ctx.db.list_directions():
            self.direction_combo.addItem(direction["name"], direction["id"])
        form.addRow("Направление", self.direction_combo)
        self.product_input = QLineEdit()
        form.addRow("Товар", self.product_input)
        outer.addLayout(form)

        row = QHBoxLayout()
        row.addStretch(1)
        cancel_btn = button("Отмена", "ghost")
        cancel_btn.clicked.connect(self.reject)
        row.addWidget(cancel_btn)
        create_btn = button("Создать", "primary")
        create_btn.clicked.connect(self._on_create)
        row.addWidget(create_btn)
        outer.addLayout(row)

    def _on_create(self) -> None:
        name = self.name_input.text().strip()
        self.lead_id = self.ctx.db.add_lead(
            None, None, {}, status=lead_domain.NEW,
            display_name=name or None,
            phone=self.phone_input.text().strip() or None,
            source_type=lead_domain.SOURCE_TYPE_MANUAL,
            direction_id=self.direction_combo.currentData(),
            product=self.product_input.text().strip() or None,
            event_source=lead_domain.EVENT_SOURCE_MANUAL,
        )
        self.accept()
