from __future__ import annotations

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QGridLayout, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget,
)

from ..context import AppContext
from ..widgets import ActivityBars, KeyValue, StatusPill, card, h1, muted


class ChatTile(QWidget):
    def __init__(self, ctx: AppContext, chat_id: int, on_click):
        super().__init__()
        self.ctx = ctx
        self.chat_id = chat_id
        self.on_click = on_click
        self.setCursor(Qt.PointingHandCursor)

        self.frame = card()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.frame)

        lay = QVBoxLayout(self.frame)
        lay.setContentsMargins(15, 14, 15, 14)
        lay.setSpacing(10)

        top = QHBoxLayout()
        self.title_label = QLabel("")
        # Chat titles come from Telegram (any group admin/channel owner
        # controls them) — plain text only, so one that looks like markup
        # can't get rendered as real HTML by QLabel's rich-text autodetect.
        self.title_label.setTextFormat(Qt.PlainText)
        self.title_label.setWordWrap(True)
        self.title_label.setStyleSheet("font-size: 14.5px;")
        top.addWidget(self.title_label, 1)
        self.status_pill = StatusPill("idle")
        top.addWidget(self.status_pill, alignment=Qt.AlignTop)
        lay.addLayout(top)

        self.bars = ActivityBars()
        lay.addWidget(self.bars)

        bottom = QHBoxLayout()
        self.count_label = QLabel("0")
        self.count_label.setStyleSheet("font-size: 16px;")
        bottom.addWidget(self.count_label)
        self.per_day_label = muted("")
        bottom.addWidget(self.per_day_label)
        bottom.addStretch(1)
        self.last_label = muted("")
        bottom.addWidget(self.last_label)
        lay.addLayout(bottom)

        self.warn_label = QLabel("")
        self.warn_label.setTextFormat(Qt.PlainText)
        self.warn_label.setStyleSheet("color: #f0c6a0; font-size: 11.5px;")
        self.warn_label.setWordWrap(True)
        lay.addWidget(self.warn_label)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.on_click(self.chat_id)

    def refresh(self, gap_notify_days: int) -> None:
        db = self.ctx.db
        chat = db.get_chat(self.chat_id)
        if not chat:
            return
        self.title_label.setText(chat["title"])
        self.status_pill.set_status(chat["status"])
        count = db.message_count(self.chat_id)
        self.count_label.setText(f"{count:,}".replace(",", " "))
        bars = db.activity_bars(self.chat_id)
        self.bars.set_values(bars)
        per_day = round(sum(bars) / max(1, len(bars)))
        self.per_day_label.setText(f"≈{per_day} сообщ./сутки")
        last = db.last_message_date(self.chat_id)
        self.last_label.setText(str(last)[:16].replace("T", " ") if last else "—")

        warn = ""
        if chat["status"] == "off":
            warn = "Сбор выключен — новые сообщения не пишутся"
        elif chat["status"] == "queued":
            warn = "Ждёт своей очереди на историю"
        elif chat["last_error"]:
            warn = f"Чат стал недоступен: {chat['last_error']}"
        elif last:
            import datetime as dt
            try:
                last_dt = dt.datetime.fromisoformat(last)
                days = (dt.datetime.now(last_dt.tzinfo) - last_dt).days
                if days > gap_notify_days:
                    warn = f"Нет новых сообщений уже {days} суток"
            except ValueError:
                pass
        self.warn_label.setText(warn)


class OverviewScreen(QWidget):
    def __init__(self, ctx: AppContext, navigate):
        super().__init__()
        self.ctx = ctx
        self.navigate = navigate
        self.tiles: dict[int, ChatTile] = {}

        outer_scroll = QScrollArea()
        outer_scroll.setWidgetResizable(True)
        container = QWidget()
        outer_scroll.setWidget(container)
        wrap = QVBoxLayout(self)
        wrap.setContentsMargins(0, 0, 0, 0)
        wrap.addWidget(outer_scroll)

        outer = QVBoxLayout(container)
        outer.setContentsMargins(40, 28, 40, 32)

        outer.addWidget(h1("Обзор сбора"))
        self.alert_label = muted("")
        outer.addWidget(self.alert_label)
        outer.addSpacing(14)

        totals_row = QHBoxLayout()
        self.kv_messages = KeyValue("Сообщений в базе")
        self.kv_photos = KeyValue("Медиафайлов на диске")
        self.kv_chats = KeyValue("Чатов в работе")
        self.kv_size = KeyValue("Размер базы")
        self.kv_export = KeyValue("Прошлая выгрузка")
        for kv in (self.kv_messages, self.kv_photos, self.kv_chats, self.kv_size, self.kv_export):
            totals_row.addWidget(kv)
        totals_row.addStretch(1)
        outer.addLayout(totals_row)
        outer.addSpacing(22)

        self.grid_widget = QWidget()
        self.grid = QGridLayout(self.grid_widget)
        self.grid.setSpacing(14)
        outer.addWidget(self.grid_widget)
        outer.addStretch(1)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(3000)
        ctx.collector.stats_changed.connect(self.refresh)
        ctx.collector.chats_changed.connect(self.refresh)

    def on_show(self, **kwargs) -> None:
        self.refresh()

    def refresh(self) -> None:
        db = self.ctx.db
        chats = db.list_chats()

        total_msgs = sum(db.message_count(c["chat_id"]) for c in chats)
        total_photos = sum(db.media_count(c["chat_id"]) for c in chats)
        enabled_n = len([c for c in chats if c["enabled"]])
        size_mb = db.file_size() / (1024 * 1024)
        logs = db.list_export_log(limit=1)
        last_export = logs[0]["created_at"][:16].replace("T", " ") if logs else "ещё не было"

        self.kv_messages.set_value(f"{total_msgs:,}".replace(",", " "))
        self.kv_photos.set_value(f"{total_photos:,}".replace(",", " "))
        self.kv_chats.set_value(f"{enabled_n} / {len(chats)}")
        self.kv_size.set_value(f"{size_mb:.1f} МБ")
        self.kv_export.set_value(last_export)

        loading = [c for c in chats if c["status"] == "loading"]
        if loading:
            self.alert_label.setText(
                f"Сейчас грузится история «{loading[0]['title']}», остальные чаты ждут в очереди — "
                "это нормально: лимит Telegram общий на аккаунт."
            )
        else:
            self.alert_label.setText("Вся история собрана, приложение только слушает новые сообщения.")

        gap_days = db.get_setting("gap_notify_days", 7)

        current_ids = {c["chat_id"] for c in chats}
        for cid in list(self.tiles):
            if cid not in current_ids:
                tile = self.tiles.pop(cid)
                tile.setParent(None)
                tile.deleteLater()

        cols = max(1, self.grid_widget.width() // 320)
        for i, chat in enumerate(chats):
            tile = self.tiles.get(chat["chat_id"])
            if tile is None:
                tile = ChatTile(self.ctx, chat["chat_id"], self._open_chat)
                self.tiles[chat["chat_id"]] = tile
            self.grid.addWidget(tile, i // cols, i % cols)
            tile.refresh(gap_days)

    def _open_chat(self, chat_id: int) -> None:
        self.navigate("collect", chat_id=chat_id)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.refresh()
