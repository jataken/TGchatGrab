"""П5: writing mail — ComposeDialog (new/reply/reply-all/forward, drafts
autosaved locally and synced to the server's Drafts folder) and
SendConfirmDialog, the "нельзя проскочить" screen that is, on principle,
the *only* code path that calls MailService.send_draft(). See that
method's docstring in services/mail_service.py for the other half of
how П-1 ("никакой автоматической отправки") is enforced structurally.
"""
from __future__ import annotations

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QComboBox, QCompleter, QDialog, QFileDialog, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QVBoxLayout, QWidget,
)

from ...context import AppContext
from ...format import human_size
from ...util import fire, run_blocking
from ...widgets import FieldRow, button, muted
from ....core import mail_compose
from ....integrations import llm as llm_integration

AUTOSAVE_DEBOUNCE_MS = 1500


class ComposeDialog(QDialog):
    def __init__(self, ctx: AppContext, draft_id: int, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.draft_id = draft_id
        self.mailbox_id = ctx.db.get_mail_draft(draft_id)["mailbox_id"]
        self.setWindowTitle("Письмо")
        self.resize(720, 640)

        outer = QVBoxLayout(self)

        row1 = QHBoxLayout()
        self.identity_combo = QComboBox()
        self._load_identities()
        self.identity_combo.currentIndexChanged.connect(self._on_identity_changed)
        row1.addWidget(muted("От:"))
        row1.addWidget(self.identity_combo, 1)
        outer.addLayout(row1)

        self.to_field = FieldRow("Кому", placeholder="client@example.com, ещё@example.com")
        outer.addWidget(self.to_field)
        self.cc_field = FieldRow("Копия", placeholder="необязательно")
        outer.addWidget(self.cc_field)
        # П10: address-book autocomplete — matches a whole field's text
        # against "Имя <адрес>"/адрес, not each comma-separated recipient
        # individually (QCompleter has no built-in notion of "the word
        # under the cursor within a delimited list"; a real per-recipient
        # completer would need its own text-editing logic, more than this
        # session's own address-book checklist item asks for). Still
        # useful for the common case — a single recipient, or completing
        # the *last* one typed.
        self._wire_contact_completer(self.to_field.input)
        self._wire_contact_completer(self.cc_field.input)
        self.subject_field = FieldRow("Тема")
        outer.addWidget(self.subject_field)

        self.body_edit = QPlainTextEdit()
        outer.addWidget(self.body_edit, 1)

        att_row = QHBoxLayout()
        att_row.addWidget(muted("Вложения:"))
        add_att_btn = button("Добавить файл…", "ghost")
        add_att_btn.clicked.connect(self._on_add_attachment)
        att_row.addWidget(add_att_btn)
        att_row.addStretch(1)
        outer.addLayout(att_row)
        self.attachment_list = QListWidget()
        self.attachment_list.setMaximumHeight(90)
        outer.addWidget(self.attachment_list)
        self.attachment_hint = muted("")
        outer.addWidget(self.attachment_hint)

        self.status_label = muted("")
        outer.addWidget(self.status_label)

        btn_row = QHBoxLayout()
        if llm_integration.is_enabled(ctx.db, ctx.security) and ctx.db.get_mail_draft(draft_id)["in_reply_to_message_id"]:
            llm_btn = button("Черновик от помощника", "ghost")
            llm_btn.clicked.connect(self._on_llm_draft)
            btn_row.addWidget(llm_btn)
        btn_row.addStretch(1)
        close_btn = button("Закрыть", "secondary")
        close_btn.clicked.connect(self._on_close)
        btn_row.addWidget(close_btn)
        self.send_btn = button("Отправить", "primary")
        self.send_btn.clicked.connect(self._on_send_clicked)
        btn_row.addWidget(self.send_btn)
        outer.addLayout(btn_row)

        self._load_draft()
        self._refresh_attachments()

        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.timeout.connect(self._autosave)
        for field in (self.to_field.input, self.cc_field.input, self.subject_field.input):
            field.textChanged.connect(self._schedule_autosave)
        self.body_edit.textChanged.connect(self._schedule_autosave)

    # ---- загрузка ------------------------------------------------------
    def _wire_contact_completer(self, line_edit) -> None:
        contacts = self.ctx.db.list_mail_contacts()
        options = [f"{c['display_name']} <{c['address']}>" if c["display_name"] else c["address"]
                   for c in contacts]
        completer = QCompleter(options, line_edit)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        line_edit.setCompleter(completer)

    def _load_identities(self) -> None:
        self.identity_combo.blockSignals(True)
        self.identity_combo.clear()
        identities = self.ctx.db.list_mail_identities(self.mailbox_id)
        if not identities:
            mailbox = self.ctx.db.get_mailbox(self.mailbox_id)
            self.identity_combo.addItem(mailbox["address"] if mailbox else "", None)
        for identity in identities:
            self.identity_combo.addItem(f"{identity['display_name']} <{identity['from_address']}>", identity["id"])
        self.identity_combo.blockSignals(False)

    def _load_draft(self) -> None:
        import json
        draft = self.ctx.db.get_mail_draft(self.draft_id)
        self.to_field.set_text(", ".join(json.loads(draft["to_addresses"] or "[]")))
        self.cc_field.set_text(", ".join(json.loads(draft["cc_addresses"] or "[]")))
        self.subject_field.set_text(draft["subject"] or "")
        self.body_edit.setPlainText(draft["body_text"] or "")
        if draft["author"] == "assistant":
            self.status_label.setText("Черновик составлен помощником — проверьте перед отправкой.")
        if draft["identity_id"]:
            idx = self.identity_combo.findData(draft["identity_id"])
            if idx >= 0:
                self.identity_combo.setCurrentIndex(idx)

    def _refresh_attachments(self) -> None:
        self.attachment_list.clear()
        attachments = self.ctx.db.list_mail_draft_attachments(self.draft_id)
        total = mail_compose.total_attachment_size([a["size_bytes"] for a in attachments])
        for att in attachments:
            size_text = human_size(att["size_bytes"])
            item = QListWidgetItem(f"{att['filename']}" + (f"  ({size_text})" if size_text else ""))
            item.setData(Qt.UserRole, att["id"])
            self.attachment_list.addItem(item)
        if total > mail_compose.ATTACHMENT_WARN_BYTES:
            self.attachment_hint.setText(
                f"⚠ Суммарный размер вложений — {human_size(total)}, больше 20 МБ: "
                f"часть почтовых серверов может отклонить письмо.")
        else:
            self.attachment_hint.setText("")

    # ---- автосохранение (П5) ----------------------------------------------
    def _schedule_autosave(self) -> None:
        self._autosave_timer.start(AUTOSAVE_DEBOUNCE_MS)

    def _autosave(self) -> None:
        self._save_local()
        self.status_label.setText("Черновик сохранён.")

    def _save_local(self) -> None:
        identity_id = self.identity_combo.currentData()
        self.ctx.db.update_mail_draft(
            self.draft_id,
            identity_id=identity_id,
            to_addresses=_split_addresses(self.to_field.text()),
            cc_addresses=_split_addresses(self.cc_field.text()),
            subject=self.subject_field.text(),
            body_text=self.body_edit.toPlainText(),
        )

    def _on_identity_changed(self, _index: int) -> None:
        self._schedule_autosave()

    # ---- вложения --------------------------------------------------------
    def _on_add_attachment(self) -> None:
        paths_, _ = QFileDialog.getOpenFileNames(self, "Добавить вложения")
        if not paths_:
            return
        for p in paths_:
            self.ctx.mail_service.add_draft_attachment(self.draft_id, p)
        self._refresh_attachments()

    # ---- помощник (П5, П-1: только в черновик) ----------------------------
    def _on_llm_draft(self) -> None:
        client = llm_integration.build_client(self.ctx.db, self.ctx.security)
        if client is None:
            return
        draft = self.ctx.db.get_mail_draft(self.draft_id)
        message = self.ctx.db.get_mail_message(draft["in_reply_to_message_id"])
        if message is None:
            return
        source_text = message["body_text"] or message["subject"] or ""
        self.status_label.setText("Спрашиваю помощника…")

        def on_error(e):
            self.status_label.setText(f"Не получилось: {e}")

        task = fire(client.draft_reply(source_text), parent=self, on_error=on_error)

        def _apply(t):
            if t.cancelled() or t.exception() is not None:
                return
            generated = t.result()
            self.body_edit.setPlainText(f"{generated}\n\n{self._quoted_tail()}")
            self.status_label.setText("Черновик от помощника вставлен — проверьте перед отправкой.")

        task.add_done_callback(_apply)

    def _quoted_tail(self) -> str:
        current = self.body_edit.toPlainText()
        marker = "\n\n--\n"
        idx = current.find(marker)
        return current[idx:].lstrip("\n") if idx != -1 else ""

    # ---- отправка (П-1: только через подтверждение) ------------------------
    def _on_send_clicked(self) -> None:
        self._save_local()
        to = _split_addresses(self.to_field.text())
        if not to:
            QMessageBox.warning(self, "Нет получателя", "Укажите хотя бы одного получателя.")
            return
        mailbox = self.ctx.db.get_mailbox(self.mailbox_id)
        if not mailbox["smtp_host"]:
            QMessageBox.warning(
                self, "Нет SMTP-сервера",
                "Для этого ящика не задан сервер исходящей почты — укажите его в настройках ящика.")
            return
        dlg = SendConfirmDialog(self.ctx, self.draft_id, parent=self)
        if dlg.exec() == QDialog.Accepted:
            self.accept()

    def _on_close(self) -> None:
        self.reject()  # closeEvent (ниже) делает сохранение и синхронизацию

    def closeEvent(self, event) -> None:  # noqa: N802 — имя метода задано Qt
        draft = self.ctx.db.get_mail_draft(self.draft_id)
        if draft is not None and draft["sent_at"] is None:
            # Письмо уже отправлено — send_draft() сам убрал серверную
            # копию черновика и пометил sent_at; досохранять и
            # пересинхронизировать здесь уже нечего, и уж точно не стоит
            # заново создавать копию в Drafts для письма, которого там
            # больше нет по замыслу.
            self._save_local()

            async def _sync():
                return await run_blocking(self.ctx.mail_service.sync_draft_to_server, self.draft_id)

            fire(_sync(), parent=self, on_error=lambda e: None)
        super().closeEvent(event)


def _split_addresses(text: str) -> list[str]:
    return [a.strip() for a in text.replace(";", ",").split(",") if a.strip()]


class SendConfirmDialog(QDialog):
    """The screen the checklist calls "нельзя проскочить" — every field
    a person would want to double-check before a message becomes
    irreversible, plus the two warnings the checklist names by name:
    more than five recipients (a misdirected "ответить всем"), and body
    text that talks about an attachment this draft doesn't have."""

    def __init__(self, ctx: AppContext, draft_id: int, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.draft_id = draft_id
        self.setWindowTitle("Проверьте перед отправкой")
        self.resize(520, 420)

        draft = ctx.db.get_mail_draft(draft_id)
        import json
        to = json.loads(draft["to_addresses"] or "[]")
        cc = json.loads(draft["cc_addresses"] or "[]")
        attachments = ctx.db.list_mail_draft_attachments(draft_id)

        outer = QVBoxLayout(self)
        outer.addWidget(_kv("Кому", ", ".join(to) or "—"))
        if cc:
            outer.addWidget(_kv("Копия", ", ".join(cc)))
        outer.addWidget(_kv("Тема", draft["subject"] or "(без темы)"))
        if attachments:
            names = ", ".join(f"{a['filename']} ({human_size(a['size_bytes'])})" for a in attachments)
            outer.addWidget(_kv("Вложения", names))

        preview = (draft["body_text"] or "").strip().splitlines()
        preview_text = "\n".join(preview[:6])
        preview_label = QLabel(preview_text or "(пусто)")
        preview_label.setTextFormat(Qt.PlainText)
        preview_label.setWordWrap(True)
        preview_label.setProperty("class", "muted")
        outer.addWidget(muted("Начало письма:"))
        outer.addWidget(preview_label)

        recipients_count = mail_compose.total_recipients(to, cc)
        if recipients_count > mail_compose.MANY_RECIPIENTS_THRESHOLD:
            outer.addWidget(_warning(
                f"⚠ Получателей: {recipients_count} — больше {mail_compose.MANY_RECIPIENTS_THRESHOLD}. "
                f"Похоже на «ответить всем» по рассылке — точно всем нужно?"))
        if mail_compose.mentions_attachment(draft["body_text"] or "") and not attachments:
            outer.addWidget(_warning(
                "⚠ В тексте упоминается вложение, но к письму ничего не прикреплено."))

        outer.addStretch(1)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel_btn = button("Отмена", "secondary")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        self.confirm_btn = button("Отправить", "primary")
        self.confirm_btn.clicked.connect(self._on_confirm)
        btn_row.addWidget(self.confirm_btn)
        outer.addLayout(btn_row)

    def _on_confirm(self) -> None:
        self.confirm_btn.setEnabled(False)

        async def _run():
            return await run_blocking(self.ctx.mail_service.send_draft, self.draft_id)

        def on_error(e):
            self.confirm_btn.setEnabled(True)
            QMessageBox.warning(self, "Не удалось отправить", str(e))

        def on_done():
            self.accept()

        fire(_run(), parent=self, on_error=on_error, on_done=on_done)


class DraftsListDialog(QDialog):
    """Not in the checklist by name, but implied by "автосохранение
    черновика" — without some way back to a saved draft, autosave has
    nothing to resume into. Click a row, it reopens the same
    ComposeDialog exactly where autosave left it."""

    def __init__(self, ctx: AppContext, mailbox_id: int, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.mailbox_id = mailbox_id
        self.setWindowTitle("Черновики")
        self.resize(480, 420)

        outer = QVBoxLayout(self)
        self.list_widget = QListWidget()
        self.list_widget.itemDoubleClicked.connect(self._on_open)
        outer.addWidget(self.list_widget, 1)
        hint = muted("Дважды кликните, чтобы продолжить редактирование.")
        outer.addWidget(hint)
        close_btn = button("Закрыть", "secondary")
        close_btn.clicked.connect(self.accept)
        outer.addWidget(close_btn)

        self._refresh()

    def _refresh(self) -> None:
        self.list_widget.clear()
        drafts = self.ctx.db.list_mail_drafts(self.mailbox_id)
        if not drafts:
            self.list_widget.addItem("Пока ни одного черновика.")
            return
        for draft in drafts:
            import json
            to = ", ".join(json.loads(draft["to_addresses"] or "[]")) or "(без получателя)"
            marker = " · от помощника" if draft["author"] == "assistant" else ""
            text = f"{draft['subject'] or '(без темы)'}{marker}\n{to}"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, draft["id"])
            self.list_widget.addItem(item)

    def _on_open(self, item: QListWidgetItem) -> None:
        draft_id = item.data(Qt.UserRole)
        if draft_id is None:
            return
        ComposeDialog(self.ctx, draft_id, parent=self).exec()
        self._refresh()


def _kv(key: str, value: str) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    label = QLabel(key + ":")
    label.setProperty("class", "muted")
    label.setFixedWidth(70)
    lay.addWidget(label)
    value_label = QLabel(value)
    value_label.setTextFormat(Qt.PlainText)
    value_label.setWordWrap(True)
    lay.addWidget(value_label, 1)
    return w


def _warning(text: str) -> QWidget:
    lbl = QLabel(text)
    lbl.setTextFormat(Qt.PlainText)
    lbl.setWordWrap(True)
    lbl.setStyleSheet("color: #e0a13a;")
    return lbl
