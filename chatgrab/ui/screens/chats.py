from __future__ import annotations

from PySide6.QtCore import Signal, Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QRadioButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from .. import theme
from ..context import AppContext
from ..util import fire
from ..widgets import Sparkline, StatusPill, ToggleSwitch, button, h1, icon_button, label, muted, skeleton_rows


class AddChatDialog(QDialog):
    def __init__(self, ctx: AppContext, dialogs: list | None = None,
                 dialogs_error: str | None = None, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle("Добавить чат")
        self.setMinimumWidth(520)
        self.chosen_dialog = None

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Ссылка или имя чата"))
        self.link_input = QLineEdit()
        self.link_input.setPlaceholderText("t.me/имя_чата, t.me/+инвайт-код или @имя_чата")
        self.link_input.textChanged.connect(lambda: setattr(self, "chosen_dialog", None))
        lay.addWidget(self.link_input)

        depth_row = QHBoxLayout()
        self.depth_all = QRadioButton("Вся история с начала")
        self.depth_all.setChecked(True)
        self.depth_from = QRadioButton("С определённой даты")
        depth_row.addWidget(self.depth_all)
        depth_row.addWidget(self.depth_from)
        lay.addLayout(depth_row)
        self.depth_date = QLineEdit()
        self.depth_date.setPlaceholderText("ГГГГ-ММ-ДД")
        self.depth_date.setEnabled(False)
        self.depth_from.toggled.connect(self.depth_date.setEnabled)
        lay.addWidget(self.depth_date)

        # Выбор аккаунта показывается только когда их больше одного —
        # иначе это лишний вопрос там, где ответ всегда один.
        self.account_combo = None
        accounts = self.ctx.db.list_accounts()
        if len(accounts) > 1:
            acc_row = QHBoxLayout()
            acc_row.addWidget(QLabel("Собирать аккаунтом"))
            self.account_combo = QComboBox()
            for acc in accounts:
                suffix = " · основной" if acc["is_default"] else ""
                self.account_combo.addItem(acc["name"] + suffix, acc["id"])
            acc_row.addWidget(self.account_combo, 1)
            lay.addLayout(acc_row)
            # Список диалогов принадлежит аккаунту: показывать чаты одного
            # номера, а собирать другим — верный способ добавить чат, в
            # котором выбранный аккаунт не состоит.
            self.account_combo.currentIndexChanged.connect(lambda _i: self._reload_dialogs())

        picker_row = QHBoxLayout()
        picker_row.addWidget(QLabel("Или выберите из своих чатов"))
        picker_row.addStretch(1)
        self.refresh_dialogs_btn = button("Обновить список", "ghost")
        self.refresh_dialogs_btn.clicked.connect(self._reload_dialogs)
        picker_row.addWidget(self.refresh_dialogs_btn)
        lay.addLayout(picker_row)

        self.dialogs_status = QLabel("")
        self.dialogs_status.setWordWrap(True)
        lay.addWidget(self.dialogs_status)

        self.dialog_list = QListWidget()
        self.dialog_list.setMaximumHeight(210)
        self.dialog_list.itemClicked.connect(self._on_pick)
        lay.addWidget(self.dialog_list)

        # design-brief.md §7 «Загрузка данных для UI»: скелетон-строки
        # вместо списка, пока «Обновить список» ждёт Telegram — тот же
        # слот, что и у dialog_list, просто одно видимо, другое скрыто.
        self.dialog_skeleton = skeleton_rows(3, height=28, spacing=6)
        self.dialog_skeleton.setVisible(False)
        lay.addWidget(self.dialog_skeleton)

        self.hint = QLabel(
            "История нового чата встанет в общую очередь — чаты грузятся по одному, "
            "чтобы Telegram не останавливал сбор. Для приватных чатов по ссылке-приглашению "
            "(t.me/+…) приложение присоединится к чату от вашего имени — иначе историю не прочитать."
        )
        self.hint.setWordWrap(True)
        self.hint.setProperty("class", "muted")
        lay.addWidget(self.hint)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel_btn = button("Отмена", "secondary")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        self.confirm_btn = button("Добавить и начать сбор", "primary")
        self.confirm_btn.clicked.connect(self._on_confirm)
        btn_row.addWidget(self.confirm_btn)
        lay.addLayout(btn_row)

        self._dialogs: list = []
        if dialogs_error:
            self._show_dialogs_error(dialogs_error)
        else:
            self._populate_dialogs(dialogs or [])

    def _reload_dialogs(self) -> None:
        self.dialogs_status.setText("")
        self.dialog_list.setVisible(False)
        self.dialog_skeleton.setVisible(True)
        self.refresh_dialogs_btn.setEnabled(False)
        account_id = self.account_combo.currentData() if self.account_combo else None
        service = self.ctx.tg
        if account_id is not None and self.ctx.accounts is not None:
            service = self.ctx.accounts.service_for(account_id)

        async def go():
            return await service.list_dialogs()

        def on_error(e):
            self.refresh_dialogs_btn.setEnabled(True)
            from ...telegram.errors import humanize_error
            self._show_dialogs_error(humanize_error(e))

        task = fire(go(), parent=self, on_error=on_error)

        def _apply(t):
            self.refresh_dialogs_btn.setEnabled(True)
            if not t.cancelled() and t.exception() is None:
                self._populate_dialogs(t.result())

        task.add_done_callback(_apply)

    def _show_dialogs_error(self, message: str) -> None:
        self.dialog_skeleton.setVisible(False)
        self.dialog_list.setVisible(True)
        self.dialogs_status.setText(
            f"Не удалось получить список ваших чатов: {message} "
            "Можно добавить чат вручную по ссылке выше."
        )
        self.dialogs_status.setStyleSheet(f"color: {theme.WARN}; font-size: 12px;")
        self.dialog_list.clear()

    def _populate_dialogs(self, dialogs: list) -> None:
        self.dialog_skeleton.setVisible(False)
        self.dialog_list.setVisible(True)
        self._dialogs = dialogs
        self.dialog_list.clear()
        if not dialogs:
            self.dialogs_status.setText(
                "Среди ваших диалогов не нашлось групп или каналов — добавьте чат вручную по ссылке выше."
            )
            self.dialogs_status.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 12px;")
            return
        self.dialogs_status.setText("")
        for d in dialogs:
            members = f"{d.members} участников" if d.members else ""
            item = QListWidgetItem(f"{d.title}   —   {members}")
            item.setData(Qt.UserRole, d)
            self.dialog_list.addItem(item)

    def _on_pick(self, item: QListWidgetItem) -> None:
        self.chosen_dialog = item.data(Qt.UserRole)
        self.link_input.blockSignals(True)
        self.link_input.setText("")
        self.link_input.blockSignals(False)

    def _on_confirm(self) -> None:
        depth_mode = "from_date" if self.depth_from.isChecked() else "all"
        depth_date = self.depth_date.text().strip() or None if depth_mode == "from_date" else None
        self.confirm_btn.setEnabled(False)

        account_id = self.account_combo.currentData() if self.account_combo else None

        async def go():
            if self.chosen_dialog is not None:
                await self.ctx.collector.add_chat_from_dialog(
                    self.chosen_dialog, depth_mode, depth_date, account_id)
            else:
                link = self.link_input.text().strip()
                if not link:
                    raise ValueError("Укажите ссылку/имя чата или выберите чат из списка.")
                await self.ctx.collector.add_chat_by_link(
                    link, depth_mode, depth_date, account_id)

        def on_error(e):
            self.confirm_btn.setEnabled(True)
            from ...telegram.errors import humanize_error
            QMessageBox.warning(self, "Не получилось добавить чат", humanize_error(e))

        def on_done():
            self.accept()

        fire(go(), parent=self, on_error=on_error, on_done=on_done)


class RemoveChatDialog(QDialog):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Убрать чат из списка")
        self.result_purge: bool | None = None
        lay = QVBoxLayout(self)
        body = QLabel(f"«{title}» перестанет отслеживаться. Что сделать с уже собранными сообщениями?")
        # `title` is the chat's own Telegram title (admin-controlled) —
        # keep it literal, not auto-detected as rich text.
        body.setTextFormat(Qt.PlainText)
        body.setWordWrap(True)
        lay.addWidget(body)
        row = QHBoxLayout()
        purge_btn = button("Удалить данные тоже", "ghost")
        purge_btn.clicked.connect(self._purge)
        row.addWidget(purge_btn)
        row.addStretch(1)
        cancel_btn = button("Отмена", "secondary")
        cancel_btn.clicked.connect(self.reject)
        row.addWidget(cancel_btn)
        keep_btn = button("Оставить данные", "primary")
        keep_btn.clicked.connect(self._keep)
        row.addWidget(keep_btn)
        lay.addLayout(row)

    def _purge(self) -> None:
        self.result_purge = True
        self.accept()

    def _keep(self) -> None:
        self.result_purge = False
        self.accept()


# Колонки строки-карточки (design-brief.md §4.3): 1fr | 190 | 130 | 110 | 150 | 90
_COL_SPARK = 190
_COL_COUNT = 130
_COL_LAST = 110
_COL_STATE = 150
_COL_ACTIONS = 90


class _ChatRow(QFrame):
    """One row of the "table becomes a card" list (design-brief.md §4.3):
    a 2px left stripe colored by status, name+handle, a 30-day sparkline,
    message count, last-message date, a centered `StatusPill`, and a
    toggle + remove button. Double-click opens «Сбор» for this chat."""

    doubleClicked = Signal()

    def __init__(self, db, chat: dict, on_toggle, on_remove):
        super().__init__()
        self.chat_id = chat["chat_id"]
        self.setProperty("class", "tablerow")
        self.setCursor(Qt.PointingHandCursor)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 11, 16, 11)
        lay.setSpacing(14)

        self.stripe = QWidget()
        self.stripe.setFixedWidth(2)
        lay.addWidget(self.stripe)
        lay.addSpacing(18)

        name_col = QVBoxLayout()
        name_col.setSpacing(2)
        self.title_label = label(chat["title"])
        self.title_label.setWordWrap(False)
        self.title_label.setStyleSheet("font-size: 13px;")
        name_col.addWidget(self.title_label)
        self.handle_label = label(f"@{chat['username'] or '—'}")
        self.handle_label.setStyleSheet(
            f"font-family: {theme.FONT_MONO}; font-size: 10.5px; color: {theme.TEXT_FAINT};"
        )
        name_col.addWidget(self.handle_label)
        name_wrap = QWidget()
        name_wrap.setLayout(name_col)
        lay.addWidget(name_wrap, 1)

        self._spark_series = db.activity_bars(chat["chat_id"], days=30)
        self.spark = Sparkline(self._spark_series, height=24)
        self.spark.setFixedWidth(_COL_SPARK)
        lay.addWidget(self.spark)

        self.count_label = label(_fmt_count(db.message_count(chat["chat_id"])))
        self.count_label.setFixedWidth(_COL_COUNT)
        self.count_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.count_label.setStyleSheet(f"font-family: {theme.FONT_MONO}; font-size: 15px;")
        lay.addWidget(self.count_label)

        last = db.last_message_date(chat["chat_id"])
        self.last_label = label(str(last)[:16].replace("T", " ") if last else "—")
        self.last_label.setFixedWidth(_COL_LAST)
        self.last_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.last_label.setStyleSheet(
            f"font-family: {theme.FONT_MONO}; font-size: 11px; color: {theme.TEXT_MUTED};"
        )
        lay.addWidget(self.last_label)

        pill_wrap = QWidget()
        pill_wrap.setFixedWidth(_COL_STATE)
        pl = QHBoxLayout(pill_wrap)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setAlignment(Qt.AlignCenter)
        self.pill = StatusPill(chat["status"])
        pl.addWidget(self.pill)
        lay.addWidget(pill_wrap)

        actions_wrap = QWidget()
        actions_wrap.setFixedWidth(_COL_ACTIONS)
        al = QHBoxLayout(actions_wrap)
        al.setContentsMargins(0, 0, 0, 0)
        al.setSpacing(6)
        al.addStretch(1)
        self.toggle = ToggleSwitch(bool(chat["enabled"]))
        self.toggle.toggled.connect(lambda v, cid=self.chat_id: on_toggle(cid, v))
        al.addWidget(self.toggle)
        self.remove_btn = icon_button("✕", "Убрать из списка")
        self.remove_btn.clicked.connect(lambda: on_remove(self.chat_id, chat["title"]))
        al.addWidget(self.remove_btn)
        lay.addWidget(actions_wrap)

        self.apply_status(chat["status"])

    def apply_status(self, status: str) -> None:
        s = theme.STATUS_STYLES.get(status, theme.STATUS_STYLES["idle"])
        self.stripe.setStyleSheet(f"background: {s['dot']}; border-radius: 1px;")
        self.pill.set_status(status)

    def update_spark(self, values: list[int]) -> None:
        # Д11/§5: Sparkline replays its grow-from-bottom entrance animation
        # on every set_values() — calling it unconditionally on this row's
        # 2-second refresh() tick (see `ChatsScreen.refresh` below) would
        # replay it forever, exactly the periodic-timer flicker §5 warns
        # against. Only push a new series when the 30-day activity actually
        # changed since the last tick.
        if values != self._spark_series:
            self._spark_series = values
            self.spark.set_values(values)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.doubleClicked.emit()


