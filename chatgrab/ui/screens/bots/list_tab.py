from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QMessageBox, QVBoxLayout, QWidget,
)

from ...context import AppContext
from ...util import fire
from ...widgets import StatusPill, button, card, h1, label, muted, plural as _plural
from .wizard import BotWizardDialog

_TYPE_LABELS = {"bot_api": "отдельный бот-аккаунт", "userbot": "от вашего аккаунта"}
_PRESET_LABELS = {"b2b": "B2B", "b2c": "B2C", "custom": "CUSTOM"}


class StatCell(QWidget):
    def __init__(self, key: str, value: str = ""):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addWidget(label(key, "kicker"))
        self.value_label = label(value)
        self.value_label.setStyleSheet("font-size: 15px;")
        lay.addWidget(self.value_label)

    def set_value(self, value: str) -> None:
        self.value_label.setText(value)


class BotRow(QWidget):
    """One bot as a full-width row: identity and controls on the top line,
    its numbers underneath, and the one-line explanation of what it does
    (or why it stopped) at the bottom."""

    def __init__(self, ctx: AppContext, bot_id: int, on_changed, navigate):
        super().__init__()
        self.ctx = ctx
        self.bot_id = bot_id
        self.on_changed = on_changed
        self.navigate = navigate

        frame = card()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(frame)

        lay = QVBoxLayout(frame)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(0)

        top = QHBoxLayout()
        top.setSpacing(10)
        self.title_label = QLabel("")
        self.title_label.setTextFormat(Qt.PlainText)
        self.title_label.setStyleSheet("font-size: 15px; font-weight: 600;")
        top.addWidget(self.title_label)
        self.type_label = label("")
        self.type_label.setStyleSheet(
            "background: rgba(233,233,237,16); color: #9a9aa3; border-radius: 6px; "
            "padding: 2px 9px; font-size: 11.5px;"
        )
        top.addWidget(self.type_label)
        self.status_pill = StatusPill("stopped")
        top.addWidget(self.status_pill)
        top.addStretch(1)
        self.rules_btn = button("Правила и сценарий", "secondary")
        self.rules_btn.clicked.connect(lambda: self.navigate("rules"))
        top.addWidget(self.rules_btn)
        self.toggle_btn = button("Запустить", "primary")
        self.toggle_btn.clicked.connect(self._on_toggle)
        top.addWidget(self.toggle_btn)
        self.delete_btn = button("Удалить", "ghost")
        self.delete_btn.clicked.connect(self._on_delete)
        top.addWidget(self.delete_btn)
        lay.addLayout(top)
        lay.addSpacing(11)

        stats = QHBoxLayout()
        stats.setSpacing(26)
        self.st_leads = StatCell("Заявок")
        self.st_contacts = StatCell("Контактов")
        self.st_manager = StatCell("Менеджер")
        self.st_preset = StatCell("Сценарий")
        for cell in (self.st_leads, self.st_contacts, self.st_manager, self.st_preset):
            stats.addWidget(cell)
        stats.addStretch(1)
        lay.addLayout(stats)
        lay.addSpacing(9)

        self.note_label = QLabel("")
        # last_error can echo text from an exception raised by a 3rd-party
        # library (aiogram/Telethon) — plain text only, same reasoning as
        # every other label showing content this app doesn't fully control.
        self.note_label.setTextFormat(Qt.PlainText)
        self.note_label.setWordWrap(True)
        lay.addWidget(self.note_label)

    def refresh(self) -> None:
        db = self.ctx.db
        bot = db.get_bot(self.bot_id)
        if not bot:
            return
        self.title_label.setText(bot["name"])
        self.status_pill.set_status(bot["status"])
        self.type_label.setText(_TYPE_LABELS.get(bot["type"], bot["type"]))

        leads = db.list_leads(bot_id=self.bot_id)
        contacts = {l["contact_id"] for l in leads}
        self.st_leads.set_value(str(len(leads)))
        self.st_contacts.set_value(str(len(contacts)))
        self.st_manager.set_value(bot["manager_chat_id"] or "не задан")
        self.st_preset.set_value(_PRESET_LABELS.get(bot["preset"], bot["preset"]))

        if bot["last_error"]:
            self.note_label.setText(bot["last_error"])
            self.note_label.setStyleSheet("color: #f0c6a0; font-size: 12.5px;")
        else:
            n_t = len(db.list_triggers(self.bot_id))
            n_s = len(db.list_scenarios(self.bot_id))
            self.note_label.setText(
                f"{n_t} {_plural(n_t, 'правило', 'правила', 'правил')} · "
                f"{n_s} {_plural(n_s, 'сценарий', 'сценария', 'сценариев')}. "
                + ("Отвечает на личные сообщения по сценарию."
                   if bot["type"] == "bot_api"
                   else "Реагирует на сообщения в чатах вашего аккаунта.")
            )
            self.note_label.setStyleSheet("color: #9a9aa3; font-size: 12.5px;")

        running = bot["status"] == "running"
        self.toggle_btn.setText("Остановить" if running else "Запустить")
        self.toggle_btn.setProperty("class", "secondary" if running else "primary")
        self.toggle_btn.style().unpolish(self.toggle_btn)
        self.toggle_btn.style().polish(self.toggle_btn)

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
        leads = self.ctx.db.list_leads(bot_id=self.bot_id)
        if QMessageBox.question(
            self, "Удалить бота",
            f"Удалить «{bot['name']}»? Правила, сценарии и шаблоны бота будут удалены. "
            f"{len(leads)} уже сохранённых заявок и контакты останутся в базе."
        ) != QMessageBox.Yes:
            return
        fire(self.ctx.bot_manager.delete_bot(self.bot_id), parent=self, on_done=self.on_changed)


class BotsListTab(QWidget):
    def __init__(self, ctx: AppContext, navigate=None):
        super().__init__()
        self.ctx = ctx
        self.navigate = navigate or (lambda *_a, **_k: None)
        self.rows: dict[int, BotRow] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.setSpacing(3)
        title_col.addWidget(h1("Боты"))
        self.summary_label = muted("")
        title_col.addWidget(self.summary_label)
        header.addLayout(title_col)
        header.addStretch(1)
        self.add_btn = button("＋ Новый бот", "primary")
        self.add_btn.clicked.connect(self._on_add)
        header.addWidget(self.add_btn, alignment=Qt.AlignBottom)
        outer.addLayout(header)
        outer.addSpacing(16)

        self.empty_label = muted("Ботов пока нет — нажмите «Новый бот», чтобы создать первого.")
        outer.addWidget(self.empty_label)

        self.rows_lay = QVBoxLayout()
        self.rows_lay.setSpacing(9)
        outer.addLayout(self.rows_lay)

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
        db = self.ctx.db
        bots = db.list_bots()
        self.empty_label.setVisible(not bots)

        running = len([b for b in bots if b["status"] == "running"])
        total_leads = len(db.list_leads())
        self.summary_label.setText(
            f"{running} из {len(bots)} работает · {total_leads} заявок всего" if bots else
            "ни одного бота ещё не создано"
        )

        current_ids = {b["id"] for b in bots}
        for bid in list(self.rows):
            if bid not in current_ids:
                row = self.rows.pop(bid)
                row.setParent(None)
                row.deleteLater()

        for i, bot in enumerate(bots):
            row = self.rows.get(bot["id"])
            if row is None:
                row = BotRow(self.ctx, bot["id"], self.refresh, self.navigate)
                self.rows[bot["id"]] = row
            self.rows_lay.insertWidget(i, row)
            row.refresh()
