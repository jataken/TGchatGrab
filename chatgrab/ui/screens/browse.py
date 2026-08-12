from __future__ import annotations

from PySide6.QtCore import QTimer, QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from ..context import AppContext
from ..widgets import button, card, h1, muted

_MEDIA_BADGE_LABELS = {
    "photo": "▣ фото приложено", "video": "▣ видео приложено",
    "voice": "▣ голосовое приложено", "document": "▣ документ приложен",
}


class MessageCard(QWidget):
    def __init__(self, ctx: AppContext, row):
        super().__init__()
        self.ctx = ctx
        self.row = row
        frame = card()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(frame)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(6)

        top = QHBoxLayout()
        author = row["sender_display_name"] or "—"
        handle = f"@{row['sender_username']}" if row["sender_username"] else ""
        top.addWidget(QLabel(f"<b>{author}</b>"))
        if handle:
            h = QLabel(handle)
            h.setProperty("class", "faint")
            top.addWidget(h)
        chat = QLabel(row["chat_title"] or "")
        chat.setStyleSheet("color: #b5abfc; font-size: 11.5px;")
        top.addWidget(chat)
        top.addStretch(1)
        date = QLabel(str(row["date"])[:19].replace("T", " "))
        date.setProperty("class", "faint")
        top.addWidget(date)
        lay.addLayout(top)

        text = QLabel(row["text"] or "")
        text.setWordWrap(True)
        text.setStyleSheet("font-size: 13.5px;")
        lay.addWidget(text)

        badges = QHBoxLayout()
        if row["media_path"]:
            media_btn = QPushButton(_MEDIA_BADGE_LABELS.get(row["media_type"], "▣ файл приложен"))
            media_btn.setCursor(Qt.PointingHandCursor)
            media_btn.setStyleSheet(
                "QPushButton { background: rgba(145,132,217,36); color: #d2cefd; border: none; "
                "border-radius: 6px; padding: 3px 9px; font-size: 11.5px; }"
            )
            media_btn.clicked.connect(self._open_media)
            badges.addWidget(media_btn)
        if row["is_forward"]:
            badges.addWidget(muted("переслано" + (f" от {row['forwarded_from']}" if row["forwarded_from"] else "")))
        if row["is_reply"]:
            badges.addWidget(muted(f"ответ на {row['reply_to_message_id']}"))
        link_btn = QPushButton(row["link"] or "")
        link_btn.setCursor(Qt.PointingHandCursor)
        link_btn.setStyleSheet(
            "QPushButton { border: none; background: transparent; color: #9184d9; font-size: 11.5px; }"
            "QPushButton:hover { text-decoration: underline; }"
        )
        link_btn.clicked.connect(self._open_link)
        badges.addWidget(link_btn)
        badges.addStretch(1)
        lay.addLayout(badges)

    def _open_media(self) -> None:
        path = self.ctx.paths.data_dir / self.row["media_path"]
        if path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _open_link(self) -> None:
        if self.row["link"]:
            QDesktopServices.openUrl(QUrl(self.row["link"]))