def _fmt_count(n: int) -> str:
    return f"{n:,}".replace(",", " ") if n else "—"


class _TableHeader(QWidget):
    """Кикеры над списком строк — те же колонки, что и у `_ChatRow`."""

    def __init__(self):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(20, 9, 16, 9)
        lay.setSpacing(14)
        lay.addSpacing(2 + 18)

        def kicker(text: str) -> QLabel:
            return label(text, "kicker")

        chat_k = kicker("ЧАТ")
        lay.addWidget(chat_k, 1)
        act_k = kicker("АКТИВНОСТЬ · 30 СУТОК")
        act_k.setFixedWidth(_COL_SPARK)
        lay.addWidget(act_k)
        count_k = kicker("СОБРАНО")
        count_k.setFixedWidth(_COL_COUNT)
        count_k.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(count_k)
        last_k = kicker("ПОСЛЕДНЕЕ")
        last_k.setFixedWidth(_COL_LAST)
        last_k.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(last_k)
        state_k = kicker("СОСТОЯНИЕ")
        state_k.setFixedWidth(_COL_STATE)
        state_k.setAlignment(Qt.AlignCenter)
        lay.addWidget(state_k)
        gather_k = kicker("СБОР")
        gather_k.setFixedWidth(_COL_ACTIONS)
        gather_k.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(gather_k)
        self.setStyleSheet(f"QWidget {{ border-bottom: 1px solid {theme.DIVIDER}; }}")


