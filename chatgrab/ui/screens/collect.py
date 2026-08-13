from __future__ import annotations

from PySide6.QtCore import QDate, QTimer, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox, QDateEdit, QGridLayout, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QProgressBar, QRadioButton, QVBoxLayout, QWidget,
)

from ..context import AppContext
from ..widgets import KeyValue, LiveChart, StatusPill, button, card, h1, label, muted


class CollectScreen(QWidget):
    """Two columns: the selected chat's card + the queue of chats waiting
    their turn on the left, the shared event log on the right — chats
    load one at a time because Telegram's rate limit is per-account, not
    per-chat, so seeing the queue next to the active job matters."""

    SPEED_SAMPLE_MS = 2000

    def __init__(self, ctx: AppContext, navigate):
        super().__init__()
        self.ctx = ctx
        self.navigate = navigate
        self.selected_chat_id: int | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 26, 40, 24)
        outer.setSpacing(0)

        head = QHBoxLayout()
        head.setSpacing(14)
        head.addWidget(h1("Сбор"))
        self.headline_label = muted("")
        head.addWidget(self.headline_label)
        head.addStretch(1)
        self.chat_picker = QComboBox()
        self.chat_picker.setMinimumWidth(220)
        self.chat_picker.currentIndexChanged.connect(self._on_pick_chat)
        head.addWidget(self.chat_picker)
        outer.addLayout(head)
        outer.addSpacing(16)

        body = QHBoxLayout()
        body.setSpacing(18)
        outer.addLayout(body, 1)

        # ---- left column: current chat + queue ----
        left_col = QVBoxLayout()
        left_col.setSpacing(12)

        cur_frame = card()
        cur_lay = QVBoxLayout(cur_frame)
        cur_lay.setContentsMargins(18, 16, 18, 16)
        cur_lay.setSpacing(0)

        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        self.title_label = QLabel("—")
        self.title_label.setTextFormat(Qt.PlainText)
        self.title_label.setStyleSheet("font-size: 16px; font-weight: 600;")
        self.title_label.setWordWrap(True)
        title_row.addWidget(self.title_label, 1)
        self.status_pill = StatusPill("idle")
        title_row.addWidget(self.status_pill, alignment=Qt.AlignVCenter)
        cur_lay.addLayout(title_row)
        cur_lay.addSpacing(10)

        actions_row = QHBoxLayout()
        actions_row.setSpacing(8)
        self.load_btn = button("Загрузить историю", "secondary")
        self.load_btn.clicked.connect(self._on_toggle_load)
        actions_row.addWidget(self.load_btn)
        self.listen_btn = button("Слушать новые сообщения", "secondary")
        self.listen_btn.clicked.connect(self._on_toggle_listen)
        actions_row.addWidget(self.listen_btn)
        self.results_btn = button("Смотреть собранное", "ghost")
        self.results_btn.clicked.connect(self._on_open_results)
        actions_row.addWidget(self.results_btn)
        actions_row.addStretch(1)
        cur_lay.addLayout(actions_row)
        cur_lay.addSpacing(14)

        prog_row = QHBoxLayout()
        self.prog_label = muted("Загрузка не запущена")
        prog_row.addWidget(self.prog_label)
        prog_row.addStretch(1)
        self.prog_pct = muted("0%")
        prog_row.addWidget(self.prog_pct)
        cur_lay.addLayout(prog_row)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 100)
        cur_lay.addWidget(self.progress)
        cur_lay.addSpacing(14)

        stats_row = QGridLayout()
        stats_row.setHorizontalSpacing(18)
        stats_row.setVerticalSpacing(10)
        self.kv_count = KeyValue("Собрано")
        self.kv_photos = KeyValue("Медиафайлов на диске")
        self.kv_last = KeyValue("Последнее сообщение")
        self.kv_speed = KeyValue("Скорость")
        stats_row.addWidget(self.kv_count, 0, 0)
        stats_row.addWidget(self.kv_photos, 0, 1)
        stats_row.addWidget(self.kv_last, 1, 0)
        stats_row.addWidget(self.kv_speed, 1, 1)
        stats_row.setColumnStretch(0, 1)
        stats_row.setColumnStretch(1, 1)
        cur_lay.addLayout(stats_row)
        left_col.addWidget(cur_frame)

        # ---- live speed chart ----
        chart_frame = card()
        chart_lay = QVBoxLayout(chart_frame)
        chart_lay.setContentsMargins(16, 12, 16, 12)
        chart_lay.setSpacing(6)
        chart_head = QHBoxLayout()
        chart_head.addWidget(label("СКОРОСТЬ СБОРА", "kicker"))
        chart_head.addStretch(1)
        self.chart_now_label = muted("")
        chart_head.addWidget(self.chart_now_label)
        chart_lay.addLayout(chart_head)
        self.speed_chart = LiveChart()
        chart_lay.addWidget(self.speed_chart)
        self.chart_foot_label = muted("сообщений в секунду · последние 2 минуты, по всем чатам")
        chart_lay.addWidget(self.chart_foot_label)
        left_col.addWidget(chart_frame)

        # ---- history depth (date range) ----
        depth_frame = card()
        depth_lay = QVBoxLayout(depth_frame)
        depth_lay.setContentsMargins(16, 12, 16, 14)
        depth_lay.setSpacing(8)
        depth_lay.addWidget(label("ЗА КАКОЙ ПЕРИОД СОБИРАТЬ", "kicker"))
        depth_row = QHBoxLayout()
        depth_row.setSpacing(10)
        self.depth_all = QRadioButton("Всю историю с начала чата")
        self.depth_all.toggled.connect(self._on_depth_mode_changed)
        depth_row.addWidget(self.depth_all)
        self.depth_from = QRadioButton("Начиная с даты")
        depth_row.addWidget(self.depth_from)
        self.depth_date = QDateEdit()
        self.depth_date.setCalendarPopup(True)
        self.depth_date.setDisplayFormat("dd.MM.yyyy")
        self.depth_date.setDate(QDate.currentDate().addMonths(-3))
        depth_row.addWidget(self.depth_date)
        depth_row.addStretch(1)
        self.depth_save_btn = button("Применить", "primary")
        self.depth_save_btn.clicked.connect(self._on_save_depth)
        depth_row.addWidget(self.depth_save_btn)
        depth_lay.addLayout(depth_row)
        self.depth_hint = muted("")
        self.depth_hint.setWordWrap(True)
        depth_lay.addWidget(self.depth_hint)
        left_col.addWidget(depth_frame)

        # ---- queue panel ----
        queue_frame = card()
        queue_lay = QVBoxLayout(queue_frame)
        queue_lay.setContentsMargins(16, 12, 16, 14)
        queue_lay.setSpacing(8)
        queue_head = QHBoxLayout()
        queue_head.addWidget(label("ОЧЕРЕДЬ", "kicker"))
        self.queue_count_label = muted("")
        queue_head.addWidget(self.queue_count_label)
        queue_head.addStretch(1)
        queue_lay.addLayout(queue_head)
        self.queue_list = QListWidget()
        self.queue_list.setStyleSheet(
            "QListWidget { border: none; background: transparent; font-size: 13px; }"
            "QListWidget::item { padding: 5px 6px; border-radius: 6px; }"
        )
        self.queue_list.setMaximumHeight(170)
        queue_lay.addWidget(self.queue_list)
        left_col.addWidget(queue_frame)
        left_col.addStretch(1)

        health_row = QHBoxLayout()
        self.health_label = muted("")
        health_row.addWidget(self.health_label)
        health_row.addStretch(1)
        self.delay_label = muted("")
        health_row.addWidget(self.delay_label)
        left_col.addLayout(health_row)

        body.addLayout(left_col, 55)

        # ---- right column: log ----
        log_panel = QWidget()
        log_panel.setProperty("class", "logpanel")
        log_lay = QVBoxLayout(log_panel)
        log_lay.setContentsMargins(0, 0, 0, 0)
        log_lay.setSpacing(0)
        log_header = QHBoxLayout()
        log_header.setContentsMargins(16, 11, 16, 11)
        log_header.addWidget(label("ЖУРНАЛ", "kicker"))
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
        body.addWidget(log_panel, 45)

        ctx.collector.chats_changed.connect(self.refresh)
        ctx.collector.log_event.connect(self._on_log_event)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_health)
        self._timer.start(2000)

        # The speed chart samples the total message count on its own short
        # tick and plots the delta — one cheap COUNT(*) every two seconds
        # rather than any per-message signal wiring.
        self._last_total: int | None = None
        self._speed_timer = QTimer(self)
        self._speed_timer.timeout.connect(self._sample_speed)
        self._speed_timer.start(self.SPEED_SAMPLE_MS)

        self._reload_log()
        self._populate_picker()

    def _sample_speed(self) -> None:
        total = self.ctx.db.message_count()
        if self._last_total is None:
            self._last_total = total
            return
        delta = max(0, total - self._last_total)
        self._last_total = total
        per_second = delta / (self.SPEED_SAMPLE_MS / 1000)
        self.speed_chart.push(per_second)

        values = self.speed_chart.values()
        recent = values[-5:] if values else [0.0]
        avg = sum(recent) / len(recent)
        self.chart_now_label.setText(f"сейчас {per_second:.1f}/с · в среднем {avg:.1f}/с")
        self.kv_speed.set_value(f"{per_second:.1f} сообщ./с")

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
        db = self.ctx.db
        chats = db.list_chats()
        loading = next((c for c in chats if c["status"] == "loading"), None)
        queued = [c for c in chats if c["status"] == "queued"]
        self.headline_label.setText(
            "один чат за раз — лимит Telegram общий на аккаунт" if loading else
            "история собрана, идёт только приём новых сообщений"
        )

        self.queue_list.clear()
        rows = ([{"title": loading["title"], "note": "грузится"}] if loading else []) + \
               [{"title": c["title"], "note": "ждёт"} for c in queued]
        self.queue_count_label.setText(
            f"{len(queued)} в очереди" if queued else ("грузится один" if loading else "пусто")
        )
        for r in rows:
            item = QListWidgetItem(f"{r['title']}   —   {r['note']}")
            if r["note"] == "грузится":
                item.setForeground(QColor("#b5abfc"))
            self.queue_list.addItem(item)

        if self.selected_chat_id is None:
            return
        chat = db.get_chat(self.selected_chat_id)
        if not chat:
            return
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
        self._sync_depth_controls(chat)

        chat_loading = chat["status"] == "loading"
        self.load_btn.setText("Приостановить загрузку" if chat_loading else
                               ("Догрузить историю" if chat["history_done"] else "Загрузить историю"))
        self.listen_btn.setText("Не слушать новые" if chat["enabled"] else "Слушать новые сообщения")

        if chat_loading:
            approx = chat["approx_total"]
            pct = min(100, round(100 * count / max(approx, 1))) if approx else 0
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

    # ---- history depth ------------------------------------------------
    def _sync_depth_controls(self, chat) -> None:
        """Mirror the stored depth into the radio/date pair without
        echoing back a change signal (which would immediately re-save)."""
        from_date = chat["depth_from_date"]
        is_from = chat["depth_mode"] == "from_date" and bool(from_date)
        for w in (self.depth_all, self.depth_from, self.depth_date):
            w.blockSignals(True)
        self.depth_all.setChecked(not is_from)
        self.depth_from.setChecked(is_from)
        if is_from:
            parsed = QDate.fromString(str(from_date)[:10], "yyyy-MM-dd")
            if parsed.isValid():
                self.depth_date.setDate(parsed)
        for w in (self.depth_all, self.depth_from, self.depth_date):
            w.blockSignals(False)
        self.depth_date.setEnabled(is_from)

        if is_from:
            hint = f"Сейчас собирается всё, что новее {self.depth_date.date().toString('dd.MM.yyyy')}."
        else:
            hint = "Сейчас собирается вся доступная история чата с самого начала."
        if chat["history_done"]:
            hint += " История уже дочитана до этой границы — после смены периода нажмите «Догрузить историю»."
        self.depth_hint.setText(hint)

    def _on_depth_mode_changed(self, _checked: bool) -> None:
        self.depth_date.setEnabled(self.depth_from.isChecked())

    def _on_save_depth(self) -> None:
        if self.selected_chat_id is None:
            return
        db = self.ctx.db
        chat = db.get_chat(self.selected_chat_id)
        if not chat:
            return
        if self.depth_from.isChecked():
            mode, from_date = "from_date", self.depth_date.date().toString("yyyy-MM-dd")
        else:
            mode, from_date = "all", None

        # Widening the window (an earlier start, or "all") means there is
        # older history to fetch again, so history_done has to be cleared
        # or the backfill worker would consider the chat finished and skip
        # it. Narrowing needs no such reset — already-stored messages stay.
        old_mode = chat["depth_mode"]
        old_from = str(chat["depth_from_date"])[:10] if chat["depth_from_date"] else None
        if mode == "all":
            widened = old_mode != "all"
        elif old_mode == "all":
            widened = False  # adding a cutoff only narrows
        else:
            widened = bool(old_from) and from_date < old_from

        fields = {"depth_mode": mode, "depth_from_date": from_date}
        if widened:
            fields["history_done"] = 0
        db.set_chat_field(self.selected_chat_id, **fields)

        note = "Период обновлён."
        if widened:
            note += " Нужно догрузить более старые сообщения — нажмите «Догрузить историю»."
        else:
            note += " Уже собранные сообщения остаются в базе."
        QMessageBox.information(self, "Период сбора", note)
        self.refresh()

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

    def _on_open_results(self) -> None:
        self.navigate("browse", chat_id=self.selected_chat_id)

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
