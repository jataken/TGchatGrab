"""П9: "Кнопка в цепочке ... открывается NewLeadDialog с предзаполнением"
— mail's own dialog, not a rewired ui/screens/bots/lead_create_dialog.py:
that one is Telegram's ("никогда не писал ни через бота, ни в
отслеживаемый чат" — a different starting point entirely), and bending
it to also understand a mail message would blur exactly the line
PLAN.md's П-2 invariant draws. The two dialogs share nothing but the
shape of the form; the lead they create lands in the same `bot_leads`
table either way — that's the domain both were always allowed to share.
"""
from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QVBoxLayout

from ...context import AppContext
from ...widgets import FieldRow, TabletCheckBox, button, muted


class MailLeadDialog(QDialog):
    def __init__(self, ctx: AppContext, message_id: int, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.message_id = message_id
        self.lead_id: int | None = None
        self.setWindowTitle("Завести заявку из письма")
        self.setMinimumWidth(420)

        message = ctx.db.get_mail_message(message_id)
        self._message = message

        outer = QVBoxLayout(self)
        self.name_field = FieldRow("Контакт")
        self.name_field.set_text(
            (message["sender_name"] if message else "") or (message["sender_address"] if message else "") or "")
        outer.addWidget(self.name_field)

        outer.addWidget(muted(f"Email: {message['sender_address'] if message else '—'}"))

        self.product_field = FieldRow("Тема запроса")
        self.product_field.set_text((message["subject"] if message else "") or "")
        outer.addWidget(self.product_field)

        self.phone_field = FieldRow("Телефон")
        self.direction_combo = QComboBox()
        self.direction_combo.addItem("— не указано —", None)
        matched_id = ctx.mail_service.matched_direction_id(message_id) if message else None
        for i, direction in enumerate(ctx.db.list_directions(), start=1):
            self.direction_combo.addItem(direction["name"], direction["id"])
            if direction["id"] == matched_id:
                self.direction_combo.setCurrentIndex(i)

        outer.addWidget(self.phone_field)
        outer.addWidget(muted("Направление"))
        outer.addWidget(self.direction_combo)

        self._proposals: dict = {}
        self._proposal_checks: dict[str, TabletCheckBox] = {}
        self.proposals_box = QVBoxLayout()
        outer.addLayout(self.proposals_box)

        attachments = ctx.db.list_mail_attachments(message_id) if message else []
        if attachments:
            parse_btn = button("Разобрать вложения", "secondary")
            parse_btn.clicked.connect(self._on_parse_attachments)
            outer.addWidget(parse_btn)

        row = QHBoxLayout()
        row.addStretch(1)
        cancel_btn = button("Отмена", "ghost")
        cancel_btn.clicked.connect(self.reject)
        row.addWidget(cancel_btn)
        create_btn = button("Создать", "primary")
        create_btn.clicked.connect(self._on_create)
        row.addWidget(create_btn)
        outer.addLayout(row)

        # Тело письма уже само по себе разбирается по телефону/ИНН/объёму
        # — предлагаем сразу, не дожидаясь клика по «Разобрать вложения»,
        # раз вложений может и не быть.
        body_proposals = ctx.mail_service.suggest_lead_fields(message_id) if message else {}
        if body_proposals:
            self._show_proposals(body_proposals)

    # ---- разбор вложений (П9) ---------------------------------------------
    def _on_parse_attachments(self) -> None:
        proposals = self.ctx.mail_service.suggest_lead_fields(self.message_id)
        if not proposals:
            muted_label = muted("Ничего не удалось разобрать автоматически.")
            self.proposals_box.addWidget(muted_label)
            return
        self._show_proposals(proposals)

    def _show_proposals(self, proposals: dict) -> None:
        while self.proposals_box.count():
            item = self.proposals_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._proposal_checks.clear()
        self._proposals = proposals
        self.proposals_box.addWidget(muted("Найдено автоматически — отметьте, что подставить:"))
        _LABELS = {"phone": "Телефон", "inn": "ИНН", "volume": "Объём",
                   "unit": "Единица", "product": "Товар", "deadline": "Срок"}
        for field, value in proposals.items():
            row = QHBoxLayout()
            cb = TabletCheckBox(f"{_LABELS.get(field, field)}: {value}")
            cb.setChecked(True)
            row.addWidget(cb)
            row.addStretch(1)
            self.proposals_box.addLayout(row)
            self._proposal_checks[field] = cb

    def _checked_proposals(self) -> dict:
        return {field: self._proposals[field] for field, cb in self._proposal_checks.items()
                if cb.isChecked() and field in self._proposals}

    # ---- создание -----------------------------------------------------
    def _on_create(self) -> None:
        applied = self._checked_proposals()
        phone = self.phone_field.text().strip() or applied.get("phone")
        product = self.product_field.text().strip() or applied.get("product")
        lead_id = self.ctx.mail_service.create_lead_from_message(
            self.message_id, direction_id=self.direction_combo.currentData(),
            product=product, phone=phone,
        )
        if lead_id is None:
            self.reject()
            return
        extra = {}
        if applied.get("volume"):
            extra["volume"] = applied["volume"]
        if applied.get("unit"):
            extra["unit"] = applied["unit"]
        if applied.get("deadline"):
            extra["deadline"] = applied["deadline"]
        name = self.name_field.text().strip()
        if name:
            extra["display_name"] = name
        if extra:
            self.ctx.db.set_lead_field(lead_id, **extra)
        self.lead_id = lead_id
        self.accept()
