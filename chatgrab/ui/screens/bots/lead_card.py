"""Карточка лида — модальное окно, открываемое из «Заявки».

С2's whole point: a lead used to be a database row you could only nudge
through three statuses. This is where it becomes something a manager
actually works in — edits the business fields, sees why it moved where it
moved, reads what the contact has said, and can't mark it lost without
saying why.

Still a QDialog opened from leads_tab.py's list rather than its own
navigation destination — С3 makes leads a top-level block (see
main_window.py), but the card itself stays a dialog: the list is what
grew filters and a funnel, not this.
"""
from __future__ import annotations

import json

from PySide6.QtCore import QDateTime, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateTimeEdit, QDialog, QFileDialog, QFormLayout, QHBoxLayout,
    QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit,
    QTabWidget, QVBoxLayout, QWidget,
)

from ...context import AppContext
from ...util import fire
from ...widgets import button, hline, label, muted
from ....core import lead as lead_domain
from ....integrations import bitrix
from ....integrations import llm


class LeadCardDialog(QDialog):
    def __init__(self, ctx: AppContext, lead_id: int, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.lead_id = lead_id
        self.setMinimumSize(720, 600)

        outer = QVBoxLayout(self)
        outer.setSpacing(10)

        # ---- header: identity + status ---------------------------------
        head = QHBoxLayout()
        self.title_label = label("", "h1")
        self.title_label.setStyleSheet("font-size: 18px;")
        head.addWidget(self.title_label, 1)
        self.status_pill = muted("")
        head.addWidget(self.status_pill)
        outer.addLayout(head)

        self.meta_label = muted("")
        self.meta_label.setWordWrap(True)
        outer.addWidget(self.meta_label)

        status_row = QHBoxLayout()
        status_row.addWidget(muted("Статус"))
        self.status_combo = QComboBox()
        for status in lead_domain.ALL_STATUSES:
            self.status_combo.addItem(lead_domain.label_for_status(status), status)
        status_row.addWidget(self.status_combo)
        self.reject_reason_combo = QComboBox()
        self.reject_reason_combo.setEditable(True)
        self.reject_reason_combo.addItems(lead_domain.REJECT_REASONS)
        self.reject_reason_combo.setVisible(False)
        status_row.addWidget(self.reject_reason_combo, 1)
        apply_status_btn = button("Применить", "secondary")
        apply_status_btn.clicked.connect(self._on_apply_status)
        status_row.addWidget(apply_status_btn)
        outer.addLayout(status_row)
        self.status_combo.currentIndexChanged.connect(self._on_status_combo_changed)

        # ---- editable business fields -----------------------------------
        form_row = QHBoxLayout()
        form_left = QFormLayout()
        form_left.setSpacing(6)
        self.direction_combo = QComboBox()
        form_left.addRow("Направление", self.direction_combo)
        self.product_input = QLineEdit()
        form_left.addRow("Товар", self.product_input)
        vol_row = QHBoxLayout()
        self.volume_input = QLineEdit()
        vol_row.addWidget(self.volume_input, 1)
        self.unit_input = QLineEdit()
        self.unit_input.setPlaceholderText("ед.")
        self.unit_input.setMaximumWidth(80)
        vol_row.addWidget(self.unit_input)
        form_left.addRow("Объём", vol_row)
        self.deadline_input = QLineEdit()
        self.deadline_input.setPlaceholderText("срок, свободный текст")
        form_left.addRow("Срок", self.deadline_input)
        form_row.addLayout(form_left, 1)

        form_right = QFormLayout()
        form_right.setSpacing(6)
        self.city_input = QLineEdit()
        form_right.addRow("Город", self.city_input)
        self.delivery_input = QLineEdit()
        form_right.addRow("Доставка", self.delivery_input)
        self.phone_input = QLineEdit()
        form_right.addRow("Телефон", self.phone_input)
        self.email_input = QLineEdit()
        form_right.addRow("Email", self.email_input)
        self.manager_input = QLineEdit()
        form_right.addRow("Менеджер", self.manager_input)
        form_row.addLayout(form_right, 1)
        outer.addLayout(form_row)

        save_row = QHBoxLayout()
        save_row.addStretch(1)
        save_fields_btn = button("Сохранить поля", "primary")
        save_fields_btn.clicked.connect(self._on_save_fields)
        save_row.addWidget(save_fields_btn)
        outer.addLayout(save_row)

        # ---- reminder ------------------------------------------------
        reminder_row = QHBoxLayout()
        reminder_row.addWidget(muted("Напоминание"))
        self.reminder_at = QDateTimeEdit()
        self.reminder_at.setCalendarPopup(True)
        self.reminder_at.setDisplayFormat("dd.MM.yyyy HH:mm")
        self.reminder_at.setDateTime(QDateTime.currentDateTime().addSecs(3600))
        reminder_row.addWidget(self.reminder_at)
        self.reminder_text = QLineEdit()
        self.reminder_text.setPlaceholderText("например: перезвонить, уточнить цену")
        reminder_row.addWidget(self.reminder_text, 1)
        set_reminder_btn = button("Поставить", "secondary")
        set_reminder_btn.clicked.connect(self._on_set_reminder)
        reminder_row.addWidget(set_reminder_btn)
        self.clear_reminder_btn = button("Снять", "ghost")
        self.clear_reminder_btn.clicked.connect(self._on_clear_reminder)
        reminder_row.addWidget(self.clear_reminder_btn)
        outer.addLayout(reminder_row)
        self.reminder_hint = muted("")
        outer.addWidget(self.reminder_hint)

        # ---- Bitrix24 (С6) -----------------------------------------------
        bitrix_row = QHBoxLayout()
        self.bitrix_send_btn = button("Отправить в Битрикс24", "secondary")
        self.bitrix_send_btn.clicked.connect(self._on_send_to_bitrix)
        bitrix_row.addWidget(self.bitrix_send_btn)
        self.bitrix_status_label = muted("")
        bitrix_row.addWidget(self.bitrix_status_label, 1)
        outer.addLayout(bitrix_row)

        # ---- tabs: история / переписка / вложения ------------------------
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)

        # История
        history_tab = QWidget()
        history_lay = QVBoxLayout(history_tab)
        self.history_list = QListWidget()
        history_lay.addWidget(self.history_list, 1)
        note_row = QHBoxLayout()
        self.note_input = QLineEdit()
        self.note_input.setPlaceholderText("Заметка — например, договорённость по телефону")
        self.note_input.returnPressed.connect(self._on_add_note)
        note_row.addWidget(self.note_input, 1)
        add_note_btn = button("Добавить", "secondary")
        add_note_btn.clicked.connect(self._on_add_note)
        note_row.addWidget(add_note_btn)
        history_lay.addLayout(note_row)
        self.tabs.addTab(history_tab, "История")

        # Переписка
        corr_tab = QWidget()
        corr_lay = QVBoxLayout(corr_tab)
        self.corr_hint = muted("")
        self.corr_hint.setWordWrap(True)
        corr_lay.addWidget(self.corr_hint)
        self.corr_view = QPlainTextEdit()
        self.corr_view.setReadOnly(True)
        corr_lay.addWidget(self.corr_view, 1)
        self.tabs.addTab(corr_tab, "Переписка")

        # Вложения
        att_tab = QWidget()
        att_lay = QVBoxLayout(att_tab)
        self.attachments_list = QListWidget()
        self.attachments_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.attachments_list.customContextMenuRequested.connect(self._on_attachment_menu)
        att_lay.addWidget(self.attachments_list, 1)
        add_att_btn = button("Прикрепить файл КП…", "secondary")
        add_att_btn.clicked.connect(self._on_add_attachment)
        att_lay.addWidget(add_att_btn)
        self.tabs.addTab(att_tab, "Вложения")

        # Помощник (С9) — опциональный LLM-ассистент, целиком построенный
        # на переписке, уже показанной на вкладке «Переписка» (та же
        # `corr_view`, а не отдельный запрос к базе) — так пользователь
        # всегда видит ровно тот текст, что уйдёт в подсказку. Каждая из
        # трёх кнопок только предлагает: поля появляются в редактируемых
        # строках со снятыми/выставленными галочками, резюме и черновик —
        # в обычных текстовых полях, ничего не пишется в лид/черновики
        # без отдельного клика «Применить»/«Сохранить».
        assist_tab = QWidget()
        assist_lay = QVBoxLayout(assist_tab)
        self.assist_hint = muted("")
        self.assist_hint.setWordWrap(True)
        assist_lay.addWidget(self.assist_hint)

        assist_lay.addWidget(muted("ИЗВЛЕЧЬ ПОЛЯ ИЗ ПЕРЕПИСКИ"))
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

        assist_lay.addWidget(muted("КРАТКОЕ СОДЕРЖАНИЕ ПЕРЕПИСКИ"))
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

        assist_lay.addWidget(muted("ЧЕРНОВИК ОТВЕТА"))
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
        self.tabs.addTab(assist_tab, "Помощник")

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = button("Закрыть", "secondary")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        outer.addLayout(close_row)

        self._populate_directions()
        self.refresh()

    # ---- loading ---------------------------------------------------------
    def _populate_directions(self) -> None:
        self.direction_combo.blockSignals(True)
        self.direction_combo.clear()
        self.direction_combo.addItem("— не указано —", None)
        for direction in self.ctx.db.list_directions():
            self.direction_combo.addItem(direction["name"], direction["id"])
        self.direction_combo.blockSignals(False)

    def refresh(self) -> None:
        lead = self.ctx.db.get_lead(self.lead_id)
        if lead is None:
            self.reject()
            return
        self._lead = lead

        contact = self.ctx.db.get_contact(lead["contact_id"]) if lead["contact_id"] else None
        handle = lead["display_name"] or lead["username"] or (
            f"@{contact['username']}" if contact and contact["username"] else None) or \
            (str(contact["telegram_id"]) if contact else f"заявка №{lead['id']}")
        self.setWindowTitle(f"Заявка — {handle}")
        self.title_label.setText(handle)

        bg, fg, _dot = lead_domain.STATUS_COLORS.get(lead["status"], lead_domain.STATUS_COLORS[lead_domain.NEW])
        self.status_pill.setText(f"●  {lead_domain.label_for_status(lead['status'])}")
        self.status_pill.setStyleSheet(
            f"color: {fg}; background: {bg}; border-radius: 6px; padding: 3px 10px; font-size: 12px;")

        created = str(lead["created_at"])[:16].replace("T", " ")
        meta_bits = [f"создана {created}", lead_domain.label_for_source_type(lead["source_type"])]
        if lead["phone"]:
            meta_bits.append(lead["phone"])
        self.meta_label.setText(" · ".join(meta_bits))

        idx = self.status_combo.findData(lead["status"])
        self.status_combo.blockSignals(True)
        self.status_combo.setCurrentIndex(max(0, idx))
        self.status_combo.blockSignals(False)
        self._on_status_combo_changed(self.status_combo.currentIndex())

        idx = self.direction_combo.findData(lead["direction_id"])
        self.direction_combo.setCurrentIndex(max(0, idx))
        self.product_input.setText(lead["product"] or "")
        self.volume_input.setText(lead["volume"] or "")
        self.unit_input.setText(lead["unit"] or "")
        self.deadline_input.setText(lead["deadline"] or "")
        self.city_input.setText(lead["city"] or "")
        self.delivery_input.setText(lead["delivery"] or "")
        self.phone_input.setText(lead["phone"] or "")
        self.email_input.setText(lead["email"] or "")
        self.manager_input.setText(lead["manager"] or "")

        if lead["next_action_at"]:
            when = str(lead["next_action_at"])[:16].replace("T", " ")
            self.reminder_hint.setText(
                f"Напоминание поставлено на {when}"
                + (f" — {lead['next_action_text']}" if lead["next_action_text"] else ""))
            dt = QDateTime.fromString(str(lead["next_action_at"])[:16], "yyyy-MM-ddTHH:mm")
            if dt.isValid():
                self.reminder_at.setDateTime(dt)
            self.reminder_text.setText(lead["next_action_text"] or "")
        else:
            self.reminder_hint.setText("Напоминание не поставлено.")
        self.clear_reminder_btn.setEnabled(bool(lead["next_action_at"]))

        self._refresh_bitrix_status(lead)
        self._refresh_history()
        self._refresh_correspondence(lead, contact)
        self._refresh_attachments(lead)
        self._refresh_assistant(lead)

    def _refresh_history(self) -> None:
        self.history_list.clear()
        events = self.ctx.db.list_lead_events(self.lead_id)
        if not events:
            self.history_list.addItem("Пока ничего не произошло.")
            return
        for event in reversed(events):  # newest first — that's what you look at
            when = str(event["created_at"])[:16].replace("T", " ")
            source = lead_domain.label_for_event_source(event["source"])
            if event["kind"] == "created":
                text = f"{when} · заявка создана ({source})"
            elif event["kind"] == "status":
                frm = lead_domain.label_for_status(event["from_status"] or "")
                to = lead_domain.label_for_status(event["to_status"] or "")
                text = f"{when} · статус: {frm} → {to} ({source})"
                if event["text"]:
                    text += f"\n    {event['text']}"
            elif event["kind"] == "note":
                text = f"{when} · заметка ({source}): {event['text']}"
            else:
                text = f"{when} · {event['kind']} ({source}){': ' + event['text'] if event['text'] else ''}"
            self.history_list.addItem(text)

    def _refresh_correspondence(self, lead, contact) -> None:
        telegram_id = lead["tg_user_id"] or (contact["telegram_id"] if contact else None)
        if not telegram_id:
            self.corr_hint.setText("У этой заявки нет привязанного Telegram-аккаунта — переписку показать нечем.")
            self.corr_view.setPlainText("")
            return
        messages = self.ctx.db.lead_correspondence(telegram_id)
        if not messages:
            self.corr_hint.setText("Среди собранных сообщений от этого контакта пока ничего нет.")
            self.corr_view.setPlainText("")
            return
        self.corr_hint.setText(f"{len(messages)} сообщений в собранной истории, от новых к старым.")
        lines = []
        for m in reversed(messages):
            when = str(m["date"])[:16].replace("T", " ")
            lines.append(f"[{when}] {m['chat_title'] or m['chat_id']}: {m['text']}")
        self.corr_view.setPlainText("\n".join(lines))
        scrollbar = self.corr_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _refresh_bitrix_status(self, lead) -> None:
        configured = bitrix.get_webhook_url(self.ctx.db, self.ctx.security) is not None
        queue_entry = self.ctx.db.get_crm_queue_entry(self.lead_id)
        if not configured:
            self.bitrix_send_btn.setEnabled(False)
            self.bitrix_status_label.setText("Bitrix24 не настроен — задайте вебхук в «Настройках».")
        elif queue_entry is not None:
            self.bitrix_send_btn.setEnabled(True)
            attempts = queue_entry["attempts"]
            self.bitrix_status_label.setText(
                f"Отправка в очереди (попыток: {attempts})." if attempts else "Отправка в очереди.")
        elif lead["crm_id"]:
            self.bitrix_send_btn.setEnabled(True)
            when = str(lead["crm_synced_at"])[:16].replace("T", " ") if lead["crm_synced_at"] else "?"
            self.bitrix_status_label.setText(f"В Bitrix24: ID {lead['crm_id']} (синхронизировано {when}).")
        else:
            self.bitrix_send_btn.setEnabled(True)
            self.bitrix_status_label.setText("Ещё не отправлялась в Bitrix24.")

    def _on_send_to_bitrix(self) -> None:
        self.ctx.bitrix_sync_service.enqueue(self.lead_id)
        self.refresh()
        # Ставит в очередь и сразу пробует — иначе клик ждал бы до 30
        # секунд следующего фонового тика без всякой видимой причины.
        # Фон остаётся страховкой на случай, если сети сейчас нет.
        task = fire(self.ctx.bitrix_sync_service.tick(), parent=self, on_error=lambda e: None)
        task.add_done_callback(lambda t: self.refresh() if not t.cancelled() else None)

    def _refresh_attachments(self, lead) -> None:
        self.attachments_list.clear()
        try:
            paths = json.loads(lead["attachments"])
        except (json.JSONDecodeError, TypeError):
            paths = []
        for path in paths:
            self.attachments_list.addItem(QListWidgetItem(path))

    def _refresh_assistant(self, lead) -> None:
        enabled = llm.is_enabled(self.ctx.db, self.ctx.security)
        has_text = bool(self.corr_view.toPlainText().strip())
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

    # ---- actions -----------------------------------------------------
    def _on_status_combo_changed(self, _index: int) -> None:
        is_lost = self.status_combo.currentData() == lead_domain.LOST
        self.reject_reason_combo.setVisible(is_lost)
        if is_lost and self._lead["reject_reason"]:
            self.reject_reason_combo.setCurrentText(self._lead["reject_reason"])

    def _on_apply_status(self) -> None:
        new_status = self.status_combo.currentData()
        reason = self.reject_reason_combo.currentText().strip() if new_status == lead_domain.LOST else None
        try:
            self.ctx.db.set_lead_status(self.lead_id, new_status, reject_reason=reason)
        except ValueError as e:
            QMessageBox.information(self, "Не получилось", str(e))
            return
        self.refresh()

    def _on_save_fields(self) -> None:
        self.ctx.db.set_lead_field(
            self.lead_id,
            direction_id=self.direction_combo.currentData(),
            product=self.product_input.text().strip() or None,
            volume=self.volume_input.text().strip() or None,
            unit=self.unit_input.text().strip() or None,
            deadline=self.deadline_input.text().strip() or None,
            city=self.city_input.text().strip() or None,
            delivery=self.delivery_input.text().strip() or None,
            phone=self.phone_input.text().strip() or None,
            email=self.email_input.text().strip() or None,
            manager=self.manager_input.text().strip() or None,
        )
        self.refresh()

    def _on_set_reminder(self) -> None:
        qdt = self.reminder_at.dateTime()
        py_dt = qdt.toPython()
        iso = py_dt.astimezone().isoformat(timespec="seconds")
        text = self.reminder_text.text().strip()
        self.ctx.db.set_lead_field(self.lead_id, next_action_at=iso, next_action_text=text or None)
        self.refresh()

    def _on_clear_reminder(self) -> None:
        self.ctx.db.set_lead_field(self.lead_id, next_action_at=None, next_action_text=None)
        self.refresh()

    def _on_add_note(self) -> None:
        text = self.note_input.text().strip()
        if not text:
            return
        self.ctx.db.add_lead_note(self.lead_id, text)
        self.note_input.clear()
        self._refresh_history()

    def _on_add_attachment(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Файл КП")
        if not path:
            return
        try:
            attachments = json.loads(self._lead["attachments"])
        except (json.JSONDecodeError, TypeError):
            attachments = []
        if path not in attachments:
            attachments.append(path)
        self.ctx.db.set_lead_field(self.lead_id, attachments=attachments)
        self._lead = self.ctx.db.get_lead(self.lead_id)
        self._refresh_attachments(self._lead)

    # ---- LLM-помощник (С9) ---------------------------------------------
    def _llm_client(self):
        return llm.build_client(self.ctx.db, self.ctx.security)

    def _assistant_source_text(self) -> str:
        """Exactly the text the «Переписка» tab already shows — a bounded
        tail of it, so a very long history doesn't spend tokens on
        anything past what a reply/extraction actually needs, and so
        what the model sees is never more than what the user can also
        see right next to it."""
        return self.corr_view.toPlainText()[-8000:]

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
        self.refresh()

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
        self._refresh_history()

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

    def _on_attachment_menu(self, pos) -> None:
        item = self.attachments_list.itemAt(pos)
        if item is None:
            return
        menu = QMenu(self)
        remove_action = menu.addAction("Открепить")
        chosen = menu.exec(self.attachments_list.viewport().mapToGlobal(pos))
        if chosen != remove_action:
            return
        try:
            attachments = json.loads(self._lead["attachments"])
        except (json.JSONDecodeError, TypeError):
            attachments = []
        path = item.text()
        if path in attachments:
            attachments.remove(path)
        self.ctx.db.set_lead_field(self.lead_id, attachments=attachments)
        self._lead = self.ctx.db.get_lead(self.lead_id)
        self._refresh_attachments(self._lead)
