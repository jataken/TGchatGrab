from __future__ import annotations

from PySide6.QtCore import QTime, Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QMessageBox, QScrollArea, QSpinBox, QTimeEdit, QVBoxLayout, QWidget,
)

from ...context import AppContext
from ...util import fire
from ...widgets import StatusPill, button, card, h1, label, muted, plural as _plural
from ....bots import settings as bot_settings
from .wizard import BotWizardDialog

_TYPE_LABELS = {"bot_api": "отдельный бот-аккаунт", "userbot": "от вашего аккаунта"}
_PRESET_LABELS = {"b2b": "B2B", "b2c": "B2C", "custom": "CUSTOM"}


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
        self.limits_btn = button("Отправка", "secondary")
        self.limits_btn.clicked.connect(self._on_limits)
        top.addWidget(self.limits_btn)
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