class BrowseScreen(QWidget):
    PAGE_SIZE = 100

    def __init__(self, ctx: AppContext, navigate):
        super().__init__()
        self.ctx = ctx
        self.navigate = navigate
        self.page = 0
        self.sort_desc = True
        self.total = 0

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        top = QWidget()
        top_lay = QVBoxLayout(top)
        top_lay.setContentsMargins(40, 28, 40, 16)
        top_lay.addWidget(h1("Поиск в собранном"))
        self.hint_label = muted("")
        top_lay.addWidget(self.hint_label)

        filters_row = QHBoxLayout()
        self.query_input = QLineEdit()
        self.query_input.setPlaceholderText("например: глицерин, флаконы, 250 мл")
        filters_row.addWidget(self.query_input, 2)
        self.author_input = QLineEdit()
        self.author_input.setPlaceholderText("автор: имя или @ник")
        filters_row.addWidget(self.author_input, 1)
        self.photos_only_cb = QCheckBox("Только с фото")
        filters_row.addWidget(self.photos_only_cb)
        self.forward_only_cb = QCheckBox("Только пересланные")
        filters_row.addWidget(self.forward_only_cb)
        self.reply_only_cb = QCheckBox("Только ответы")
        filters_row.addWidget(self.reply_only_cb)
        clear_btn = button("Сбросить", "secondary")
        clear_btn.clicked.connect(self._clear_filters)
        filters_row.addWidget(clear_btn)
        top_lay.addLayout(filters_row)

        chip_row = QHBoxLayout()
        self.chat_picker = QComboBox()
        self.chat_picker.addItem("Все чаты", 0)
        chip_row.addWidget(self.chat_picker)
        self.sort_combo = QComboBox()
        self.sort_combo.addItems(["Сначала новые", "Сначала старые"])
        chip_row.addWidget(self.sort_combo)
        chip_row.addStretch(1)
        top_lay.addLayout(chip_row)

        status_row = QHBoxLayout()
        self.count_label = muted("")
        status_row.addWidget(self.count_label)
        status_row.addStretch(1)
        self.export_found_btn = button("Выгрузить найденное", "primary")
        self.export_found_btn.clicked.connect(self._export_found)
        status_row.addWidget(self.export_found_btn)
        top_lay.addLayout(status_row)
        outer.addWidget(top)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.results_container = QWidget()
        self.results_lay = QVBoxLayout(self.results_container)
        self.results_lay.setContentsMargins(40, 0, 40, 10)
        self.results_lay.setSpacing(8)
        self.results_lay.addStretch(1)
        self.scroll.setWidget(self.results_container)
        outer.addWidget(self.scroll, 1)

        pager = QHBoxLayout()
        pager.setContentsMargins(40, 6, 40, 20)
        self.prev_btn = button("← Раньше", "secondary")
        self.prev_btn.clicked.connect(self._prev_page)
        pager.addWidget(self.prev_btn)
        self.page_label = muted("")
        pager.addWidget(self.page_label)
        self.next_btn = button("Позже →", "secondary")
        self.next_btn.clicked.connect(self._next_page)
        pager.addWidget(self.next_btn)
        pager.addStretch(1)
        outer.addLayout(pager)

        for w in (self.query_input, self.author_input):
            w.textChanged.connect(self._debounced_search)
        for cb in (self.photos_only_cb, self.forward_only_cb, self.reply_only_cb):
            cb.toggled.connect(self._on_filter_changed)
        self.chat_picker.currentIndexChanged.connect(self._on_filter_changed)
        self.sort_combo.currentIndexChanged.connect(self._on_sort_change)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.timeout.connect(self._run_search)

        self._last_rows = []

    def on_show(self, **kwargs) -> None:
        self._populate_chat_picker()
        self._run_search()

    def _populate_chat_picker(self) -> None:
        current = self.chat_picker.currentData()
        self.chat_picker.blockSignals(True)
        self.chat_picker.clear()
        self.chat_picker.addItem("Все чаты", 0)
        for chat in self.ctx.db.list_chats():
            self.chat_picker.addItem(chat["title"], chat["chat_id"])
        idx = self.chat_picker.findData(current) if current else 0
        self.chat_picker.setCurrentIndex(max(0, idx))
        self.chat_picker.blockSignals(False)

    def _debounced_search(self) -> None:
        self.page = 0
        self._debounce.start(280)

    def _on_filter_changed(self) -> None:
        self.page = 0
        self._run_search()

    def _clear_filters(self) -> None:
        self.page = 0
        self.query_input.blockSignals(True)
        self.author_input.blockSignals(True)
        self.query_input.clear()
        self.author_input.clear()
        self.query_input.blockSignals(False)
        self.author_input.blockSignals(False)
        self.photos_only_cb.setChecked(False)
        self.forward_only_cb.setChecked(False)
        self.reply_only_cb.setChecked(False)
        self.chat_picker.setCurrentIndex(0)
        self._run_search()

    def _on_sort_change(self, _index: int) -> None:
        self.sort_desc = self.sort_combo.currentIndex() == 0
        self.page = 0
        self._run_search()

    def _prev_page(self) -> None:
        if self.page > 0:
            self.page -= 1
            self._run_search()

    def _next_page(self) -> None:
        if (self.page + 1) * self.PAGE_SIZE < self.total:
            self.page += 1
            self._run_search()

    def current_filters(self) -> dict:
        return {
            "query": self.query_input.text(),
            "author": self.author_input.text(),
            "chat_id": self.chat_picker.currentData() or None,
            "photos_only": self.photos_only_cb.isChecked(),
            "forwards_only": self.forward_only_cb.isChecked(),
            "replies_only": self.reply_only_cb.isChecked(),
        }

    def _run_search(self) -> None:
        filters = self.current_filters()
        rows, total = self.ctx.db.search_messages(
            query=filters["query"], chat_id=filters["chat_id"], author=filters["author"],
            photos_only=filters["photos_only"], forwards_only=filters["forwards_only"],
            replies_only=filters["replies_only"], sort_desc=self.sort_desc,
            page=self.page, page_size=self.PAGE_SIZE,
        )
        self.total = total
        self._last_rows = rows
        self._render_results(rows)

        active = any([filters["query"].strip(), filters["author"].strip(), filters["photos_only"],
                      filters["forwards_only"], filters["replies_only"], filters["chat_id"]])
        self.hint_label.setText(
            "Поиск идёт по тексту в базе — быстрее, чем открывать чаты в Telegram."
            if active else "Показаны последние собранные сообщения по всем чатам."
        )
        if total == 0:
            self.count_label.setText("Ничего не найдено")
        else:
            self.count_label.setText(f"{total:,} сообщений найдено".replace(",", " "))
        self.export_found_btn.setText(f"Выгрузить найденное ({total})" if total else "Выгрузить найденное")

        start = self.page * self.PAGE_SIZE + 1
        end = min(total, start + len(rows) - 1)
        self.page_label.setText(f"{start}–{end} из {total}" if total else "")
        self.prev_btn.setEnabled(self.page > 0)
        self.next_btn.setEnabled(end < total)

    def _render_results(self, rows) -> None:
        while self.results_lay.count() > 1:
            item = self.results_lay.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        for row in rows:
            card_widget = MessageCard(self.ctx, row)
            self.results_lay.insertWidget(self.results_lay.count() - 1, card_widget)

    def _export_found(self) -> None:
        filters = self.current_filters()
        self.navigate("export", search_filters=filters)
