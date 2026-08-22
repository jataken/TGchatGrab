from __future__ import annotations

from PySide6.QtCore import (
    Property, QEasingCurve, QPropertyAnimation, QTimer, QUrl, Qt,
)
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QGraphicsOpacityEffect, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from .. import theme
from ..context import AppContext
from ..widgets import Card, TabletCheckBox, button, chip, h1, label, muted
from ...core import lead as lead_domain
from .bots.lead_card import LeadCardDialog

_MEDIA_BADGE_LABELS = {
    "photo": "▣ фото приложено", "video": "▣ видео приложено",
    "voice": "▣ голосовое приложено", "document": "▣ документ приложен",
}


class MessageCard(Card):
    """design-brief.md §4.5 — a `Card` with a 2px ACCENT_700 left stripe
    (10px top/bottom inset): author/handle/chat/date header row, plain-text
    message body, then a badges row (media/forward/reply/link/lead)."""

    def __init__(self, ctx: AppContext, row):
        super().__init__(stripe_color=theme.ACCENT_700)
        self.ctx = ctx
        self.row = row
        self._base_top = 12
        self._offset = 0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 12, 14, 12)
        lay.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(8)
        author_name = row["sender_display_name"] or "—"
        handle = f"@{row['sender_username']}" if row["sender_username"] else "—"
        # Author, chat title and message text below all come straight from
        # Telegram — any sender/admin controls them. QLabel auto-detects
        # rich text, so without an explicit PlainText format a name or
        # message that merely looks like a tag would render as real HTML.
        # Bold styling for the author is done via QSS, not an f-string
        # "<b>" wrapper, so there's no interpolated markup to reason about.
        author_label = QLabel(author_name)
        author_label.setTextFormat(Qt.PlainText)
        author_label.setStyleSheet("font-weight: 600; font-size: 13px;")
        top.addWidget(author_label)
        handle_label = QLabel(handle)
        handle_label.setTextFormat(Qt.PlainText)
        handle_label.setStyleSheet(
            f"font-family: {theme.FONT_MONO}; font-size: 10.5px; color: {theme.TEXT_FAINT};"
        )
        top.addWidget(handle_label)
        chat = QLabel(row["chat_title"] or "")
        chat.setTextFormat(Qt.PlainText)
        chat.setStyleSheet(f"color: {theme.ACCENT_400}; font-size: 11.5px;")
        top.addWidget(chat)
        top.addStretch(1)
        date = QLabel(str(row["date"])[:16].replace("T", " "))
        date.setTextFormat(Qt.PlainText)
        date.setStyleSheet(
            f"font-family: {theme.FONT_MONO}; font-size: 10.5px; color: {theme.TEXT_FAINT};"
        )
        top.addWidget(date)
        lay.addLayout(top)

        text = QLabel(row["text"] or "")
        text.setTextFormat(Qt.PlainText)
        text.setWordWrap(True)
        text.setStyleSheet("font-size: 13.5px;")
        lay.addWidget(text)

        badges = QHBoxLayout()
        badges.setSpacing(8)
        if row["media_path"]:
            media_btn = QPushButton(_MEDIA_BADGE_LABELS.get(row["media_type"], "▣ файл приложен"))
            media_btn.setCursor(Qt.PointingHandCursor)
            media_btn.setStyleSheet(
                f"QPushButton {{ background: {theme.OVERLAY_ACCENT_ACTIVE}; color: {theme.ACCENT_200}; "
                "border: none; border-radius: 6px; padding: 3px 9px; font-size: 11.5px; }"
            )
            media_btn.clicked.connect(self._open_media)
            badges.addWidget(media_btn)
        if row["is_forward"]:
            fw = label("переслано" + (f" от {row['forwarded_from']}" if row["forwarded_from"] else ""))
            fw.setStyleSheet(f"color: {theme.TEXT_FAINT}; font-size: 11px;")
            badges.addWidget(fw)
        if row["is_reply"]:
            rp = label(f"ответ на {row['reply_to_message_id']}")
            rp.setStyleSheet(f"color: {theme.TEXT_FAINT}; font-size: 11px;")
            badges.addWidget(rp)
        badges.addStretch(1)
        link_btn = QPushButton(row["link"] or "")
        link_btn.setCursor(Qt.PointingHandCursor)
        link_btn.setStyleSheet(
            f"QPushButton {{ border: none; background: transparent; color: {theme.ACCENT_600}; "
            f"font-family: {theme.FONT_MONO}; font-size: 10.5px; }}"
            f"QPushButton:hover {{ color: {theme.ACCENT_400}; }}"
        )
        link_btn.clicked.connect(self._open_link)
        badges.addWidget(link_btn)
        lead_btn = QPushButton("Создать лид")
        lead_btn.setCursor(Qt.PointingHandCursor)
        lead_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.OVERLAY_ACCENT_ACTIVE}; color: {theme.ACCENT_200}; "
            "border: none; border-radius: 6px; padding: 3px 9px; font-size: 11.5px; }"
        )
        lead_btn.clicked.connect(self._on_create_lead)
        badges.addWidget(lead_btn)
        lay.addLayout(badges)

    def _on_create_lead(self) -> None:
        row = self.row
        lead_id = self.ctx.db.add_lead(
            None, None, {"text": row["text"] or ""}, status=lead_domain.NEW,
            tg_user_id=row["sender_id"], username=row["sender_username"],
            display_name=row["sender_display_name"],
            source_chat_id=row["chat_id"], source_type=lead_domain.SOURCE_TYPE_CHAT,
            event_source=lead_domain.EVENT_SOURCE_MANUAL,
        )
        LeadCardDialog(self.ctx, lead_id, parent=self).exec()

    def _open_media(self) -> None:
        path = self.ctx.paths.data_dir / self.row["media_path"]
        if path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _open_link(self) -> None:
        if self.row["link"]:
            QDesktopServices.openUrl(QUrl(self.row["link"]))

    def _get_offset(self) -> int:
        return self._offset

    def _set_offset(self, value: int) -> None:
        self._offset = value
        self.layout().setContentsMargins(18, self._base_top + value, 14, 12)

    # Qt-property the entrance animation drives — animating this widget's
    # own `pos()` would fight the parent QVBoxLayout on every layout pass
    # (same reasoning as widgets.py's `_LogRow.content_offset`), so the
    # "slide up" is a shrinking top margin instead.
    content_offset = Property(int, _get_offset, _set_offset)


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
        top_lay.setSpacing(12)

        head_row = QHBoxLayout()
        head_col = QVBoxLayout()
        head_col.setSpacing(2)
        head_col.addWidget(h1("Собранное"))
        self.hint_label = muted("")
        head_col.addWidget(self.hint_label)
        head_row.addLayout(head_col)
        head_row.addStretch(1)
        self.export_found_btn = button("Выгрузить найденное", "primary")
        self.export_found_btn.clicked.connect(self._export_found)
        head_row.addWidget(self.export_found_btn, alignment=Qt.AlignVCenter)
        top_lay.addLayout(head_row)

        # «Карточка фильтров» (design-brief.md §4.5): запрос/автор/сортировка,
        # затем ряд чипов чата (та же чип-схема, что уже принята для выбора
        # чата в других экранах этой сессии — collect.py/coбранное всегда
        # так и делали, брифа «селект чата» здесь не заводим отдельно),
        # затем три чекбокса-таблетки + счётчик + сброс.
        filters_card = Card()
        filters_lay = QVBoxLayout(filters_card)
        filters_lay.setContentsMargins(14, 12, 14, 12)
        filters_lay.setSpacing(10)

        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self.query_input = QLineEdit()
        self.query_input.setPlaceholderText("например: глицерин, флаконы, 250 мл")
        self.query_input.setStyleSheet(f"background: {theme.SURFACE_INPUT};")
        row1.addWidget(self.query_input, 2)
        self.author_input = QLineEdit()
        self.author_input.setPlaceholderText("автор: имя или @ник")
        self.author_input.setStyleSheet(f"background: {theme.SURFACE_INPUT};")
        row1.addWidget(self.author_input, 1)
        self.sort_combo = QComboBox()
        self.sort_combo.addItems(["Сначала новые", "Сначала старые"])
        self.sort_combo.setFixedWidth(150)
        row1.addWidget(self.sort_combo)
        filters_lay.addLayout(row1)

        chip_row = QHBoxLayout()
        chip_scroll = QScrollArea()
        chip_scroll.setWidgetResizable(True)
        chip_scroll.setFrameShape(QScrollArea.NoFrame)
        chip_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        chip_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        chip_scroll.setFixedHeight(36)
        chip_host = QWidget()
        self.chip_lay = QHBoxLayout(chip_host)
        self.chip_lay.setContentsMargins(0, 0, 0, 0)
        self.chip_lay.setSpacing(6)
        self.chip_lay.addStretch(1)
        chip_scroll.setWidget(chip_host)
        chip_row.addWidget(chip_scroll, 1)
        self.chat_chip_group = QButtonGroup(self)
        self.chat_chip_group.setExclusive(True)
        self.chat_chips: dict[int, QPushButton] = {}
        self.selected_chat_id = 0
        filters_lay.addLayout(chip_row)

        row2 = QHBoxLayout()
        row2.setSpacing(8)
        self.photos_only_cb = TabletCheckBox("Только с фото")
        row2.addWidget(self.photos_only_cb)
        self.forward_only_cb = TabletCheckBox("Только пересланные")
        row2.addWidget(self.forward_only_cb)
        self.reply_only_cb = TabletCheckBox("Только ответы")
        row2.addWidget(self.reply_only_cb)
        row2.addStretch(1)
        self.count_label = label("")
        self.count_label.setStyleSheet(
            f"font-family: {theme.FONT_MONO}; font-size: 11px; color: {theme.TEXT_MUTED};"
        )
        row2.addWidget(self.count_label, alignment=Qt.AlignVCenter)
        clear_btn = button("Сбросить", "secondary")
        clear_btn.clicked.connect(self._clear_filters)
        row2.addWidget(clear_btn)
        filters_lay.addLayout(row2)
        top_lay.addWidget(filters_card)

        # Saved searches: the export screen has had presets from the start,
        # while the filter set that actually finds things had to be rebuilt
        # by hand every time.
        preset_row = QHBoxLayout()
        preset_row.setSpacing(6)
        preset_row.addWidget(muted("Сохранённые поиски"))
        self.preset_combo = QComboBox()
        self.preset_combo.setMinimumWidth(180)
        self.preset_combo.currentIndexChanged.connect(self._on_preset_picked)
        preset_row.addWidget(self.preset_combo)
        save_search_btn = button("Сохранить как…", "secondary")
        save_search_btn.clicked.connect(self._on_save_preset)
        preset_row.addWidget(save_search_btn)
        self.delete_search_btn = button("Удалить", "ghost")
        self.delete_search_btn.clicked.connect(self._on_delete_preset)
        preset_row.addWidget(self.delete_search_btn)
        preset_row.addStretch(1)
        top_lay.addLayout(preset_row)
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
        self.sort_combo.currentIndexChanged.connect(self._on_sort_change)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.timeout.connect(self._run_search)

        self._last_rows = []

    # ---- saved searches ------------------------------------------------
    def _populate_presets(self) -> None:
        current = self.preset_combo.currentData()
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItem("— не выбран —", None)
        for preset in self.ctx.db.list_search_presets():
            self.preset_combo.addItem(preset["name"], preset["name"])
        idx = self.preset_combo.findData(current)
        self.preset_combo.setCurrentIndex(max(0, idx))
        self.preset_combo.blockSignals(False)
        self.delete_search_btn.setEnabled(self.preset_combo.currentData() is not None)

    def _on_save_preset(self) -> None:
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "Сохранить поиск", "Название:")
        if not ok or not name.strip():
            return
        params = self.current_filters()
        params["sort_desc"] = self.sort_desc
        self.ctx.db.save_search_preset(name.strip(), params)
        self._populate_presets()
        idx = self.preset_combo.findData(name.strip())
        if idx >= 0:
            self.preset_combo.blockSignals(True)
            self.preset_combo.setCurrentIndex(idx)
            self.preset_combo.blockSignals(False)
            self.delete_search_btn.setEnabled(True)

    def _on_preset_picked(self, _index: int) -> None:
        name = self.preset_combo.currentData()
        self.delete_search_btn.setEnabled(name is not None)
        if name is None:
            return
        import json
        preset = next((p for p in self.ctx.db.list_search_presets() if p["name"] == name), None)
        if preset is None:
            return
        params = json.loads(preset["params"])
        for widget in (self.query_input, self.author_input):
            widget.blockSignals(True)
        self.query_input.setText(params.get("query", ""))
        self.author_input.setText(params.get("author", ""))
        for widget in (self.query_input, self.author_input):
            widget.blockSignals(False)
        for cb, key in ((self.photos_only_cb, "photos_only"),
                        (self.forward_only_cb, "forwards_only"),
                        (self.reply_only_cb, "replies_only")):
            cb.blockSignals(True)
            cb.setChecked(bool(params.get(key)))
            cb.blockSignals(False)
        self.sort_desc = bool(params.get("sort_desc", True))
        self.sort_combo.blockSignals(True)
        self.sort_combo.setCurrentIndex(0 if self.sort_desc else 1)
        self.sort_combo.blockSignals(False)
        chat_id = params.get("chat_id") or 0
        self._select_chat_chip(chat_id) if chat_id else self._select_chat_chip(0)
        self.page = 0
        self._run_search()

    def _on_delete_preset(self) -> None:
        name = self.preset_combo.currentData()
        if name is None:
            return
        self.ctx.db.delete_search_preset(name)
        self._populate_presets()

    def on_show(self, chat_id: int | None = None, **kwargs) -> None:
        self._populate_presets()
        self._populate_chat_picker()
        if chat_id is not None:
            self._select_chat_chip(chat_id)
        self._run_search()

    def _select_chat_chip(self, chat_id: int) -> None:
        chip_btn = self.chat_chips.get(chat_id)
        if chip_btn is not None:
            chip_btn.setChecked(True)
        self.selected_chat_id = chat_id if chip_btn is not None else 0

    def _populate_chat_picker(self) -> None:
        current = self.selected_chat_id
        for btn in self.chat_chips.values():
            self.chat_chip_group.removeButton(btn)
            btn.setParent(None)
            btn.deleteLater()
        self.chat_chips.clear()

        all_chip = chip("Все чаты")
        all_chip.setChecked(current == 0)
        all_chip.clicked.connect(lambda: self._pick_chat_chip(0))
        self.chat_chip_group.addButton(all_chip)
        self.chip_lay.insertWidget(0, all_chip)
        self.chat_chips[0] = all_chip

        for i, chat in enumerate(self.ctx.db.list_chats(), start=1):
            btn = chip(chat["title"])
            btn.setChecked(current == chat["chat_id"])
            btn.clicked.connect(lambda _c, cid=chat["chat_id"]: self._pick_chat_chip(cid))
            self.chat_chip_group.addButton(btn)
            self.chip_lay.insertWidget(i, btn)
            self.chat_chips[chat["chat_id"]] = btn

        if current not in self.chat_chips:
            self.selected_chat_id = 0
            self.chat_chips[0].setChecked(True)

    def _pick_chat_chip(self, chat_id: int) -> None:
        self.selected_chat_id = chat_id
        chip_btn = self.chat_chips.get(chat_id)
        if chip_btn is not None:
            chip_btn.setChecked(True)
        self._on_filter_changed()

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
        self._pick_chat_chip(0)

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
            "chat_id": self.selected_chat_id or None,
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
        for index, row in enumerate(rows):
            card_widget = MessageCard(self.ctx, row)
            self.results_lay.insertWidget(self.results_lay.count() - 1, card_widget)
            self._animate_card_in(card_widget, index)

    @staticmethod
    def _animate_card_in(card_widget: MessageCard, index: int) -> None:
        """design-brief.md §4.5: fade + 6px slide up, 350мс, delay
        `index×45мс` for only the first ~12 cards — beyond that, no delay,
        so a 100-row page doesn't visibly "crawl" in."""
        effect = QGraphicsOpacityEffect(card_widget)
        card_widget.setGraphicsEffect(effect)
        effect.setOpacity(0.0)
        fade = QPropertyAnimation(effect, b"opacity", card_widget)
        fade.setDuration(350)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        fade.setEasingCurve(QEasingCurve.OutCubic)
        slide = QPropertyAnimation(card_widget, b"content_offset", card_widget)
        slide.setDuration(350)
        slide.setStartValue(6)
        slide.setEndValue(0)
        slide.setEasingCurve(QEasingCurve.OutCubic)
        card_widget._chatgrab_anims = (fade, slide)
        delay_ms = index * 45 if index < 12 else 0
        QTimer.singleShot(delay_ms, fade.start)
        QTimer.singleShot(delay_ms, slide.start)

    def _export_found(self) -> None:
        filters = self.current_filters()
        self.navigate("export", search_filters=filters)