class ChatsScreen(QWidget):
    def __init__(self, ctx: AppContext, navigate):
        super().__init__()
        self.ctx = ctx
        self.navigate = navigate
        self._rows: dict[int, _ChatRow] = {}
        self._filter_text = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 28, 40, 28)

        header = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.addWidget(h1("Источники"))
        self.summary_label = muted("")
        title_col.addWidget(self.summary_label)
        header.addLayout(title_col)
        header.addStretch(1)

        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("Фильтр по названию")
        self.filter_input.setFixedWidth(210)
        self.filter_input.setStyleSheet(f"background: {theme.SURFACE_INPUT};")
        self.filter_input.textChanged.connect(self._on_filter_changed)
        header.addWidget(self.filter_input, alignment=Qt.AlignVCenter)
        header.addSpacing(8)
        self.add_chat_btn = button("＋ Добавить чат", "primary")
        self.add_chat_btn.clicked.connect(self._on_add_chat)
        header.addWidget(self.add_chat_btn, alignment=Qt.AlignVCenter)
        outer.addLayout(header)
        outer.addSpacing(16)

        # "Таблица становится карточкой" (design-brief.md §4.3): один
        # `class="card"` контейнер, внутри — шапка-кикер и прокручиваемый
        # список строк-виджетов, а не QTableWidget.
        table_card = QFrame()
        table_card.setProperty("class", "card")
        table_lay = QVBoxLayout(table_card)
        table_lay.setContentsMargins(0, 0, 0, 0)
        table_lay.setSpacing(0)
        table_lay.addWidget(_TableHeader())

        self.rows_scroll = QScrollArea()
        self.rows_scroll.setWidgetResizable(True)
        self.rows_scroll.setFrameShape(QFrame.NoFrame)
        rows_host = QWidget()
        self.rows_lay = QVBoxLayout(rows_host)
        self.rows_lay.setContentsMargins(0, 0, 0, 0)
        self.rows_lay.setSpacing(0)
        self.rows_lay.addStretch(1)
        self.rows_scroll.setWidget(rows_host)
        table_lay.addWidget(self.rows_scroll, 1)
        outer.addWidget(table_card, 1)
        outer.addSpacing(10)

        self.empty_label = muted(
            "Пока нет ни одного отслеживаемого чата — добавьте первый кнопкой выше."
        )
        self.empty_label.hide()
        outer.addWidget(self.empty_label)

        footer = muted(
            "Выключенный сбор не удаляет уже собранное — данные чата остаются в базе. "
            "Двойной клик по строке открывает «Сбор данных»."
        )
        footer.setWordWrap(True)
        outer.addWidget(footer)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(2000)
        ctx.collector.chats_changed.connect(self.refresh)

    def on_show(self, **kwargs) -> None:
        self.refresh()

    def _on_add_chat(self) -> None:
        # Fetch the account's dialogs *before* opening the modal dialog —
        # QDialog.exec() starts a nested Qt event loop, and depending on
        # the Qt/qasync combination that can delay or starve pending
        # asyncio callbacks, which used to leave the "your chats" list
        # stuck empty with no explanation. Loading first sidesteps that
        # entirely and lets us show a real error if it fails.
        self.add_chat_btn.setEnabled(False)

        async def go():
            return await self.ctx.tg.list_dialogs()

        def on_error(e):
            self.add_chat_btn.setEnabled(True)
            from ...telegram.errors import humanize_error
            dlg = AddChatDialog(self.ctx, dialogs_error=humanize_error(e), parent=self)
            if dlg.exec() == QDialog.Accepted:
                self.refresh()

        task = fire(go(), parent=self, on_error=on_error)

        def _open(t):
            self.add_chat_btn.setEnabled(True)
            if t.cancelled() or t.exception() is not None:
                return
            dlg = AddChatDialog(self.ctx, dialogs=t.result(), parent=self)
            if dlg.exec() == QDialog.Accepted:
                self.refresh()

        task.add_done_callback(_open)

    def _on_filter_changed(self, text: str) -> None:
        self._filter_text = text.strip().lower()
        self._apply_filter()

    def _apply_filter(self) -> None:
        for row in self._rows.values():
            if not self._filter_text:
                row.setVisible(True)
                continue
            haystack = f"{row.title_label.text()} {row.handle_label.text()}".lower()
            row.setVisible(self._filter_text in haystack)

    def refresh(self) -> None:
        db = self.ctx.db
        chats = db.list_chats()
        enabled_n = len([c for c in chats if c["enabled"]])
        total_msgs = sum(db.message_count(c["chat_id"]) for c in chats)
        total_media = sum(db.media_count(c["chat_id"]) for c in chats)
        self.summary_label.setText(
            f"{enabled_n} из {len(chats)} в работе · {_fmt_count(total_msgs)} сообщений в базе · "
            f"{_fmt_count(total_media)} медиафайлов"
        )

        self.empty_label.setVisible(not chats)
        self.rows_scroll.setVisible(bool(chats))

        seen = set()
        for chat in chats:
            chat_id = chat["chat_id"]
            seen.add(chat_id)
            row = self._rows.get(chat_id)
            if row is None:
                row = _ChatRow(db, chat, self._on_toggle, self._on_remove)
                row.doubleClicked.connect(lambda cid=chat_id: self.navigate("collect", chat_id=cid))
                self.rows_lay.insertWidget(self.rows_lay.count() - 1, row)
                self._rows[chat_id] = row
            else:
                row.count_label.setText(_fmt_count(db.message_count(chat_id)))
                last = db.last_message_date(chat_id)
                row.last_label.setText(str(last)[:16].replace("T", " ") if last else "—")
                row.apply_status(chat["status"])
                row.toggle.set_checked(bool(chat["enabled"]))
                row.update_spark(db.activity_bars(chat_id, days=30))

        for chat_id in list(self._rows):
            if chat_id not in seen:
                row = self._rows.pop(chat_id)
                row.setParent(None)
                row.deleteLater()

        self._apply_filter()

    def _on_toggle(self, chat_id: int, value: bool) -> None:
        # chats_changed fires from set_chat_enabled() and triggers a full
        # refresh() below — this is what makes the row's status/stripe
        # switch immediately rather than waiting for the next 2s timer
        # tick (design-brief.md §4.3's explicit requirement).
        self.ctx.collector.set_chat_enabled(chat_id, value)

    def _on_remove(self, chat_id: int, title: str) -> None:
        dlg = RemoveChatDialog(title, parent=self)
        if dlg.exec() == QDialog.Accepted and dlg.result_purge is not None:
            self.ctx.collector.remove_chat(chat_id, purge=dlg.result_purge)
            self.refresh()
