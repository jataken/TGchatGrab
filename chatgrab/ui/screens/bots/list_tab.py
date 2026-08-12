from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog, QGridLayout, QHBoxLayout, QLabel, QMessageBox, QVBoxLayout, QWidget,
)

from ...context import AppContext
from ...util import fire
from ...widgets import StatusPill, button, card, h1, muted
from .wizard import BotWizardDialog

_TYPE_LABELS = {"bot_api": "Бот-ассистент (Bot API)", "userbot": "Юзербот-триггеры"}
_PRESET_LABELS = {"b2b": "B2B", "b2c": "B2C", "custom": "Кастом"}


class BotCard(QWidget):
    def __init__(self, ctx: AppContext, bot_id: int, on_changed):
        super().__init__()
        self.ctx = ctx
        self.bot_id = bot_id
        self.on_changed = on_changed

        self.frame = card()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.frame)

        lay = QVBoxLayout(self.frame)
        lay.setContentsMargins(15, 14, 15, 14)
        lay.setSpacing(8)

        top = QHBoxLayout()
        self.title_label = QLabel("")
        self.title_label.setTextFormat(Qt.PlainText)
        self.title_label.setStyleSheet("font-size: 14.5px; font-weight: 600;")
        top.addWidget(self.title_label, 1)
        self.status_pill = StatusPill("stopped")
        top.addWidget(self.status_pill, alignment=Qt.AlignTop)
        lay.addLayout(top)

        self.meta_label = muted("")
        lay.addWidget(self.meta_label)

        self.error_label = QLabel("")
        # last_error can echo text from an exception raised by a 3rd-party
        # library (aiogram/Telethon) — plain text only, same reasoning as
        # every other label showing content this app doesn't fully control.
        self.error_label.setTextFormat(Qt.PlainText)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #e0a8b0; font-size: 11.5px;")
        lay.addWidget(self.error_label)

        btn_row = QHBoxLayout()
        self.toggle_btn = button("Запустить", "primary")
        self.toggle_btn.clicked.connect(self._on_toggle)
        btn_row.addWidget(self.toggle_btn)
        self.delete_btn = button("Удалить", "ghost")
        self.delete_btn.clicked.connect(self._on_delete)
        btn_row.addWidget(self.delete_btn)
        btn_row.addStretch(1)
        lay.addLayout(btn_row)

    def refresh(self) -> None:
        bot = self.ctx.db.get_bot(self.bot_id)
        if not bot:
            return
        self.title_label.setText(bot["name"])
        self.status_pill.set_status(bot["status"])
        type_label = _TYPE_LABELS.get(bot["type"], bot["type"])
        preset_label = _PRESET_LABELS.get(bot["preset"], bot["preset"])
        manager = bot["manager_chat_id"] or "не задан"
        self.meta_label.setText(f"{type_label} · пресет {preset_label} · менеджер: {manager}")
        self.error_label.setText(bot["last_error"] or "")
        self.error_label.setVisible(bool(bot["last_error"]))
        self.toggle_btn.setText("Остановить" if bot["status"] == "running" else "Запустить")
        self.toggle_btn.setProperty("class", "secondary" if bot["status"] == "running" else "primary")
        self.toggle_btn.setStyle(self.toggle_btn.style())

    def _on_toggle(self) -> None:
        bot = self.ctx.db.get_bot(self.bot_id)
        if not bot:
            return
        self.toggle_btn.setEnabled(False)
        coro = self.ctx.bot_manager.stop_bot(self.bot_id) if bot["status"] == "running" \
            else self.ctx.bot_manager.start_bot(self.bot_id)

        def on_done():
            self.toggle_btn.setEnabled(True)
            self.on_changed()

        def on_error(e):
            self.toggle_btn.setEnabled(True)
            QMessageBox.warning(self, "Не получилось", str(e))
            self.on_changed()

        fire(coro, parent=self, on_error=on_error, on_done=on_done)

    def _on_delete(self) -> None:
        bot = self.ctx.db.get_bot(self.bot_id)
        if not bot:
            return
        if QMessageBox.question(
            self, "Удалить бота",
            f"Удалить «{bot['name']}»? Правила, сценарии и шаблоны бота будут удалены. "
            "Уже сохранённые заявки и контакты останутся в базе."
        ) != QMessageBox.Yes:
            return
        fire(self.ctx.bot_manager.delete_bot(self.bot_id), parent=self, on_done=self.on_changed)


class BotsListTab(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.cards: dict[int, BotCard] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 16, 0, 16)

        header = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.addWidget(muted("Все боты этого инстанса — каждый со своим типом, правилами и данными."))
        header.addLayout(title_col)
        header.addStretch(1)
        self.add_btn = button("＋ Новый бот", "primary")
        self.add_btn.clicked.connect(self._on_add)
        header.addWidget(self.add_btn)
        outer.addLayout(header)
        outer.addSpacing(14)

        self.empty_label = muted("Ботов пока нет — нажмите «Новый бот», чтобы создать первого.")
        outer.addWidget(self.empty_label)

        self.grid_widget = QWidget()
        self.grid = QGridLayout(self.grid_widget)
        self.grid.setSpacing(14)
        outer.addWidget(self.grid_widget)
        outer.addStretch(1)

        ctx.bot_manager.bots_changed.connect(self.refresh)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(3000)

    def on_show(self) -> None:
        self.refresh()

    def _on_add(self) -> None:
        dlg = BotWizardDialog(self.ctx, parent=self)
        if dlg.exec() == QDialog.Accepted:
            self.refresh()

    def refresh(self) -> None:
        bots = self.ctx.db.list_bots()
        self.empty_label.setVisible(not bots)
        self.grid_widget.setVisible(bool(bots))

        current_ids = {b["id"] for b in bots}
        for bid in list(self.cards):
            if bid not in current_ids:
                c = self.cards.pop(bid)
                c.setParent(None)
                c.deleteLater()

        cols = max(1, self.grid_widget.width() // 320) or 1
        for i, bot in enumerate(bots):
            c = self.cards.get(bot["id"])
            if c is None:
                c = BotCard(self.ctx, bot["id"], self.refresh)
                self.cards[bot["id"]] = c
            self.grid.addWidget(c, i // cols, i % cols)
            c.refresh()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.refresh()
