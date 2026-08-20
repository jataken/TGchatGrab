"""П6: полноэкранный разбор непрочитанного с клавиатуры — одна цепочка
на экране, J/K листают вперёд/назад, цифра 1-9 ставит/снимает ярлык с
этой горячей цифрой, E — в архив (и сразу следующая), R — ответить,
L — завести заявку из письма или открыть уже существующую (П9),
/ — выйти и передать фокус поиску на экране «Почта», Esc — просто выйти.

Отдельный модальный диалог, а не пункт навигации: это *режим* поверх
экрана «Почта» (открывается с него и туда же возвращает), а не
самостоятельный раздел — см. PLAN.md, П6.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
)

from ...context import AppContext
from ...util import fire, run_blocking
from ...widgets import button, muted
from .compose import ComposeDialog

_UNLABELLED_SWATCH = "#3a3d4a"


class TriageDialog(QDialog):
    def __init__(self, ctx: AppContext, mailbox_id: int, folder: str | None = None,
                 parent=None, on_search=None):
        super().__init__(parent)
        self.ctx = ctx
        self.mailbox_id = mailbox_id
        self.folder = folder
        self._on_search = on_search
        self.setWindowTitle("Режим разбора")
        self.resize(900, 620)

        self._queue = [
            t["thread_id"] for t in
            self.ctx.db.list_mail_threads(mailbox_id, folder=folder, unread_only=True, limit=1000)
        ]
        self._index = 0

        outer = QVBoxLayout(self)

        self.counter_label = muted("")
        outer.addWidget(self.counter_label)

        self.subject_label = QLabel("")
        self.subject_label.setTextFormat(Qt.PlainText)
        self.subject_label.setWordWrap(True)
        self.subject_label.setStyleSheet("font-size: 18px; font-weight: 600;")
        outer.addWidget(self.subject_label)

        self.from_label = muted("")
        outer.addWidget(self.from_label)

        self.body_label = QLabel("")
        self.body_label.setTextFormat(Qt.PlainText)
        self.body_label.setWordWrap(True)
        self.body_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        outer.addWidget(self.body_label, 1)

        self.label_row = QHBoxLayout()
        outer.addLayout(self.label_row)

        self.status = muted("")
        outer.addWidget(self.status)

        hint = muted(
            "J/K — вперёд/назад · 1–9 — ярлык · E — в архив · "
            "R — ответить · L — заявка · / — поиск · Esc — выйти")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        close_btn = button("Закрыть", "secondary")
        close_btn.clicked.connect(self.accept)
        outer.addWidget(close_btn)

        self._render_current()

    # ---- очередь --------------------------------------------------------
    def _current_thread_id(self) -> int | None:
        if 0 <= self._index < len(self._queue):
            return self._queue[self._index]
        return None

    def _render_current(self) -> None:
        self._clear_label_row()
        thread_id = self._current_thread_id()
        if thread_id is None:
            self.counter_label.setText("Осталось: 0")
            self.subject_label.setText("Непрочитанных больше нет.")
            self.from_label.setText("")
            self.body_label.setText("")
            return
        self.counter_label.setText(f"Осталось: {len(self._queue) - self._index}")
        messages = self.ctx.db.list_thread_messages(thread_id)
        if not messages:
            # Цепочка исчезла (например, все письма перенесены в другой
            # ящик синком с сервера) — пропускаем, не тычась в пустоту.
            self._advance()
            return
        latest = messages[-1]
        self.subject_label.setText(latest["subject"] or "(без темы)")
        who = latest["sender_name"] or latest["sender_address"] or "—"
        self.from_label.setText(who)
        body = (latest["body_text"] or "").strip()
        self.body_label.setText(body[:2000] if body else "(текст ещё не загружен)")
        self._render_label_chips(thread_id)
        self.ctx.db.mark_thread_read(thread_id)

    def _clear_label_row(self) -> None:
        while self.label_row.count():
            item = self.label_row.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def _render_label_chips(self, thread_id: int) -> None:
        applied = {l["id"] for l in self.ctx.db.list_labels_for_thread(thread_id)}
        for lb in self.ctx.db.list_mail_labels(self.mailbox_id):
            title = f"{lb['hotkey']} {lb['name']}" if lb["hotkey"] else lb["name"]
            btn = QPushButton(title)
            btn.setCheckable(True)
            btn.setChecked(lb["id"] in applied)
            color = lb["color"] if lb["id"] in applied else _UNLABELLED_SWATCH
            btn.setStyleSheet(
                f"QPushButton {{ background-color: {color}; color: white; border: none; "
                f"border-radius: 8px; padding: 3px 10px; }}")
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _c, lid=lb["id"]: self._toggle_label(lid))
            self.label_row.addWidget(btn)
        self.label_row.addStretch(1)

    def _advance(self) -> None:
        if self._index < len(self._queue):
            self._index += 1
        self._render_current()

    def _retreat(self) -> None:
        if self._index > 0:
            self._index -= 1
        self._render_current()

    # ---- действия ---------------------------------------------------------
    def _toggle_label(self, label_id: int) -> None:
        thread_id = self._current_thread_id()
        if thread_id is None:
            return
        applied = {l["id"] for l in self.ctx.db.list_labels_for_thread(thread_id)}
        on = label_id not in applied

        async def _run():
            return await run_blocking(self.ctx.mail_service.set_thread_label, thread_id, label_id, on)

        def on_done():
            if self._current_thread_id() == thread_id:
                self._clear_label_row()
                self._render_label_chips(thread_id)

        fire(_run(), parent=self, on_error=lambda e: self.status.setText(f"Не получилось: {e}"), on_done=on_done)

    def _on_archive(self) -> None:
        thread_id = self._current_thread_id()
        if thread_id is None:
            return

        async def _run():
            return await run_blocking(self.ctx.mail_service.archive_thread, thread_id)

        def on_done():
            self._advance()

        fire(_run(), parent=self, on_error=lambda e: self.status.setText(f"Не получилось: {e}"), on_done=on_done)

    def _on_reply(self) -> None:
        thread_id = self._current_thread_id()
        if thread_id is None:
            return
        messages = self.ctx.db.list_thread_messages(thread_id)
        if not messages:
            return
        draft_id = self.ctx.mail_service.start_reply_draft(messages[-1]["id"], reply_all=False)
        if draft_id is None:
            return
        ComposeDialog(self.ctx, draft_id, parent=self).exec()

    def _on_slash(self) -> None:
        self.accept()
        if self._on_search:
            self._on_search()

    def _on_lead(self) -> None:
        # П9: same "already linked → open the lead, else create one"
        # branch as MessagePane._on_lead_clicked, just reached from the
        # keyboard on this dialog's current thread instead of a mouse
        # click on a message row.
        thread_id = self._current_thread_id()
        if thread_id is None:
            return
        thread = self.ctx.db.get_mail_thread(thread_id)
        if thread is not None and thread["lead_id"]:
            from ..bots.lead_card import LeadCardDialog
            LeadCardDialog(self.ctx, thread["lead_id"], parent=self).exec()
            return
        messages = self.ctx.db.list_thread_messages(thread_id)
        if not messages:
            return
        from .lead_create_dialog import MailLeadDialog
        MailLeadDialog(self.ctx, messages[-1]["id"], parent=self).exec()

    # ---- клавиатура -------------------------------------------------------
    def keyPressEvent(self, event) -> None:
        key = event.key()
        text = event.text()
        if key == Qt.Key_J:
            self._advance()
        elif key == Qt.Key_K:
            self._retreat()
        elif key == Qt.Key_E:
            self._on_archive()
        elif key == Qt.Key_R:
            self._on_reply()
        elif key == Qt.Key_L:
            self._on_lead()
        elif key == Qt.Key_Slash:
            self._on_slash()
        elif text.isdigit() and text != "0":
            label = next((l for l in self.ctx.db.list_mail_labels(self.mailbox_id)
                          if l["hotkey"] == int(text)), None)
            if label is not None:
                self._toggle_label(label["id"])
        else:
            super().keyPressEvent(event)
