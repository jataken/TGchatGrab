from __future__ import annotations

from PySide6.QtCore import QTime, Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QMessageBox, QScrollArea, QSpinBox, QTimeEdit, QVBoxLayout, QWidget,
)

from ... import theme
from ...context import AppContext
from ...icons import nav_icon
from ...util import fire
from ...widgets import Card, StatusPill, button, h1, hline, label, muted
from ....bots import settings as bot_settings
from .wizard import BotWizardDialog

_TYPE_LABELS = {"bot_api": "отдельный бот-аккаунт", "userbot": "от вашего аккаунта"}
_PRESET_LABELS = {"b2b": "B2B", "b2c": "B2C", "custom": "CUSTOM"}
_STATUS_STRIPE = {"running": theme.GOOD, "stopped": theme.TEXT_FAINT, "error": theme.BAD}


class SendLimitsDialog(QDialog):
    """How fast this bot is allowed to send, and the account-safety layer
    around that (С4: outbox). For a userbot these are the settings that
    keep the user's own phone number out of trouble, so they live one
    click from the run/stop button rather than in app settings."""

    def __init__(self, ctx: AppContext, bot_id: int, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.bot_id = bot_id
        bot = ctx.db.get_bot(bot_id)
        self.setWindowTitle("Ограничения отправки")
        self.setMinimumWidth(560)
        self.resize(560, 640)
        values = bot_settings.load(bot)

        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        lay = QVBoxLayout(inner)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        intro = muted(
            "Telegram ограничивает аккаунт за пачки исходящих сообщений. "
            "Эти паузы — то, что отличает бота от человека, разбирающего список."
            if bot and bot["type"] == "userbot" else
            "Ограничения на скорость отправки сообщений этим ботом."
        )
        intro.setWordWrap(True)
        lay.addWidget(intro)
        lay.addSpacing(8)

        counts = ctx.db.outbox_counts(bot_id)
        self.counters_label = muted(
            f"Отправлено за час: {counts['hour']} из {int(values['max_per_hour'])} · "
            f"за сутки: {counts['day']} из {int(values['max_per_day'])} · "
            f"первых сообщений сегодня: {counts['first_today']} из {int(values['max_first_messages_per_day'])}"
        )
        self.counters_label.setWordWrap(True)
        lay.addWidget(self.counters_label)
        lay.addSpacing(10)

        self.gap = QDoubleSpinBox()
        self.gap.setRange(*bot_settings.BOUNDS["send_gap_seconds"])
        self.gap.setDecimals(1)
        self.gap.setSingleStep(0.5)
        self.gap.setSuffix(" с")
        self.gap.setValue(float(values["send_gap_seconds"]))
        lay.addWidget(muted("Пауза между любыми двумя сообщениями этого бота"))
        lay.addWidget(self.gap)

        self.cooldown = QSpinBox()
        self.cooldown.setRange(*[int(b) for b in bot_settings.BOUNDS["dm_cooldown_seconds"]])
        self.cooldown.setSuffix(" с")
        self.cooldown.setValue(int(values["dm_cooldown_seconds"]))
        lay.addWidget(muted("Не писать одному и тому же человеку чаще, чем раз в"))
        lay.addWidget(self.cooldown)

        self.cap = QSpinBox()
        self.cap.setRange(*[int(b) for b in bot_settings.BOUNDS["max_reminders_per_tick"]])
        self.cap.setValue(int(values["max_reminders_per_tick"]))
        lay.addWidget(muted("Максимум напоминаний за один заход (остальные — в следующий)"))
        lay.addWidget(self.cap)

        self.estimate = muted("")
        self.estimate.setWordWrap(True)
        lay.addWidget(self.estimate)
        for w in (self.gap, self.cap):
            w.valueChanged.connect(self._update_estimate)
        self._update_estimate()

        # ---- account-wide limits (С4) --------------------------------
        lay.addSpacing(10)
        lay.addWidget(label("ЛИМИТЫ НА АККАУНТ", "kicker"))
        self.per_hour = QSpinBox()
        self.per_hour.setRange(*[int(b) for b in bot_settings.BOUNDS["max_per_hour"]])
        self.per_hour.setValue(int(values["max_per_hour"]))
        lay.addWidget(muted("Сообщений в час, не больше"))
        lay.addWidget(self.per_hour)
        self.per_day = QSpinBox()
        self.per_day.setRange(*[int(b) for b in bot_settings.BOUNDS["max_per_day"]])
        self.per_day.setValue(int(values["max_per_day"]))
        lay.addWidget(muted("Сообщений в сутки, не больше"))
        lay.addWidget(self.per_day)
        self.first_per_day = QSpinBox()
        self.first_per_day.setRange(*[int(b) for b in bot_settings.BOUNDS["max_first_messages_per_day"]])
        self.first_per_day.setValue(int(values["max_first_messages_per_day"]))
        lay.addWidget(muted("Из них — первых сообщений новым контактам в сутки, не больше"))
        lay.addWidget(self.first_per_day)
        self.contact_cooldown_days = QSpinBox()
        self.contact_cooldown_days.setRange(*[int(b) for b in bot_settings.BOUNDS["contact_cooldown_days"]])
        self.contact_cooldown_days.setSuffix(" сут.")
        self.contact_cooldown_days.setValue(int(values["contact_cooldown_days"]))
        lay.addWidget(muted("Не писать одному контакту повторно (сообщения по своей инициативе) чаще, чем раз в"))
        lay.addWidget(self.contact_cooldown_days)

        # ---- quiet hours -----------------------------------------------
        lay.addSpacing(10)
        lay.addWidget(label("ТИХИЕ ЧАСЫ", "kicker"))
        lay.addWidget(muted(
            "Действуют только на сообщения по инициативе бота (расписание, напоминания молчащим) — "
            "ответ на входящее уходит в любое время."))
        window_row = QHBoxLayout()
        self.quiet_start = QTimeEdit()
        self.quiet_start.setDisplayFormat("HH:mm")
        self.quiet_start.setTime(QTime.fromString(str(values["quiet_start"]), "HH:mm"))
        window_row.addWidget(muted("с"))
        window_row.addWidget(self.quiet_start)
        self.quiet_end = QTimeEdit()
        self.quiet_end.setDisplayFormat("HH:mm")
        self.quiet_end.setTime(QTime.fromString(str(values["quiet_end"]), "HH:mm"))
        window_row.addWidget(muted("до"))
        window_row.addWidget(self.quiet_end)
        window_row.addStretch(1)
        lay.addLayout(window_row)
        self.quiet_weekends = QCheckBox("Не писать по инициативе бота в выходные")
        self.quiet_weekends.setChecked(bool(values["quiet_weekends"]))
        lay.addWidget(self.quiet_weekends)

        # ---- safety --------------------------------------------------
        lay.addSpacing(10)
        lay.addWidget(label("БЕЗОПАСНОСТЬ", "kicker"))
        self.dry_run = QCheckBox("Пробный режим — ничего не отправлять, только записывать в журнал")
        self.dry_run.setChecked(bool(values["dry_run"]))
        lay.addWidget(self.dry_run)
        self.auto_send_cold = QCheckBox("Отправлять первое сообщение новому контакту сразу, без черновика")
        self.auto_send_cold.setChecked(bool(values["auto_send_cold"]))
        self._auto_send_cold_was = self.auto_send_cold.isChecked()
        lay.addWidget(self.auto_send_cold)
        warn = muted(
            "Выключено по умолчанию не просто так: холодная рассылка от обычного аккаунта — "
            "самый быстрый способ получить ограничение. Пока выключено, первое сообщение "
            "по инициативе бота ложится в черновики — их видно на экране «Боты»."
        )
        warn.setWordWrap(True)
        lay.addWidget(warn)

        # ---- per-bot blacklist -----------------------------------------
        lay.addSpacing(10)
        lay.addWidget(label("ЧЁРНЫЙ СПИСОК", "kicker"))
        lay.addWidget(muted("Этому боту, по инициативе или в ответ, не писать этим адресатам никогда"))
        self.blacklist_list = QListWidget()
        self.blacklist_list.setMaximumHeight(90)
        lay.addWidget(self.blacklist_list)
        bl_row = QHBoxLayout()
        self.blacklist_input = QLineEdit()
        self.blacklist_input.setPlaceholderText("telegram id или @username")
        bl_row.addWidget(self.blacklist_input, 1)
        add_bl_btn = button("Добавить", "secondary")
        add_bl_btn.clicked.connect(self._on_add_blacklist)
        bl_row.addWidget(add_bl_btn)
        remove_bl_btn = button("Убрать выбранного", "ghost")
        remove_bl_btn.clicked.connect(self._on_remove_blacklist)
        bl_row.addWidget(remove_bl_btn)
        lay.addLayout(bl_row)
        self._reload_blacklist()

        # Аккаунт, от имени которого пишет юзербот. Появляется только
        # когда аккаунтов больше одного и только для юзерботов: у бота
        # через Bot API свой собственный аккаунт по определению.
        self.account_combo = None
        accounts = ctx.db.list_accounts()
        if bot and bot["type"] == "userbot" and len(accounts) > 1:
            lay.addSpacing(10)
            lay.addWidget(muted(
                "Аккаунт, от имени которого бот пишет. Отдельный номер под рассылку "
                "означает, что ограничение за отправку не заденет сбор чатов."))
            self.account_combo = QComboBox()
            for acc in accounts:
                suffix = " · основной" if acc["is_default"] else ""
                self.account_combo.addItem(acc["name"] + suffix, acc["id"])
            current = bot["account_id"] if "account_id" in bot.keys() else None
            if current is None:
                default = ctx.db.default_account()
                current = default["id"] if default else None
            idx = self.account_combo.findData(current)
            self.account_combo.setCurrentIndex(max(0, idx))
            lay.addWidget(self.account_combo)

        row = QHBoxLayout()
        row.addStretch(1)
        cancel = button("Отмена", "secondary")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        save = button("Сохранить", "primary")
        save.clicked.connect(self._on_save)
        row.addWidget(save)
        outer.addLayout(row)

    def _update_estimate(self) -> None:
        gap = self.gap.value()
        cap = self.cap.value()
        seconds = gap * max(0, cap - 1)
        minutes = seconds / 60
        human = f"{seconds:.0f} с" if seconds < 90 else f"{minutes:.0f} мин"
        self.estimate.setText(
            f"При этих значениях полный заход напоминаний ({cap} шт.) растянется примерно на {human}."
        )

    def _reload_blacklist(self) -> None:
        self.blacklist_list.clear()
        for row in self.ctx.db.list_blacklist(self.bot_id):
            self.blacklist_list.addItem(row["target"])

    def _on_add_blacklist(self) -> None:
        target = self.blacklist_input.text().strip()
        if not target:
            return
        self.ctx.db.add_to_blacklist(self.bot_id, target)
        self.blacklist_input.clear()
        self._reload_blacklist()

    def _on_remove_blacklist(self) -> None:
        item = self.blacklist_list.currentItem()
        if item is None:
            return
        self.ctx.db.remove_from_blacklist(self.bot_id, item.text())
        self._reload_blacklist()

    def _on_save(self) -> None:
        # Turning this on is the one setting invariant 6 explicitly asks
        # to be guarded — a plain checkbox is too easy to tick without
        # reading the warning label above it.
        if self.auto_send_cold.isChecked() and not self._auto_send_cold_was:
            if QMessageBox.question(
                self, "Включить рассылку первых сообщений",
                "Первое сообщение новым контактам будет уходить сразу, без черновика и без клика. "
                "Это главный способ получить ограничение на обычный аккаунт Telegram. Включить?"
            ) != QMessageBox.Yes:
                self.auto_send_cold.setChecked(False)

        self.ctx.db.set_bot_field(self.bot_id, settings=bot_settings.dumps({
            "send_gap_seconds": self.gap.value(),
            "dm_cooldown_seconds": self.cooldown.value(),
            "max_reminders_per_tick": self.cap.value(),
            "max_per_hour": self.per_hour.value(),
            "max_per_day": self.per_day.value(),
            "max_first_messages_per_day": self.first_per_day.value(),
            "contact_cooldown_days": self.contact_cooldown_days.value(),
            "quiet_start": self.quiet_start.time().toString("HH:mm"),
            "quiet_end": self.quiet_end.time().toString("HH:mm"),
            "quiet_weekends": self.quiet_weekends.isChecked(),
            "dry_run": self.dry_run.isChecked(),
            "auto_send_cold": self.auto_send_cold.isChecked(),
        }))
        if self.account_combo is not None:
            self.ctx.db.set_bot_field(self.bot_id, account_id=self.account_combo.currentData())
            QMessageBox.information(
                self, "Аккаунт бота",
                "Смена аккаунта вступит в силу после остановки и повторного запуска бота.")
        self.accept()


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


class BotCard(QWidget):
    """One bot as a grid card (design-brief.md §4.7): status-striped
    `Card`, identity + status pill, an error banner when `last_error` is
    set, three metrics under a top divider, and actions. The three
    metrics (rules / leads / replies sent in the last 24h) are all
    already computable from existing `db` methods (`list_triggers`,
    `list_leads`, `outbox_counts`) — no new `count_bot_*` methods needed,
    same reasoning as `settings.py`'s Д5 media/backup metrics."""

    def __init__(self, ctx: AppContext, bot_id: int, on_changed, navigate):
        super().__init__()
        self.ctx = ctx
        self.bot_id = bot_id
        self.on_changed = on_changed
        self.navigate = navigate

        self.frame = Card()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.frame)

        lay = QVBoxLayout(self.frame)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.title_label = QLabel("")
        self.title_label.setTextFormat(Qt.PlainText)
        self.title_label.setStyleSheet("font-size: 14px; font-weight: 600;")
        top.addWidget(self.title_label, 1)
        self.status_pill = StatusPill("stopped")
        top.addWidget(self.status_pill)
        lay.addLayout(top)

        self.subtitle_label = QLabel("")
        self.subtitle_label.setTextFormat(Qt.PlainText)
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setStyleSheet(f"font-size: 11.5px; color: {theme.TEXT_MUTED};")
        lay.addWidget(self.subtitle_label)

        # last_error can echo text from an exception raised by a 3rd-party
        # library (aiogram/Telethon) — plain text only, same reasoning as
        # every other label showing content this app doesn't fully control.
        self.error_label = QLabel("")
        self.error_label.setTextFormat(Qt.PlainText)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(
            "background: rgba(200,90,110,.12); border: 1px solid rgba(200,90,110,.25); "
            f"border-radius: 8px; padding: 6px 9px; color: {theme.BAD_FG}; font-size: 11.5px;"
        )
        self.error_label.hide()
        lay.addWidget(self.error_label)

        lay.addWidget(hline())
        metrics_row = QHBoxLayout()
        metrics_row.setSpacing(18)
        self.st_rules = StatCell("ПРАВИЛ")
        self.st_leads = StatCell("ЗАЯВОК")
        self.st_replies = StatCell("ОТВЕТОВ 24Ч")
        for cell in (self.st_rules, self.st_leads, self.st_replies):
            metrics_row.addWidget(cell)
        metrics_row.addStretch(1)
        lay.addLayout(metrics_row)

        btn_row1 = QHBoxLayout()
        btn_row1.setSpacing(6)
        self.toggle_btn = button("Запустить", "primary")
        self.toggle_btn.clicked.connect(self._on_toggle)
        btn_row1.addWidget(self.toggle_btn)
        self.rules_btn = button("Правила", "secondary")
        self.rules_btn.clicked.connect(lambda: self.navigate("rules"))
        btn_row1.addWidget(self.rules_btn)
        btn_row1.addStretch(1)
        lay.addLayout(btn_row1)

        btn_row2 = QHBoxLayout()
        btn_row2.setSpacing(6)
        self.limits_btn = button("Отправка", "ghost")
        self.limits_btn.clicked.connect(self._on_limits)
        btn_row2.addWidget(self.limits_btn)
        btn_row2.addStretch(1)
        self.delete_btn = button("Удалить", "danger")
        self.delete_btn.clicked.connect(self._on_delete)
        btn_row2.addWidget(self.delete_btn)
        lay.addLayout(btn_row2)

    def refresh(self) -> None:
        db = self.ctx.db
        bot = db.get_bot(self.bot_id)
        if not bot:
            return
        self.title_label.setText(bot["name"])
        self.status_pill.set_status(bot["status"])
        self.frame.set_stripe_color(_STATUS_STRIPE.get(bot["status"], theme.TEXT_FAINT))
        self.subtitle_label.setText(
            f"{_TYPE_LABELS.get(bot['type'], bot['type'])} · "
            f"пресет {_PRESET_LABELS.get(bot['preset'], bot['preset'])} · "
            f"менеджер: {bot['manager_chat_id'] or 'не задан'}"
        )
        if bot["last_error"]:
            self.error_label.setText(bot["last_error"])
            self.error_label.show()
        else:
            self.error_label.hide()

        self.st_rules.set_value(str(len(db.list_triggers(self.bot_id))))
        self.st_leads.set_value(str(len(db.list_leads(bot_id=self.bot_id))))
        self.st_replies.set_value(str(db.outbox_counts(self.bot_id)["day"]))

        running = bot["status"] == "running"
        self.toggle_btn.setText("Остановить" if running else "Запустить")
        self.toggle_btn.setProperty("class", "secondary" if running else "primary")
        self.toggle_btn.style().unpolish(self.toggle_btn)
        self.toggle_btn.style().polish(self.toggle_btn)

    def _on_limits(self) -> None:
        if SendLimitsDialog(self.ctx, self.bot_id, parent=self).exec() == QDialog.Accepted:
            self.refresh()

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


