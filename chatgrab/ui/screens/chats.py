from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QRadioButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..context import AppContext
from ..util import fire
from ..widgets import StatusPill, ToggleSwitch, button, h1, muted


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
        self.dialogs_status.setText("Загружаю список ваших чатов…")
        self.dialogs_status.setStyleSheet("color: #9a9aa3; font-size: 12px;")
        self.refresh_dialogs_btn.setEnabled(False)

        async def go():
            return await self.ctx.tg.list_dialogs()

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
        self.dialogs_status.setText(
            f"Не удалось получить список ваших чатов: {message} "
            "Можно добавить чат вручную по ссылке выше."
        )
        self.dialogs_status.setStyleSheet("color: #f0c6a0; font-size: 12px;")
        self.dialog_list.clear()

    def _populate_dialogs(self, dialogs: list) -> None:
        self._dialogs = dialogs
        self.dialog_list.clear()
        if not dialogs:
            self.dialogs_status.setText(
                "Среди ваших диалогов не нашлось групп или каналов — добавьте чат вручную по ссылке выше."
            )
            self.dialogs_status.setStyleSheet("color: #9a9aa3; font-size: 12px;")
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

        async def go():
            if self.chosen_dialog is not None:
                await self.ctx.collector.add_chat_from_dialog(self.chosen_dialog, depth_mode, depth_date)
            else:
                link = self.link_input.text().strip()
                if not link:
                    raise ValueError("Укажите ссылку/имя чата или выберите чат из списка.")
                await self.ctx.collector.add_chat_by_link(link, depth_mode, depth_date)

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


class ChatsScreen(QWidget):
    def __init__(self, ctx: AppContext, navigate):
        super().__init__()
        self.ctx = ctx
        self.navigate = navigate

        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 28, 40, 28)

        header = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.addWidget(h1("Источники"))
        self.summary_label = muted("")
        title_col.addWidget(self.summary_label)
        header.addLayout(title_col)
        header.addStretch(1)
        self.add_chat_btn = button("＋ Добавить чат", "primary")
        self.add_chat_btn.clicked.connect(self._on_add_chat)
        header.addWidget(self.add_chat_btn, alignment=Qt.AlignBottom)
        outer.addLayout(header)
        outer.addSpacing(16)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Чат", "Сообщений", "Медиа", "Последнее", "Состояние", "Сбор"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setShowGrid(False)
        self.table.cellDoubleClicked.connect(self._on_row_open)
        outer.addWidget(self.table, 1)

        footer = muted("Выключенный сбор не удаляет уже собранное — данные чата остаются в базе.")
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

    def _on_row_open(self, row: int, _col: int) -> None:
        item = self.table.item(row, 0)
        chat_id = item.data(Qt.UserRole)
        self.navigate("collect", chat_id=chat_id)

    def refresh(self) -> None:
        db = self.ctx.db
        chats = db.list_chats()
        enabled_n = len([c for c in chats if c["enabled"]])
        total_msgs = sum(db.message_count(c["chat_id"]) for c in chats)
        total_media = sum(db.media_count(c["chat_id"]) for c in chats)
        self.summary_label.setText(
            f"{enabled_n} из {len(chats)} в работе · {total_msgs:,} сообщений в базе · "
            f"{total_media:,} медиафайлов".replace(",", " ")
        )

        self.table.setRowCount(len(chats))
        for row, chat in enumerate(chats):
            depth = "вся история" if chat["depth_mode"] == "all" else f"с {chat['depth_from_date']}"
            title_item = QTableWidgetItem(f"{chat['title']}\n@{chat['username'] or '—'} · {depth}")
            title_item.setData(Qt.UserRole, chat["chat_id"])
            self.table.setItem(row, 0, title_item)

            count = db.message_count(chat["chat_id"])
            self.table.setItem(row, 1, QTableWidgetItem(f"{count:,}".replace(",", " ") if count else "—"))

            media = db.media_count(chat["chat_id"])
            self.table.setItem(row, 2, QTableWidgetItem(f"{media:,}".replace(",", " ") if media else "—"))

            last = db.last_message_date(chat["chat_id"]) or "—"
            self.table.setItem(row, 3, QTableWidgetItem(str(last)[:19].replace("T", " ")))

            pill = StatusPill(chat["status"])
            self.table.setCellWidget(row, 4, pill)

            actions = QWidget()
            a_lay = QHBoxLayout(actions)
            a_lay.setContentsMargins(4, 2, 4, 2)
            toggle = ToggleSwitch(bool(chat["enabled"]))
            toggle.toggled.connect(lambda val, cid=chat["chat_id"]: self.ctx.collector.set_chat_enabled(cid, val))
            a_lay.addWidget(toggle)
            remove_btn = QPushButton("✕")
            remove_btn.setFixedSize(26, 26)
            remove_btn.setCursor(Qt.PointingHandCursor)
            remove_btn.setStyleSheet(
                "QPushButton { border: none; border-radius: 7px; color: #9a9aa3; background: transparent; }"
                "QPushButton:hover { background: rgba(200,90,110,40); color: #e9b3bf; }"
            )
            remove_btn.clicked.connect(lambda _, cid=chat["chat_id"], t=chat["title"]: self._on_remove(cid, t))
            a_lay.addWidget(remove_btn)
            a_lay.addStretch(1)
            self.table.setCellWidget(row, 5, actions)

            self.table.setRowHeight(row, 46)

        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)

    def _on_remove(self, chat_id: int, title: str) -> None:
        dlg = RemoveChatDialog(title, parent=self)
        if dlg.exec() == QDialog.Accepted and dlg.result_purge is not None:
            self.ctx.collector.remove_chat(chat_id, purge=dlg.result_purge)
            self.refresh()
