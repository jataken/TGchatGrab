"""П10: «Почта → Вложения» — every attachment across every synced
message, one list, searchable by filename/sender/date — the "менеджер
вложений" from the checklist. Read-only: opening one reuses the same
AttachmentViewerDialog the reading screen already opens from a single
message (П3), so a viewer built once still only exists once.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLineEdit,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ...context import AppContext
from ...format import human_size, short_dt
from ...widgets import card, h1, muted

_EXT_ALL = "Любой тип"


class MailAttachmentsScreen(QWidget):
    def __init__(self, ctx: AppContext, navigate):
        super().__init__()
        self.ctx = ctx
        self.navigate = navigate

        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 28, 40, 32)
        outer.addWidget(h1("Вложения"))
        self.summary_label = muted("")
        outer.addWidget(self.summary_label)
        outer.addSpacing(14)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Поиск по имени файла…")
        self.search_input.textChanged.connect(lambda _t: self.refresh())
        filter_row.addWidget(self.search_input, 1)
        self.sender_input = QLineEdit()
        self.sender_input.setPlaceholderText("Отправитель…")
        self.sender_input.textChanged.connect(lambda _t: self.refresh())
        filter_row.addWidget(self.sender_input, 1)
        self.mailbox_combo = QComboBox()
        self.mailbox_combo.currentIndexChanged.connect(lambda _i: self.refresh())
        filter_row.addWidget(self.mailbox_combo)
        self.ext_combo = QComboBox()
        self.ext_combo.currentIndexChanged.connect(lambda _i: self.refresh())
        filter_row.addWidget(self.ext_combo)
        outer.addLayout(filter_row)
        outer.addSpacing(10)

        list_card = card()
        list_lay = QVBoxLayout(list_card)
        list_lay.setContentsMargins(16, 12, 16, 14)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Файл", "От кого", "Тема письма", "Дата", "Размер"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.cellDoubleClicked.connect(self._on_open)
        list_lay.addWidget(self.table)
        outer.addWidget(list_card, 1)

    def on_show(self, **kwargs) -> None:
        self._populate_mailboxes()
        self.refresh()

    def _populate_mailboxes(self) -> None:
        current = self.mailbox_combo.currentData()
        self.mailbox_combo.blockSignals(True)
        self.mailbox_combo.clear()
        self.mailbox_combo.addItem("Любой ящик", None)
        for mb in self.ctx.db.list_mailboxes():
            self.mailbox_combo.addItem(mb["address"], mb["id"])
        idx = self.mailbox_combo.findData(current)
        self.mailbox_combo.setCurrentIndex(max(0, idx))
        self.mailbox_combo.blockSignals(False)

    def _rebuild_ext_filter(self, rows) -> None:
        current = self.ext_combo.currentData()
        exts = sorted({Path(r["filename"]).suffix.lower() for r in rows if Path(r["filename"]).suffix})
        self.ext_combo.blockSignals(True)
        self.ext_combo.clear()
        self.ext_combo.addItem(_EXT_ALL, None)
        for ext in exts:
            self.ext_combo.addItem(ext, ext)
        idx = self.ext_combo.findData(current)
        self.ext_combo.setCurrentIndex(max(0, idx))
        self.ext_combo.blockSignals(False)

    def refresh(self) -> None:
        rows = self.ctx.db.list_all_mail_attachments(
            mailbox_id=self.mailbox_combo.currentData(),
            query=self.search_input.text().strip() or None,
            sender=self.sender_input.text().strip() or None,
        )
        # Тип фильтруется на стороне экрана, не в самом запросе — список
        # расширений строится из того же среза, что уже пришёл по
        # остальным фильтрам, а не заново по всей базе (см. docstring
        # list_all_mail_attachments — content_type сервером не всегда
        # заполняется, только имя файла — надёжный источник расширения).
        self._rebuild_ext_filter(rows)
        ext = self.ext_combo.currentData()
        if ext:
            rows = [r for r in rows if Path(r["filename"]).suffix.lower() == ext]

        self.summary_label.setText(f"{len(rows)} вложений" if rows else "Вложений пока нет.")
        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            item = QTableWidgetItem(row["filename"])
            item.setData(Qt.UserRole, row["id"])
            self.table.setItem(i, 0, item)
            who = row["sender_name"] or row["sender_address"] or "—"
            self.table.setItem(i, 1, QTableWidgetItem(who))
            self.table.setItem(i, 2, QTableWidgetItem(row["message_subject"] or "(без темы)"))
            self.table.setItem(i, 3, QTableWidgetItem(short_dt(row["message_date"]) if row["message_date"] else "—"))
            self.table.setItem(i, 4, QTableWidgetItem(human_size(row["size_bytes"] or 0)))
            self.table.setRowHeight(i, 32)

        self.table.resizeColumnsToContents()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(2, QHeaderView.Stretch)

    def _on_open(self, row: int, _col: int) -> None:
        item = self.table.item(row, 0)
        if item is None:
            return
        attachment_id = item.data(Qt.UserRole)
        attachment = self.ctx.db.get_mail_attachment(attachment_id)
        if attachment is None:
            return
        from .attachment_view import AttachmentViewerDialog
        AttachmentViewerDialog(self.ctx, attachment, parent=self).exec()
