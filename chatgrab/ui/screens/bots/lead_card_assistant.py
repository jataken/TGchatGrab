"""«Помощник» tab (С9) — split out of lead_card.py in Р5, same
composition pattern as lead_card_bitrix.py's panel.

get_source_text is a zero-arg callable the dialog wires to its own
«Переписка» tab's corr_view.toPlainText() — not a separate query to the
database. That's a deliberate С9 invariant, not an accident of how this
used to be one file: the user must always be looking at exactly the text
that would go into a prompt, on the tab right next to this one, so
extraction/summary/draft can never see anything the human reviewing them
doesn't already see too.

on_changed is called wherever this panel actually writes something (apply
extracted fields, save a summary as a note) so the dialog embedding it can
re-read the lead and refresh whatever else on screen might reflect that —
saving a draft doesn't call it, matching the original: a draft goes into
outbox_drafts, not onto this lead, so nothing here needs to change.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLineEdit, QMessageBox,
    QPlainTextEdit, QVBoxLayout, QWidget,
)

from ...context import AppContext
from ...util import fire
from ...widgets import button, hline, label, muted
from ....core import lead as lead_domain
from ....integrations import llm


class LeadCardAssistantPanel(QWidget):
    def __init__(self, ctx: AppContext, lead_id: int, get_source_text: Callable[[], str],
                 on_changed: Callable[[], None] | None = None, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.lead_id = lead_id
        self._get_source_text = get_source_text
        self._on_changed = on_changed or (lambda: None)
        self._lead = None

        assist_lay = QVBoxLayout(self)
        self.assist_hint = muted("")
        self.assist_hint.setWordWrap(True)
        assist_lay.addWidget(self.assist_hint)

        assist_lay.addWidget(label("ИЗВЛЕЧЬ ПОЛЯ ИЗ ПЕРЕПИСКИ", "kicker"))
        extract_row = QHBoxLayout()
        self.extract_btn = button("Извлечь поля", "secondary")
        self.extract_btn.clicked.connect(self._on_llm_extract)
        extract_row.addWidget(self.extract_btn)
        extract_row.addStretch(1)
        assist_lay.addLayout(extract_row)

        self._extract_rows: dict[str, tuple[QCheckBox, QLineEdit]] = {}
        extract_form = QFormLayout()
        for field_key in lead_domain.SCENARIO_LEAD_FIELDS:
            field_row = QHBoxLayout()
            field_row.setContentsMargins(0, 0, 0, 0)
            cb = QCheckBox()
            field_row.addWidget(cb)
            edit = QLineEdit()
            field_row.addWidget(edit, 1)
            row_widget = QWidget()
            row_widget.setLayout(field_row)
            extract_form.addRow(lead_domain.SCENARIO_LEAD_FIELD_LABELS[field_key], row_widget)
            self._extract_rows[field_key] = (cb, edit)
        assist_lay.addLayout(extract_form)
        apply_extract_btn = button("Применить отмеченные", "primary")
        apply_extract_btn.clicked.connect(self._on_llm_apply_extract)
        assist_lay.addWidget(apply_extract_btn)
        assist_lay.addWidget(hline())

        assist_lay.addWidget(label("КРАТКОЕ СОДЕРЖАНИЕ ПЕРЕПИСКИ", "kicker"))
        summary_row = QHBoxLayout()
        self.summary_btn = button("Составить резюме", "secondary")
        self.summary_btn.clicked.connect(self._on_llm_summary)
        summary_row.addWidget(self.summary_btn)
        summary_row.addStretch(1)
        assist_lay.addLayout(summary_row)
        self.summary_view = QPlainTextEdit()
        self.summary_view.setMaximumHeight(110)
        assist_lay.addWidget(self.summary_view)
        save_summary_btn = button("Сохранить как заметку", "secondary")
        save_summary_btn.clicked.connect(self._on_llm_save_summary)
        assist_lay.addWidget(save_summary_btn)
        assist_lay.addWidget(hline())

        assist_lay.addWidget(label("ЧЕРНОВИК ОТВЕТА", "kicker"))
        draft_row = QHBoxLayout()
        draft_row.addWidget(muted("От имени бота"))
        self.draft_bot_combo = QComboBox()
        draft_row.addWidget(self.draft_bot_combo, 1)
        self.draft_btn = button("Сгенерировать черновик", "secondary")
        self.draft_btn.clicked.connect(self._on_llm_draft)
        draft_row.addWidget(self.draft_btn)
        assist_lay.addLayout(draft_row)
        self.draft_view = QPlainTextEdit()
        self.draft_view.setMaximumHeight(110)
        assist_lay.addWidget(self.draft_view)
        save_draft_btn = button("Сохранить как черновик в «Черновики»", "primary")
        save_draft_btn.clicked.connect(self._on_llm_save_draft)
        assist_lay.addWidget(save_draft_btn)
        assist_lay.addStretch(1)

    def refresh(self, lead) -> None:
        self._lead = lead
        enabled = llm.is_enabled(self.ctx.db, self.ctx.security)
        has_text = bool(self._get_source_text().strip())
        if not enabled:
            self.assist_hint.setText(
                "Выключен — включите в Настройках, в разделе «LLM-помощник», и укажите ключ API."
            )
        elif not has_text:
            self.assist_hint.setText("Нет собранной переписки с этим контактом — анализировать нечего.")
        else:
            self.assist_hint.setText(
                "Работает как подсказка: ничего не сохраняется в заявку, пока вы сами не нажмёте "
                "«Применить»/«Сохранить»."
            )
        for btn in (self.extract_btn, self.summary_btn, self.draft_btn):
            btn.setEnabled(enabled and has_text)

        self.draft_bot_combo.blockSignals(True)
        self.draft_bot_combo.clear()
        bots = self.ctx.db.list_bots()
        for bot in bots:
            self.draft_bot_combo.addItem(bot["name"], bot["id"])
        if lead["bot_id"] is not None:
            idx = self.draft_bot_combo.findData(lead["bot_id"])
            if idx >= 0:
                self.draft_bot_combo.setCurrentIndex(idx)
        self.draft_bot_combo.blockSignals(False)
        self.draft_bot_combo.setEnabled(enabled and bool(bots))
        if not bots:
            self.draft_btn.setEnabled(False)

    # ---- LLM calls -----------------------------------------------------
    def _llm_client(self):
        return llm.build_client(self.ctx.db, self.ctx.security)

    def _assistant_source_text(self) -> str:
        """Bounded tail — a very long history shouldn't spend tokens on
        anything past what a reply/extraction actually needs."""
        return self._get_source_text()[-8000:]

    def _on_llm_extract(self) -> None:
        client = self._llm_client()
        if client is None:
            return
        self.extract_btn.setEnabled(False)

        def on_error(e):
            self.extract_btn.setEnabled(True)
            QMessageBox.warning(self, "Не получилось", str(e))

        task = fire(client.extract_lead_fields(self._assistant_source_text()),
                    parent=self, on_error=on_error)

        def _apply(t):
            self.extract_btn.setEnabled(True)
            if t.cancelled() or t.exception() is not None:
                return
            fields = t.result()
            for field_key, (cb, edit) in self._extract_rows.items():
                value = fields.get(field_key, "")
                edit.setText(value)
                cb.setChecked(bool(value))
            if not fields:
                QMessageBox.information(self, "Помощник", "В переписке не нашлось значений для полей заявки.")

        task.add_done_callback(_apply)

    def _on_llm_apply_extract(self) -> None:
        updates = {}
        for field_key, (cb, edit) in self._extract_rows.items():
            if cb.isChecked():
                value = edit.text().strip()
                if value:
                    updates[field_key] = value
        if not updates:
            return
        self.ctx.db.set_lead_field(self.lead_id, **updates)
        self._on_changed()

    def _on_llm_summary(self) -> None:
        client = self._llm_client()
        if client is None:
            return
        self.summary_btn.setEnabled(False)

        def on_error(e):
            self.summary_btn.setEnabled(True)
            QMessageBox.warning(self, "Не получилось", str(e))

        task = fire(client.summarize_correspondence(self._assistant_source_text()),
                    parent=self, on_error=on_error)

        def _apply(t):
            self.summary_btn.setEnabled(True)
            if t.cancelled() or t.exception() is not None:
                return
            self.summary_view.setPlainText(t.result())

        task.add_done_callback(_apply)

    def _on_llm_save_summary(self) -> None:
        text = self.summary_view.toPlainText().strip()
        if not text:
            return
        self.ctx.db.add_lead_note(self.lead_id, f"Резюме переписки (LLM-помощник):\n{text}")
        self._on_changed()

    def _on_llm_draft(self) -> None:
        client = self._llm_client()
        if client is None:
            return
        self.draft_btn.setEnabled(False)

        def on_error(e):
            self.draft_btn.setEnabled(True)
            QMessageBox.warning(self, "Не получилось", str(e))

        task = fire(client.draft_reply(self._assistant_source_text()), parent=self, on_error=on_error)

        def _apply(t):
            self.draft_btn.setEnabled(True)
            if t.cancelled() or t.exception() is not None:
                return
            self.draft_view.setPlainText(t.result())

        task.add_done_callback(_apply)

    def _on_llm_save_draft(self) -> None:
        text = self.draft_view.toPlainText().strip()
        if not text:
            return
        bot_id = self.draft_bot_combo.currentData()
        if bot_id is None:
            QMessageBox.information(self, "Помощник", "Нет бота, от имени которого отправить черновик.")
            return
        target = self._draft_target()
        if target is None:
            QMessageBox.information(self, "Помощник", "У заявки нет Telegram-адресата для отправки.")
            return
        # Прямо в outbox_drafts, тем же путём, что и «холодное первое
        # сообщение» из С4 — отправка всё равно идёт только по клику
        # человека на экране «Боты → Черновики» (bot_manager.send_draft),
        # не отсюда: инвариант 6 требует именно этого для любой первой
        # проактивной отправки незнакомцу, а не только для «холодных».
        self.ctx.db.add_draft(bot_id, target, text, reason="черновик LLM-помощника")
        QMessageBox.information(
            self, "Готово",
            "Черновик сохранён — его можно проверить и отправить на экране «Боты», в разделе «Черновики».")

    def _draft_target(self) -> str | None:
        lead = self._lead
        contact = self.ctx.db.get_contact(lead["contact_id"]) if lead["contact_id"] else None
        if lead["username"]:
            return f"@{lead['username']}"
        if lead["tg_user_id"]:
            return str(lead["tg_user_id"])
        if contact and contact["username"]:
            return f"@{contact['username']}"
        if contact:
            return str(contact["telegram_id"])
        return None
