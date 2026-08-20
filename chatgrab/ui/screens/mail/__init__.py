"""П2: три колонки — ящики и папки → список цепочек → чтение. Первый
настоящий экран блока «Почта»; управление ящиками (П1,
ui/screens/mail_settings.py) остаётся вторым пунктом того же блока, а не
переезжает — оно уже было отдельным экраном до этой сессии, см.
main_window.py.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton, QScrollArea,
    QSplitter, QVBoxLayout, QWidget,
)

from ...context import AppContext
from ...format import human_size, short_dt
from ...util import fire, run_blocking
from ...widgets import button, card, chip, h1, muted
from .attachment_view import AttachmentViewerDialog
from .compose import ComposeDialog, DraftsListDialog
from .triage import TriageDialog

# П-4: "Заявка.pdf.exe" must still read as executable — checked against
# every dot-separated suffix, not just the last one, so a double
# extension trick doesn't hide the real one.
_EXECUTABLE_EXTENSIONS = {
    ".exe", ".bat", ".cmd", ".com", ".scr", ".ps1", ".vbs", ".vbe", ".js",
    ".jse", ".jar", ".msi", ".msp", ".sh", ".app", ".apk", ".hta", ".wsf",
}

_SPECIAL_USE_LABELS = {
    "Sent": "Отправленные", "Drafts": "Черновики", "Trash": "Корзина",
    "Junk": "Спам", "Archive": "Архив", "All": "Все письма",
}


def is_executable_attachment(filename: str) -> bool:
    suffixes = [s.lower() for s in re.findall(r"\.[A-Za-z0-9]+", filename or "")]
    return any(s in _EXECUTABLE_EXTENSIONS for s in suffixes)

# «> текст» — самый частый маркер цитаты, плюс два разделителя, которыми
# Outlook и большинство веб-почтовиков подписывают пересланный/
# исходный текст. Эвристика, не парсер: цель — свернуть очевидную цитату
# по умолчанию (инвариант П-3), а не разобрать письмо по стандарту.
_QUOTE_MARKER_RE = re.compile(
    r"^\s*(?:>|-{2,}\s*Original Message\s*-{2,}|-{2,}\s*Пересылаемое сообщение\s*-{2,})",
    re.IGNORECASE | re.MULTILINE,
)

SPLITTER_SETTING_KEY = "mail_screen_splitter"
_FILTER_ALL, _FILTER_UNREAD, _FILTER_ATTACH = "all", "unread", "attach"


def split_quoted(body: str) -> tuple[str, str]:
    """Тело письма -> (свой текст, цитируемый хвост). Хвост пуст, если
    маркер цитаты не найден — тогда сворачивать нечего."""
    text = body or ""
    m = _QUOTE_MARKER_RE.search(text)
    if not m:
        return text, ""
    return text[: m.start()].rstrip(), text[m.start():].strip()


class MessagePane(QWidget):
    """Одно письмо внутри открытой ветки: шапка, тело (цитата свёрнута по
    умолчанию), кнопка «показать оригинал» для HTML — во внешнем
    браузере, не внутри приложения (инвариант П-3)."""

    def __init__(self, ctx: AppContext, message, on_need_body, on_changed=None):
        super().__init__()
        self.ctx = ctx
        self.message = message
        self._on_need_body = on_need_body
        # on_changed(kind, message_id_header, mailbox_id, previous_folder,
        # dest_folder) — MailScreen's hook for refreshing the thread list
        # and offering "Отменить" after a move/trash/permanent-delete.
        self._on_changed = on_changed or (lambda **kw: None)

        frame = card()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(frame)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(6)

        top = QHBoxLayout()
        who = message["sender_name"] or message["sender_address"] or "—"
        who_label = QLabel(who)
        who_label.setTextFormat(Qt.PlainText)
        who_label.setStyleSheet("font-weight: 600;")
        top.addWidget(who_label)
        if message["sender_address"] and message["sender_name"]:
            addr = QLabel(f"<{message['sender_address']}>")
            addr.setTextFormat(Qt.PlainText)
            addr.setProperty("class", "faint")
            top.addWidget(addr)
        top.addStretch(1)

        self.flag_btn = button("★" if message["is_flagged"] else "☆", "ghost")
        self.flag_btn.setToolTip("Отметить важным")
        self.flag_btn.clicked.connect(self._on_toggle_flag)
        top.addWidget(self.flag_btn)

        self.move_btn = button("→", "ghost")
        self.move_btn.setToolTip("Переместить в папку…")
        self.move_btn.clicked.connect(self._on_move_clicked)
        top.addWidget(self.move_btn)

        self.trash_btn = button("🗑", "ghost")
        self._update_trash_button()
        self.trash_btn.clicked.connect(self._on_trash_clicked)
        top.addWidget(self.trash_btn)

        top.addWidget(muted(short_dt(message["date"])))
        lay.addLayout(top)

        reply_row = QHBoxLayout()
        reply_btn = button("Ответить", "ghost")
        reply_btn.clicked.connect(lambda: self._on_compose(kind="reply"))
        reply_row.addWidget(reply_btn)
        reply_all_btn = button("Ответить всем", "ghost")
        reply_all_btn.clicked.connect(lambda: self._on_compose(kind="reply_all"))
        reply_row.addWidget(reply_all_btn)
        forward_btn = button("Переслать", "ghost")
        forward_btn.clicked.connect(lambda: self._on_compose(kind="forward"))
        reply_row.addWidget(forward_btn)
        reply_row.addStretch(1)
        lay.addLayout(reply_row)

        self.body_container = QVBoxLayout()
        self.body_container.setSpacing(6)
        lay.addLayout(self.body_container)
        self._render_body()

    def _render_body(self) -> None:
        while self.body_container.count():
            item = self.body_container.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

        message = self.message
        if not message["body_fetched"]:
            hint = muted("Текст ещё не загружен.")
            self.body_container.addWidget(hint)
            load_btn = button("Загрузить текст", "secondary")
            load_btn.clicked.connect(self._on_load_body)
            self.body_container.addWidget(load_btn)
            return

        main_text, quoted = split_quoted(message["body_text"] or "")
        body_label = QLabel(main_text or "(пусто)")
        body_label.setTextFormat(Qt.PlainText)
        body_label.setWordWrap(True)
        body_label.setStyleSheet("font-size: 13.5px;")
        self.body_container.addWidget(body_label)

        if quoted:
            self.quote_btn = button("Показать цитируемое", "ghost")
            self.quote_btn.setCheckable(True)
            self.quote_btn.toggled.connect(self._on_toggle_quote)
            self.body_container.addWidget(self.quote_btn)
            self.quote_label = QLabel(quoted)
            self.quote_label.setTextFormat(Qt.PlainText)
            self.quote_label.setWordWrap(True)
            self.quote_label.setProperty("class", "muted")
            self.quote_label.setVisible(False)
            self.body_container.addWidget(self.quote_label)

        if message["body_html_path"]:
            html_btn = button("Показать оригинал (в браузере)", "ghost")
            html_btn.clicked.connect(self._open_original)
            self.body_container.addWidget(html_btn)

        attachments = self.ctx.db.list_mail_attachments(message["id"])
        if attachments:
            att_row = QHBoxLayout()
            att_row.addWidget(muted("📎"))
            for att in attachments:
                label = att["filename"] or "вложение"
                if is_executable_attachment(label):
                    label = f"⚠ {label}"  # П-4: исполняемое расширение помечается
                size_text = human_size(att["size_bytes"])
                if size_text:
                    label += f" ({size_text})"
                att_btn = button(label, "ghost")
                att_btn.clicked.connect(lambda _c=False, a=att: self._open_attachment(a))
                att_row.addWidget(att_btn)
            att_row.addStretch(1)
            if len(attachments) > 1:
                save_all_btn = button("Сохранить все…", "ghost")
                save_all_btn.clicked.connect(lambda: self._save_all_attachments(attachments))
                att_row.addWidget(save_all_btn)
            self.body_container.addLayout(att_row)

    def _on_toggle_quote(self, checked: bool) -> None:
        self.quote_label.setVisible(checked)
        self.quote_btn.setText("Скрыть цитируемое" if checked else "Показать цитируемое")

    def _on_load_body(self) -> None:
        self._on_need_body(self.message["id"], self._on_body_loaded)

    def _on_body_loaded(self, message_id: int) -> None:
        refreshed = self.ctx.db.get_mail_message(message_id)
        if refreshed is not None:
            self.message = refreshed
            self._render_body()

    def _open_original(self) -> None:
        path = self.message["body_html_path"]
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _save_all_attachments(self, attachments) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Сохранить вложения в папку")
        if not folder:
            return
        for att in attachments:
            if att["path"] and Path(att["path"]).exists():
                shutil.copy2(att["path"], Path(folder) / (att["filename"] or Path(att["path"]).name))

    def _open_attachment(self, attachment) -> None:
        # П-4: opens a viewer inside the app — never runs the file, no
        # matter what its extension claims to be.
        AttachmentViewerDialog(self.ctx, attachment, parent=self).exec()

    # ---- ответить / ответить всем / переслать (П5) -------------------------
    def _on_compose(self, kind: str) -> None:
        message_id = self.message["id"]
        if kind == "reply":
            draft_id = self.ctx.mail_service.start_reply_draft(message_id, reply_all=False)
        elif kind == "reply_all":
            draft_id = self.ctx.mail_service.start_reply_draft(message_id, reply_all=True)
        else:
            draft_id = self.ctx.mail_service.start_forward_draft(message_id)
        if draft_id is None:
            return
        ComposeDialog(self.ctx, draft_id, parent=self).exec()

    # ---- flags / move / trash (П4) ---------------------------------------
    def _update_trash_button(self) -> None:
        self.trash_btn.setToolTip(
            "Удалить окончательно" if self._is_in_trash() else "Переместить в корзину")

    def _is_in_trash(self) -> bool:
        trash = self.ctx.db.get_mail_folder_by_special_use(self.message["mailbox_id"], "Trash")
        return trash is not None and self.message["folder"] == trash["name"]

    def _on_toggle_flag(self) -> None:
        message_id = self.message["id"]
        new_value = not bool(self.message["is_flagged"])
        self.flag_btn.setText("★" if new_value else "☆")  # мгновенный отклик, не дожидаясь сети

        async def _run():
            return await run_blocking(self.ctx.mail_service.set_flagged, message_id, new_value)

        def on_done():
            refreshed = self.ctx.db.get_mail_message(message_id)
            if refreshed is not None:
                self.message = refreshed
            self._on_changed()

        fire(_run(), parent=self, on_error=lambda e: None, on_done=on_done)

    def _on_move_clicked(self) -> None:
        menu = QMenu(self)
        folders = self.ctx.db.list_mail_folders(self.message["mailbox_id"])
        added = 0
        for f in folders:
            if f["name"] == self.message["folder"]:
                continue
            label = f["name"]
            if f["special_use"]:
                label += f" ({_SPECIAL_USE_LABELS.get(f['special_use'], f['special_use'])})"
            action = menu.addAction(label)
            action.triggered.connect(lambda _c=False, n=f["name"]: self._do_move(n))
            added += 1
        if added == 0:
            return
        menu.exec(self.move_btn.mapToGlobal(self.move_btn.rect().bottomLeft()))

    def _do_move(self, dest_folder: str) -> None:
        message_id = self.message["id"]
        message_id_header = self.message["message_id"]
        mailbox_id = self.message["mailbox_id"]
        previous_folder = self.message["folder"]

        async def _run():
            return await run_blocking(self.ctx.mail_service.move_message, message_id, dest_folder)

        def on_done():
            self._on_changed(kind="move", message_id_header=message_id_header, mailbox_id=mailbox_id,
                              previous_folder=previous_folder, dest_folder=dest_folder)

        fire(_run(), parent=self, on_error=lambda e: None, on_done=on_done)

    def _on_trash_clicked(self) -> None:
        message_id = self.message["id"]
        message_id_header = self.message["message_id"]
        mailbox_id = self.message["mailbox_id"]
        previous_folder = self.message["folder"]

        if self._is_in_trash():
            if QMessageBox.question(
                self, "Удалить окончательно", "Удалить письмо навсегда? Отменить нельзя."
            ) != QMessageBox.Yes:
                return

            async def _run_delete():
                return await run_blocking(self.ctx.mail_service.permanently_delete, message_id)

            def on_deleted():
                self._on_changed(kind="delete")

            fire(_run_delete(), parent=self, on_error=lambda e: None, on_done=on_deleted)
            return

        async def _run_trash():
            return await run_blocking(self.ctx.mail_service.move_to_trash, message_id)

        def on_trashed():
            self._on_changed(kind="trash", message_id_header=message_id_header, mailbox_id=mailbox_id,
                              previous_folder=previous_folder, dest_folder="Корзина")

        fire(_run_trash(), parent=self, on_error=lambda e: None, on_done=on_trashed)


class MailScreen(QWidget):
    def __init__(self, ctx: AppContext, navigate):
        super().__init__()
        self.ctx = ctx
        self.navigate = navigate
        self.selected_mailbox_id: int | None = None
        self.selected_folder: str | None = None
        self.selected_thread_id: int | None = None
        self.filter_mode = _FILTER_ALL

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QHBoxLayout()
        header.setContentsMargins(40, 22, 40, 10)
        header.addWidget(h1("Почта"))
        header.addStretch(1)
        self.drafts_btn = button("Черновики", "ghost")
        self.drafts_btn.clicked.connect(self._on_show_drafts)
        header.addWidget(self.drafts_btn)
        self.triage_btn = button("Режим разбора", "ghost")
        self.triage_btn.clicked.connect(self._on_open_triage)
        header.addWidget(self.triage_btn)
        compose_btn = button("Написать", "primary")
        compose_btn.clicked.connect(self._on_compose_new)
        header.addWidget(compose_btn)
        manage_btn = button("Ящики", "secondary")
        manage_btn.clicked.connect(lambda: self.navigate("mail_settings"))
        header.addWidget(manage_btn)
        header_widget = QWidget()
        header_widget.setLayout(header)
        outer.addWidget(header_widget)

        self.splitter = QSplitter(Qt.Horizontal)
        outer.addWidget(self.splitter, 1)

        self.splitter.addWidget(self._build_mailbox_column())
        self.splitter.addWidget(self._build_thread_column())
        self.splitter.addWidget(self._build_reading_column())
        self.splitter.setSizes(self._restore_splitter_sizes())
        self.splitter.splitterMoved.connect(self._save_splitter_sizes)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.timeout.connect(self._load_threads)

    def on_show(self, **kwargs) -> None:
        self._load_mailboxes()

    # ---- написать / черновики (П5) ----------------------------------------
    def _on_compose_new(self) -> None:
        if self.selected_mailbox_id is None:
            QMessageBox.information(self, "Выберите ящик", "Сначала выберите ящик слева.")
            return
        draft_id = self.ctx.mail_service.start_new_draft(self.selected_mailbox_id)
        ComposeDialog(self.ctx, draft_id, parent=self).exec()
        self._load_threads()

    def _on_show_drafts(self) -> None:
        if self.selected_mailbox_id is None:
            QMessageBox.information(self, "Выберите ящик", "Сначала выберите ящик слева.")
            return
        DraftsListDialog(self.ctx, self.selected_mailbox_id, parent=self).exec()
        self._load_threads()

    # ---- режим триажа (П6) ------------------------------------------------
    def _on_open_triage(self) -> None:
        if self.selected_mailbox_id is None:
            QMessageBox.information(self, "Выберите ящик", "Сначала выберите ящик слева.")
            return
        TriageDialog(self.ctx, self.selected_mailbox_id, folder=self.selected_folder,
                     parent=self, on_search=self._focus_search).exec()
        self._load_threads()
        self._load_mailboxes_badges_only()

    def _focus_search(self) -> None:
        self.search_input.setFocus()

    # ---- левая колонка: ящики и папки ----------------------------------
    def _build_mailbox_column(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(24, 0, 12, 16)
        lay.addWidget(muted("ЯЩИКИ"))
        self.mailbox_list = QListWidget()
        self.mailbox_list.currentItemChanged.connect(self._on_mailbox_selected)
        lay.addWidget(self.mailbox_list, 1)
        self.mailbox_empty_hint = muted("")
        self.mailbox_empty_hint.setWordWrap(True)
        self.mailbox_empty_hint.setVisible(False)
        lay.addWidget(self.mailbox_empty_hint)
        return w

    def _load_mailboxes(self) -> None:
        current = (self.selected_mailbox_id, self.selected_folder)
        self.mailbox_list.blockSignals(True)
        self.mailbox_list.clear()
        rows = []
        for mailbox in self.ctx.db.list_mailboxes(enabled_only=True):
            for folder in self.ctx.db.list_mail_folders(mailbox["id"]):
                if folder["enabled"]:
                    rows.append((mailbox, folder))

        if not rows:
            self.mailbox_empty_hint.setText(
                "Пока ни одного синхронизированного ящика. Добавьте его на экране «Ящики».")
            self.mailbox_empty_hint.setVisible(True)
        else:
            self.mailbox_empty_hint.setVisible(False)

        restore_index = 0
        for i, (mailbox, folder) in enumerate(rows):
            unread = self._folder_unread_count(mailbox["id"], folder["name"])
            label = f"{mailbox['address']} — {folder['name']}"
            if unread:
                label += f"  ({unread})"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, (mailbox["id"], folder["name"]))
            self.mailbox_list.addItem(item)
            if (mailbox["id"], folder["name"]) == current:
                restore_index = i
        self.mailbox_list.blockSignals(False)
        if rows:
            self.mailbox_list.setCurrentRow(restore_index)
            self.selected_mailbox_id, self.selected_folder = rows[restore_index][0]["id"], rows[restore_index][1]["name"]
        else:
            self.selected_mailbox_id = self.selected_folder = None
        self._load_threads()

    def _folder_unread_count(self, mailbox_id: int, folder: str) -> int:
        threads = self.ctx.db.list_mail_threads(mailbox_id, folder=folder, unread_only=True)
        return sum(t["unread_count"] for t in threads)

    def _on_mailbox_selected(self, current: QListWidgetItem, _previous) -> None:
        if current is None:
            return
        self.selected_mailbox_id, self.selected_folder = current.data(Qt.UserRole)
        self._load_threads()

    # ---- средняя колонка: список цепочек --------------------------------
    def _build_thread_column(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 0, 12, 16)

        search_row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Поиск по письмам…")
        self.search_input.textChanged.connect(self._debounced_search)
        search_row.addWidget(self.search_input, 1)
        self.search_server_btn = button("Искать на сервере", "ghost")
        self.search_server_btn.clicked.connect(self._on_search_server)
        search_row.addWidget(self.search_server_btn)
        lay.addLayout(search_row)

        chip_row = QHBoxLayout()
        self.filter_group = QButtonGroup(self)
        self.filter_group.setExclusive(True)
        self._filter_chips = {}
        for key, title in ((_FILTER_ALL, "Все"), (_FILTER_UNREAD, "Непрочитанные"),
                           (_FILTER_ATTACH, "С вложениями")):
            btn = chip(title)
            btn.setChecked(key == _FILTER_ALL)
            btn.clicked.connect(lambda _c, k=key: self._on_filter_picked(k))
            self.filter_group.addButton(btn)
            chip_row.addWidget(btn)
            self._filter_chips[key] = btn
        chip_row.addStretch(1)
        self.bulk_label_btn = button("Ярлык на выделенное", "ghost")
        self.bulk_label_btn.clicked.connect(self._on_bulk_label_clicked)
        chip_row.addWidget(self.bulk_label_btn)
        lay.addLayout(chip_row)

        self.search_status = muted("")
        lay.addWidget(self.search_status)

        self.thread_list = QListWidget()
        # П6: «выделение с Shift, ярлык на всё выделенное одним нажатием» —
        # обычный ExtendedSelection читает и Shift-диапазон, и Ctrl-клик
        # вразнобой, ничего своего изобретать не пришлось.
        self.thread_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.thread_list.currentItemChanged.connect(self._on_thread_selected)
        lay.addWidget(self.thread_list, 1)
        return w

    # ---- ярлыки (П6) ------------------------------------------------------
    def _on_bulk_label_clicked(self) -> None:
        if self.selected_mailbox_id is None:
            return
        thread_ids = [item.data(Qt.UserRole) for item in self.thread_list.selectedItems()]
        if not thread_ids:
            QMessageBox.information(self, "Ничего не выделено", "Выделите одну или несколько цепочек слева.")
            return
        labels = self.ctx.db.list_mail_labels(self.selected_mailbox_id)
        if not labels:
            QMessageBox.information(self, "Нет ярлыков", "На этом ящике ещё нет ни одного ярлыка.")
            return
        menu = QMenu(self)
        for lb in labels:
            action = menu.addAction(lb["name"])
            action.triggered.connect(lambda _c=False, i=lb["id"]: self._apply_bulk_label(thread_ids, i))
        menu.exec(self.bulk_label_btn.mapToGlobal(self.bulk_label_btn.rect().bottomLeft()))

    def _apply_bulk_label(self, thread_ids: list[int], label_id: int) -> None:
        async def _run():
            return await run_blocking(self.ctx.mail_service.apply_label_to_threads, thread_ids, label_id)

        def on_done():
            self._load_threads()

        fire(_run(), parent=self, on_error=lambda e: None, on_done=on_done)

    def _on_label_chip_clicked(self, thread_id: int, label_id: int) -> None:
        # «клик по плашке снимает ярлык — без диалога подтверждения»
        async def _run():
            return await run_blocking(self.ctx.mail_service.set_thread_label, thread_id, label_id, False)

        def on_done():
            self._load_threads()

        fire(_run(), parent=self, on_error=lambda e: None, on_done=on_done)

    def _debounced_search(self) -> None:
        self._debounce.start(280)

    def _on_filter_picked(self, key: str) -> None:
        self.filter_mode = key
        self._load_threads()

    def _load_threads(self) -> None:
        self.thread_list.clear()
        if self.selected_mailbox_id is None:
            return
        query = self.search_input.text().strip()
        if query:
            rows = self.ctx.db.search_mail(
                self.selected_mailbox_id, query, folder=self.selected_folder)
            thread_ids_seen = []
            threads = []
            for row in rows:
                if row["thread_id"] not in thread_ids_seen:
                    thread_ids_seen.append(row["thread_id"])
            all_threads = {
                t["thread_id"]: t for t in self.ctx.db.list_mail_threads(
                    self.selected_mailbox_id, folder=self.selected_folder, limit=1000)
            }
            threads = [all_threads[tid] for tid in thread_ids_seen if tid in all_threads]
            self.search_status.setText(f"{len(threads)} цепочек найдено локально.")
        else:
            threads = self.ctx.db.list_mail_threads(
                self.selected_mailbox_id, folder=self.selected_folder,
                unread_only=(self.filter_mode == _FILTER_UNREAD),
                with_attachments_only=(self.filter_mode == _FILTER_ATTACH),
            )
            self.search_status.setText("")

        for t in threads:
            self._add_thread_item(t)

    def _add_thread_item(self, t) -> None:
        unread = t["unread_count"] > 0
        dot = "●  " if unread else "○  "
        star = "★  " if t["has_flagged"] else ""
        clip = "  📎" if t["has_attachments"] else ""
        subject = t["subject"] or "(без темы)"
        who = t["sender_name"] or t["sender_address"] or "—"
        count = f" ({t['message_count']})" if t["message_count"] > 1 else ""
        text = f"{dot}{star}{subject}{count}{clip}\n{who} · {short_dt(t['last_date'])}"
        thread_id = t["thread_id"]

        item = QListWidgetItem()
        item.setData(Qt.UserRole, thread_id)
        self.thread_list.addItem(item)

        # П6: строка — не просто текст QListWidgetItem, а собственный
        # виджет (setItemWidget), потому что цветные плашки ярлыков
        # внутри строки должны сами быть кликабельны ("клик по плашке
        # снимает ярлык"), а не просто нарисованы. _load_mailboxes_
        # badges_only() читает/пишет row_widget.title_label вместо
        # item.text()/item.font() ровно из-за этой смены.
        row_widget = QWidget()
        row_lay = QVBoxLayout(row_widget)
        row_lay.setContentsMargins(4, 4, 4, 4)
        row_lay.setSpacing(3)
        title_label = QLabel(text)
        title_label.setTextFormat(Qt.PlainText)
        font = title_label.font()
        font.setBold(unread)
        title_label.setFont(font)
        row_widget.title_label = title_label
        row_lay.addWidget(title_label)

        labels = self.ctx.db.list_labels_for_thread(thread_id)
        if labels:
            chip_row = QHBoxLayout()
            chip_row.setSpacing(4)
            for lb in labels:
                chip_btn = QPushButton(lb["name"])
                chip_btn.setCursor(Qt.PointingHandCursor)
                chip_btn.setToolTip("Снять ярлык")
                chip_btn.setStyleSheet(
                    f"QPushButton {{ background-color: {lb['color']}; color: white; border: none; "
                    f"border-radius: 8px; padding: 1px 8px; font-size: 11px; }}")
                chip_btn.clicked.connect(
                    lambda _c, tid=thread_id, lid=lb["id"]: self._on_label_chip_clicked(tid, lid))
                chip_row.addWidget(chip_btn)
            chip_row.addStretch(1)
            row_lay.addLayout(chip_row)

        item.setSizeHint(row_widget.sizeHint())
        self.thread_list.setItemWidget(item, row_widget)

    def _on_search_server(self) -> None:
        query = self.search_input.text().strip()
        if not query or self.selected_mailbox_id is None or self.selected_folder is None:
            return
        self.search_server_btn.setEnabled(False)
        self.search_status.setText("Ищу на сервере…")
        mailbox_id, folder = self.selected_mailbox_id, self.selected_folder

        async def _run():
            return await run_blocking(self.ctx.mail_service.search_server, mailbox_id, folder, query)

        def on_error(e):
            self.search_server_btn.setEnabled(True)
            self.search_status.setText(f"Не удалось искать на сервере: {e}")

        task = fire(_run(), parent=self, on_error=on_error)

        def _apply(t):
            self.search_server_btn.setEnabled(True)
            if t.cancelled() or t.exception() is not None:
                return
            pulled = t.result()
            self.search_status.setText(
                f"С сервера подтянуто новых писем: {pulled}." if pulled else "На сервере ничего нового не нашлось.")
            self._load_threads()

        task.add_done_callback(_apply)

    # ---- правая колонка: чтение -----------------------------------------
    def _build_reading_column(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 0, 24, 16)

        self.undo_bar = QWidget()
        undo_lay = QHBoxLayout(self.undo_bar)
        undo_lay.setContentsMargins(0, 0, 0, 8)
        self.undo_label = muted("")
        undo_lay.addWidget(self.undo_label, 1)
        undo_btn = button("Отменить", "ghost")
        undo_btn.clicked.connect(self._on_undo_clicked)
        undo_lay.addWidget(undo_btn)
        self.undo_bar.setVisible(False)
        lay.addWidget(self.undo_bar)
        self._last_action: dict | None = None

        self.reading_subject = QLabel("")
        self.reading_subject.setTextFormat(Qt.PlainText)
        self.reading_subject.setWordWrap(True)
        self.reading_subject.setStyleSheet("font-size: 16px; font-weight: 600;")
        lay.addWidget(self.reading_subject)

        self.reading_scroll = QScrollArea()
        self.reading_scroll.setWidgetResizable(True)
        self.reading_container = QWidget()
        self.reading_lay = QVBoxLayout(self.reading_container)
        self.reading_lay.setSpacing(8)
        self.reading_lay.addStretch(1)
        self.reading_scroll.setWidget(self.reading_container)
        lay.addWidget(self.reading_scroll, 1)

        self.reading_hint = muted("Выберите цепочку слева.")
        lay.insertWidget(1, self.reading_hint)
        self.reading_scroll.setVisible(False)
        return w

    def _on_thread_selected(self, current: QListWidgetItem, _previous) -> None:
        if current is None:
            self.selected_thread_id = None
            return
        self.selected_thread_id = current.data(Qt.UserRole)
        self._render_thread(self.selected_thread_id)
        self._mark_read(self.selected_thread_id)

    def _render_thread(self, thread_id: int) -> None:
        while self.reading_lay.count() > 1:
            item = self.reading_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

        messages = self.ctx.db.list_thread_messages(thread_id)
        if not messages:
            return
        self.reading_hint.setVisible(False)
        self.reading_scroll.setVisible(True)
        self.reading_subject.setText(messages[-1]["subject"] or "(без темы)")
        for message in messages:
            pane = MessagePane(self.ctx, message, self._fetch_body, on_changed=self._on_message_changed)
            self.reading_lay.insertWidget(self.reading_lay.count() - 1, pane)

    def _on_message_changed(self, kind: str | None = None, message_id_header: str | None = None,
                             mailbox_id: int | None = None, previous_folder: str | None = None,
                             dest_folder: str | None = None) -> None:
        # П4: перемещение/удаление отражаются в списке цепочек и в
        # счётчиках слева сразу, без ожидания следующего тика синхрони-
        # зации — то, что и так уже применилось локально в MailService.
        if kind in ("move", "trash") and message_id_header and mailbox_id is not None and previous_folder:
            self._last_action = {
                "message_id_header": message_id_header, "mailbox_id": mailbox_id,
                "previous_folder": previous_folder,
            }
            label = "Перемещено в «Корзина»" if kind == "trash" else f"Перемещено в «{dest_folder}»"
            self.undo_label.setText(label)
            self.undo_bar.setVisible(True)
        elif kind == "delete":
            self._last_action = None
            self.undo_bar.setVisible(False)
        self._load_threads()
        self._load_mailboxes_badges_only()

    def _on_undo_clicked(self) -> None:
        action = self._last_action
        if action is None:
            return
        self._last_action = None
        self.undo_bar.setVisible(False)
        mailbox_id = action["mailbox_id"]
        message = self.ctx.db.get_mail_message_by_message_id(mailbox_id, action["message_id_header"])
        if message is None:
            return  # уже успело измениться дальше — отменять нечего
        message_id = message["id"]
        previous_folder = action["previous_folder"]

        async def _run():
            return await run_blocking(self.ctx.mail_service.move_message, message_id, previous_folder)

        def on_done():
            self._load_threads()
            self._load_mailboxes_badges_only()
            if self.selected_thread_id is not None:
                self._render_thread(self.selected_thread_id)

        fire(_run(), parent=self, on_error=lambda e: None, on_done=on_done)

    def _fetch_body(self, message_id: int, on_done) -> None:
        async def _run():
            return await run_blocking(self.ctx.mail_service.fetch_body, message_id)

        def on_error(e):
            self.search_status.setText(f"Не удалось загрузить текст письма: {e}")

        task = fire(_run(), parent=self, on_error=on_error)

        def _apply(t):
            if t.cancelled() or t.exception() is not None:
                return
            on_done(message_id)

        task.add_done_callback(_apply)

    def _mark_read(self, thread_id: int) -> None:
        changed = self.ctx.db.mark_thread_read(thread_id)
        if not changed or self.selected_mailbox_id is None:
            return
        self._load_mailboxes_badges_only()
        items = [(row["folder"], row["uid"]) for row in changed]
        mailbox_id = self.selected_mailbox_id

        async def _run():
            return await run_blocking(self.ctx.mail_service.push_read_flags, mailbox_id, items)

        fire(_run(), parent=self, on_error=lambda e: None)

    def _load_mailboxes_badges_only(self) -> None:
        """Обновляет счётчики непрочитанного в левой колонке и жирность
        строк в списке цепочек, не трогая текущий выбор — полный
        `_load_threads()` пересоздал бы список и потерял прокрутку ради
        одной строки, у которой просто снялась жирность."""
        for i in range(self.thread_list.count()):
            item = self.thread_list.item(i)
            if item.data(Qt.UserRole) == self.selected_thread_id:
                # П6: строка теперь setItemWidget(), не item.text()/
                # item.font() — см. ту же смену в _add_thread_item().
                row_widget = self.thread_list.itemWidget(item)
                if row_widget is None:
                    continue
                font = row_widget.title_label.font()
                font.setBold(False)
                row_widget.title_label.setFont(font)
                text = row_widget.title_label.text()
                if text.startswith("●"):
                    row_widget.title_label.setText("○" + text[1:])
        for i in range(self.mailbox_list.count()):
            item = self.mailbox_list.item(i)
            mailbox_id, folder = item.data(Qt.UserRole)
            unread = self._folder_unread_count(mailbox_id, folder)
            label = f"{mailbox_id and self.ctx.db.get_mailbox(mailbox_id)['address']} — {folder}"
            if unread:
                label += f"  ({unread})"
            item.setText(label)

    # ---- сохранение пропорций колонок в app_settings ---------------------
    def _restore_splitter_sizes(self) -> list[int]:
        sizes = self.ctx.db.get_setting(SPLITTER_SETTING_KEY, None)
        if isinstance(sizes, list) and len(sizes) == 3:
            return sizes
        return [260, 360, 640]

    def _save_splitter_sizes(self, _pos: int, _index: int) -> None:
        self.ctx.db.set_setting(SPLITTER_SETTING_KEY, self.splitter.sizes())