class _NewBotCard(QFrame):
    """design-brief.md §4.7's dashed placeholder — the grid's last cell,
    and (when there are no bots at all) its only cell."""

    clicked = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("newbotcard")
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(150)
        self.setStyleSheet(
            f"QFrame#newbotcard {{ border: 1px dashed {theme.BORDER_HOVER}; "
            "border-radius: 11px; background: transparent; }\n"
            f"QFrame#newbotcard:hover {{ border-color: {theme.ACCENT}; }}"
        )
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignCenter)
        lay.setSpacing(8)
        self._icon_label = QLabel()
        self._icon_label.setAlignment(Qt.AlignCenter)
        lay.addWidget(self._icon_label)
        self._text_label = label("Новый бот из пресета B2B / B2C")
        self._text_label.setWordWrap(True)
        self._text_label.setAlignment(Qt.AlignCenter)
        lay.addWidget(self._text_label)
        self._set_hovered(False)

    def _set_hovered(self, hovered: bool) -> None:
        color = theme.ACCENT if hovered else theme.TEXT_FAINT
        icon = nav_icon("bots", color, size=22)
        self._icon_label.setPixmap(icon.pixmap(22, 22) if icon else QPixmap())
        text_color = theme.ACCENT_300 if hovered else theme.TEXT_MUTED
        self._text_label.setStyleSheet(f"font-size: 12.5px; color: {text_color};")

    def enterEvent(self, event) -> None:  # noqa: N802
        self._set_hovered(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._set_hovered(False)
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.clicked.emit()
        super().mousePressEvent(event)


class BotsListTab(QWidget):
    # design-brief.md §9.6: at 980×620 (the app's own minimum, main_window.py)
    # the tile grid reflows to 2 columns, narrower still to 1 — not squeezed
    # into a sliver third column the way a fixed `range(3)` grid would.
    _MIN_TILE_WIDTH = 270

    def __init__(self, ctx: AppContext, navigate=None):
        super().__init__()
        self.ctx = ctx
        self.navigate = navigate or (lambda *_a, **_k: None)
        self.cards: dict[int, BotCard] = {}
        self._new_bot_card: _NewBotCard | None = None
        self._columns = 3

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

        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(12)
        self.grid.setVerticalSpacing(12)
        outer.addLayout(self.grid)

        ctx.bot_manager.bots_changed.connect(self.refresh)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(3000)

    def _update_columns(self) -> bool:
        columns = max(1, min(3, self.width() // self._MIN_TILE_WIDTH))
        if columns == self._columns:
            return False
        self._columns = columns
        return True

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._update_columns():
            self.refresh()

    def on_show(self) -> None:
        self._update_columns()
        self.refresh()

    def _on_add(self) -> None:
        dlg = BotWizardDialog(self.ctx, parent=self)
        if dlg.exec() == QDialog.Accepted:
            self.refresh()

    def refresh(self) -> None:
        db = self.ctx.db
        bots = db.list_bots()

        running = len([b for b in bots if b["status"] == "running"])
        total_leads = len(db.list_leads())
        self.summary_label.setText(
            f"{running} из {len(bots)} работает · {total_leads} заявок всего" if bots else
            "ни одного бота ещё не создано"
        )

        current_ids = {b["id"] for b in bots}
        for bid in list(self.cards):
            if bid not in current_ids:
                gone = self.cards.pop(bid)
                gone.setParent(None)
                gone.deleteLater()

        # Detach everything from the grid (widgets survive — just get
        # reparented out) so positions can be recomputed cleanly each
        # tick, same as bots are added/removed/reordered underneath —
        # also the mechanism a column-count change (resizeEvent) reflows
        # through, not just an add/remove/reorder.
        while self.grid.count():
            item = self.grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
        for col in range(3):
            self.grid.setColumnStretch(col, 1 if col < self._columns else 0)

        for i, bot in enumerate(bots):
            bot_card = self.cards.get(bot["id"])
            if bot_card is None:
                bot_card = BotCard(self.ctx, bot["id"], self.refresh, self.navigate)
                self.cards[bot["id"]] = bot_card
            bot_card.refresh()
            self.grid.addWidget(bot_card, i // self._columns, i % self._columns, Qt.AlignTop)

        if self._new_bot_card is None:
            self._new_bot_card = _NewBotCard()
            self._new_bot_card.clicked.connect(self._on_add)
        n = len(bots)
        self.grid.addWidget(self._new_bot_card, n // self._columns, n % self._columns, Qt.AlignTop)
