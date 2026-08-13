from __future__ import annotations

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QProgressBar,
    QPushButton, QVBoxLayout, QWidget,
)

from ..context import AppContext
from ..util import fire
from ..widgets import KeyValue, StatusPill, button, h1, muted


class CollectScreen(QWidget):
    def __init__(self, ctx: AppContext, navigate):
        super().__init__()
        self.ctx = ctx
        self.navigate = navigate
        self.selected_chat_id: int | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 28, 40, 18)
        outer.setSpacing(0)

        top = QHBoxLayout()
        kicker = muted("ВЫБРАННЫЙ ЧАТ")
        left_col = QVBoxLayout()
        left_col.addWidget(kicker)
        row = QHBoxLayout()
        self.title_label = h1("—")
        row.addWidget(self.title_label)
        self.status_pill = StatusPill("idle")
        row.addWidget(self.status_pill)
        row.addStretch(1)
        self.load_btn = button("Загрузить историю", "primary")
        self.load_btn.clicked.connect(self._on_toggle_load)
        row.addWidget(self.load_btn)
        self.listen_btn = button("Слушать новые сообщения", "secondary")
        self.listen_btn.clicked.connect(self._on_toggle_listen)
        row.addWidget(self.listen_btn)
        left_col.addLayout(row)
        top.addLayout(left_col)
        outer.addLayout(top)
        outer.addSpacing(16)

        self.chat_picker = QComboBox()
        self.chat_picker.currentIndexChanged.connect(self._on_pick_chat)
        outer.addWidget(self.chat_picker)
        outer.addSpacing(14)

        stats_row = QHBoxLayout()
        self.kv_count = KeyValue("Собрано")
        self.kv_photos = KeyValue("Медиафайлов на диске")
        self.kv_last = KeyValue("Последнее сообщение")
        self.kv_depth = KeyValue("Глубина истории")
        for kv in (self.kv_count, self.kv_photos, self.kv_last, self.kv_depth):
            stats_row.addWidget(kv, 1)
        outer.addLayout(stats_row)
        outer.addSpacing(16)

        prog_row = QHBoxLayout()
        self.prog_label = muted("Загрузка не запущена")
        prog_row.addWidget(self.prog_label)
        prog_row.addStretch(1)
        self.prog_pct = muted("0%")
        prog_row.addWidget(self.prog_pct)
        outer.addLayout(prog_row)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 100)
        outer.addWidget(self.progress)
        outer.addSpacing(10)

        health_row = QHBoxLayout()
        self.health_label = muted("")
        health_row.addWidget(self.health_label)
        health_row.addStretch(1)
        self.delay_label = muted("")
        health_row.addWidget(self.delay_label)
        outer.addLayout(health_row)
        outer.addSpacing(14)

        log_panel = QWidget()
        log_panel.setProperty("class", "logpanel")
        log_lay = QVBoxLayout(log_panel)
        log_lay.setContentsMargins(0, 0, 0, 0)
        log_lay.setSpacing(0)
        log_header = QHBoxLayout()
        log_header.setContentsMargins(16, 11, 16, 11)
        log_header.addWidget(muted("ОБЩИЙ ЖУРНАЛ СОБЫТИЙ"))
        log_header.addStretch(1)
        self.log_count_label = muted("")
        log_header.addWidget(self.log_count_label)
        log_header_widget = QWidget()
        log_header_widget.setLayout(log_header)
        log_lay.addWidget(log_header_widget)
        self.log_list = QListWidget()
        self.log_list.setStyleSheet(
            "QListWidget { border: none; background: transparent; font-family: Consolas, monospace; font-size: 12px; }"
            "QListWidget::item { padding: 4px 12px; }"
        )
        log_lay.addWidget(self.log_list, 1)
        outer.addWidget(log_panel, 1)

        ctx.collector.chats_changed.connect(self.refresh)
        ctx.collector.log_event.connect(self._on_log_event)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_health)
        self._timer.start(2000)

        self._reload_log()
        self._populate_picker()

    def on_show(self, chat_id: int | None = None, **kwargs) -> None:
        self._populate_picker()
        if chat_id is not None:
            self.selected_chat_id = chat_id
        elif self.selected_chat_id is None:
            chats = self.ctx.db.list_chats()
            if chats:
                self.selected_chat_id = chats[0]["chat_id"]
        self._sync_picker_selection()
        self.refresh()

    def _populate_picker(self) -> None:
        self.chat_picker.blockSignals(True)
        self.chat_picker.clear()
        for chat in self.ctx.db.list_chats():
            self.chat_picker.addItem(chat["title"], chat["chat_id"])
        self.chat_picker.blockSignals(False)
        self._sync_picker_selection()

    def _sync_picker_selection(self) -> None:
        if self.selected_chat_id is None:
            return
        idx = self.chat_picker.findData(self.selected_chat_id)
        if idx >= 0:
            self.chat_picker.blockSignals(True)
            self.chat_picker.setCurrentIndex(idx)
            self.chat_picker.blockSignals(False)

    def _on_pick_chat(self, index: int) -> None:
        chat_id = self.chat_picker.itemData(index)
        if chat_id is not None:
            self.selected_chat_id = chat_id
            self.refresh()

    def refresh(self) -> None:
        if self.selected_chat_id is None:
            return
        chat = self.ctx.db.get_chat(self.selected_chat_id)
        if not chat:
            return
        db = self.ctx.db
        self.title_label.setText(chat["title"])
        self.status_pill.set_status(chat["status"])
        count = db.message_count(chat["chat_id"])
        self.kv_count.set_value(f"{count:,}".replace(",", " "))
        cfg = self.ctx.config
        media_enabled = cfg.photos_enabled or cfg.videos_enabled or cfg.voice_enabled or cfg.documents_enabled
        media = db.media_count(chat["chat_id"]) if media_enabled else 0
        self.kv_photos.set_value(f"{media:,}".replace(",", " ") if media_enabled else "выключено")
        last = db.last_message_date(chat["chat_id"])
        self.kv_last.set_value(str(last)[:19].replace("T", " ") if last else "—")
        depth = "вся история" if chat["depth_mode"] == "all" else f"с {chat['depth_from_date']}"
        if chat["history_done"]:
            depth += " · собрана"
        self.kv_depth.set_value(depth)

        loading = chat["status"] == "loading"
        self.load_btn.setText("Приостановить загрузку" if loading else
                               ("Догрузить историю" if chat["history_done"] else "Загрузить историю"))
        self.listen_btn.setText("Не слушать новые" if chat["enabled"] else "Слушать новые сообщения")

        if loading:
            approx = chat["approx_total"]
            if approx:
                pct = min(100, round(100 * count / max(approx, 1)))
            else:
                pct = 0
            self.progress.setValue(pct)
            self.prog_pct.setText(f"{pct}%")
            self.prog_label.setText(f"Загрузка истории · пауза между запросами {self.ctx.collector.delay.current:.1f} с")
        elif chat["history_done"]:
            self.progress.setValue(100)
            self.prog_pct.setText("100%")
            self.prog_label.setText("История собрана полностью")
        else:
            self.progress.setValue(0)
            self.prog_pct.setText("0%")
            self.prog_label.setText("Загрузка не запущена")

        self._refresh_health()

    def _refresh_health(self) -> None:
        h = self.ctx.collector.health
        self.health_label.setText(
            f"Запросов за час: {h.requests_last_hour()} · пауз сегодня: {h.floodwaits_today} "
            f"· время в паузах: {h.pause_seconds_today} с"
        )
        self.delay_label.setText(f"Текущая пауза: {self.ctx.collector.delay.current:.1f} с")

    def _on_toggle_load(self) -> None:
        if self.selected_chat_id is not None:
            self.ctx.collector.toggle_history_loading(self.selected_chat_id)
            self.refresh()

    def _on_toggle_listen(self) -> None:
        if self.selected_chat_id is not None:
            self.ctx.collector.toggle_listen(self.selected_chat_id)
            self.refresh()

    def _reload_log(self) -> None:
        self.log_list.clear()
        for entry in self.ctx.collector.log_entries[:200]:
            self._add_log_item(entry)
        self.log_count_label.setText(f"по всем чатам · {len(self.ctx.collector.log_entries)} записей")

    def _on_log_event(self, entry: dict) -> None:
        self._add_log_item(entry, prepend=True)
        if self.log_list.count() > 300:
            self.log_list.takeItem(self.log_list.count() - 1)
        self.log_count_label.setText(f"по всем чатам · {len(self.ctx.collector.log_entries)} записей")

    def _add_log_item(self, entry: dict, prepend: bool = False) -> None:
        color = {"warn": "#f0c6a0", "ok": "#bfe5cd"}.get(entry.get("tone"), "#d6d6db")
        text = f"{entry['time']}   {entry['chat']}   —   {entry['text']}"
        item = QListWidgetItem(text)
        item.setForeground(QColor(color))
        if prepend:
            self.log_list.insertItem(0, item)
        else:
            self.log_list.addItem(item)
