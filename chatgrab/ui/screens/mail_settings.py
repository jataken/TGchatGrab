"""Почта → ящики: добавить, проверить подключение, включить/выключить,
удалить, управлять папками (П4).
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QScrollArea, QVBoxLayout, QWidget,
)

from ..context import AppContext
from ..format import short_dt
from ..util import fire, run_blocking
from ..widgets import FieldRow, button, card, h1, muted
from ...integrations.mail import credentials as mail_credentials
from ...integrations.mail.imap_client import autodetect

_SPECIAL_USE_LABELS = {
    "Sent": "Отправленные", "Drafts": "Черновики", "Trash": "Корзина",
    "Junk": "Спам", "Archive": "Архив", "All": "Все письма",
}


class FolderManagerDialog(QDialog):
    """Create/rename/delete/subscribe — one mailbox's folder tree, П4.
    A dialog rather than inline in the mailbox row: folder admin is a
    rare, deliberate action, not something that needs to stay visible
    on every screen load the way the mailbox list itself does."""

    def __init__(self, ctx: AppContext, mailbox_id: int, mailbox_address: str, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.mailbox_id = mailbox_id
        self.setWindowTitle(f"Папки — {mailbox_address}")
        self.resize(520, 560)

        outer = QVBoxLayout(self)

        create_row = QHBoxLayout()
        self.new_name_field = QLineEdit()
        self.new_name_field.setPlaceholderText("Имя новой папки…")
        create_row.addWidget(self.new_name_field, 1)
        create_btn = button("Создать", "secondary")
        create_btn.clicked.connect(self._on_create)
        create_row.addWidget(create_btn)
        outer.addLayout(create_row)

        self.status = muted("")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        self.list_box = QVBoxLayout(inner)
        self.list_box.setSpacing(6)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        close_btn = button("Закрыть", "secondary")
        close_btn.clicked.connect(self.accept)
        outer.addWidget(close_btn)

        self._refresh()

    def _refresh(self) -> None:
        while self.list_box.count():
            item = self.list_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        for folder in self.ctx.db.list_mail_folders(self.mailbox_id):
            self.list_box.addWidget(self._build_folder_row(folder))
        self.list_box.addStretch(1)

    def _build_folder_row(self, folder) -> QWidget:
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(8)

        label = folder["name"]
        if folder["special_use"]:
            label += f"  · {_SPECIAL_USE_LABELS.get(folder['special_use'], folder['special_use'])}"
        rl.addWidget(QLabel(label), 1)

        subscribed = QCheckBox("синхронизировать")
        subscribed.setChecked(bool(folder["enabled"]))
        subscribed.toggled.connect(lambda checked, n=folder["name"]: self._on_toggle_subscribed(n, checked))
        rl.addWidget(subscribed)

        rename_btn = button("✎", "ghost")
        rename_btn.setToolTip("Переименовать")
        rename_btn.clicked.connect(lambda _c, n=folder["name"]: self._on_rename(n))
        rl.addWidget(rename_btn)

        delete_btn = button("✕", "ghost")
        delete_btn.setToolTip("Удалить")
        delete_btn.clicked.connect(lambda _c, n=folder["name"]: self._on_delete(n))
        rl.addWidget(delete_btn)
        return row

    def _run(self, coro_fn, on_success=None) -> None:
        self.status.setText("Выполняю…")
        task = fire(coro_fn(), parent=self,
                     on_error=lambda e: self.status.setText(f"Не получилось: {e}"))

        def _apply(t):
            if t.cancelled() or t.exception() is not None:
                return
            self.status.setText("")
            if on_success:
                on_success()
            self._refresh()

        task.add_done_callback(_apply)

    def _on_create(self) -> None:
        name = self.new_name_field.text().strip()
        if not name:
            return
        mailbox_id = self.mailbox_id

        async def _go():
            return await run_blocking(self.ctx.mail_service.create_folder, mailbox_id, name)

        self._run(_go, on_success=self.new_name_field.clear)

    def _on_rename(self, old_name: str) -> None:
        new_name, ok = _ask_text(self, "Переименовать папку", f"Новое имя для «{old_name}»:", old_name)
        if not ok or not new_name.strip() or new_name.strip() == old_name:
            return
        mailbox_id = self.mailbox_id

        async def _go():
            return await run_blocking(
                self.ctx.mail_service.rename_folder, mailbox_id, old_name, new_name.strip())

        self._run(_go)

    def _on_delete(self, name: str) -> None:
        if QMessageBox.question(
            self, "Удалить папку",
            f"Удалить папку «{name}» и всю собранную из неё почту? Отменить нельзя."
        ) != QMessageBox.Yes:
            return
        mailbox_id = self.mailbox_id

        async def _go():
            return await run_blocking(self.ctx.mail_service.delete_folder, mailbox_id, name)

        self._run(_go)

    def _on_toggle_subscribed(self, name: str, checked: bool) -> None:
        mailbox_id = self.mailbox_id

        async def _go():
            return await run_blocking(
                self.ctx.mail_service.set_folder_subscribed, mailbox_id, name, checked)

        self._run(_go)


def _ask_text(parent, title: str, label: str, initial: str) -> tuple[str, bool]:
    from PySide6.QtWidgets import QInputDialog
    return QInputDialog.getText(parent, title, label, text=initial)


class MailSettingsScreen(QWidget):
    def __init__(self, ctx: AppContext, navigate):
        super().__init__()
        self.ctx = ctx
        self.navigate = navigate

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        scroll.setWidget(container)
        wrap = QVBoxLayout(self)
        wrap.setContentsMargins(0, 0, 0, 0)
        wrap.addWidget(scroll)

        outer = QVBoxLayout(container)
        outer.setContentsMargins(40, 28, 40, 32)
        outer.addWidget(h1("Почта"))
        outer.addWidget(muted(
            "Ящики синхронизируются в фоне, независимо от сбора Telegram — оба работают "
            "параллельно и никак не связаны друг с другом."
        ))
        outer.addSpacing(18)

        outer.addWidget(self._build_add_card())
        outer.addSpacing(20)
        outer.addWidget(self._build_list_card())
        outer.addStretch(1)

    # ---- добавление ------------------------------------------------------
    def _build_add_card(self) -> QWidget:
        c = card()
        lay = QVBoxLayout(c)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)
        lay.addWidget(muted("ДОБАВИТЬ ЯЩИК"))
        hint = muted(
            "Для Яндекса, Mail.ru, Gmail и Rambler сервер определяется по адресу "
            "автоматически. Для остальных — впишите его вручную ниже."
        )
        hint.setWordWrap(True)
        lay.addWidget(hint)

        self.address_field = FieldRow("Адрес", placeholder="you@example.com")
        lay.addWidget(self.address_field)
        self.password_field = FieldRow("Пароль", password=True)
        lay.addWidget(self.password_field)

        self.advanced_btn = button("Указать сервер вручную", "ghost")
        self.advanced_btn.setCheckable(True)
        self.advanced_btn.toggled.connect(self._on_toggle_advanced)
        lay.addWidget(self.advanced_btn)

        self.advanced_box = QWidget()
        adv_lay = QHBoxLayout(self.advanced_box)
        adv_lay.setContentsMargins(0, 0, 0, 0)
        adv_lay.setSpacing(10)
        self.imap_host_field = FieldRow("IMAP-сервер", placeholder="imap.example.com")
        adv_lay.addWidget(self.imap_host_field, 1)
        self.imap_port_field = FieldRow("Порт")
        self.imap_port_field.set_text("993")
        adv_lay.addWidget(self.imap_port_field)
        lay.addWidget(self.advanced_box)
        self.advanced_box.setVisible(False)

        self.add_status = muted("")
        self.add_status.setWordWrap(True)
        lay.addWidget(self.add_status)

        btn_row = QHBoxLayout()
        self.test_btn = button("Проверить подключение", "secondary")
        self.test_btn.clicked.connect(self._on_test)
        btn_row.addWidget(self.test_btn)
        self.save_btn = button("Добавить", "primary")
        self.save_btn.clicked.connect(self._on_add)
        btn_row.addWidget(self.save_btn)
        btn_row.addStretch(1)
        lay.addLayout(btn_row)
        return c

    def _on_toggle_advanced(self, checked: bool) -> None:
        self.advanced_box.setVisible(checked)
        self.advanced_btn.setText("Скрыть настройки сервера" if checked else "Указать сервер вручную")

    def _resolved_host(self) -> tuple[str, int, str | None, int] | None:
        """(imap_host, imap_port, smtp_host, smtp_port) — from the manual
        fields if the user opened them, otherwise from the known-provider
        table, otherwise None (nothing to connect to yet)."""
        if self.advanced_box.isVisible() and self.imap_host_field.text().strip():
            host = self.imap_host_field.text().strip()
            try:
                port = int(self.imap_port_field.text().strip() or "993")
            except ValueError:
                port = 993
            return host, port, None, 465
        return autodetect(self.address_field.text().strip())

    def _on_test(self) -> None:
        self._run_check(save_after=False)

    def _on_add(self) -> None:
        self._run_check(save_after=True)

    def _run_check(self, save_after: bool) -> None:
        address = self.address_field.text().strip()
        password = self.password_field.text()
        if not address or "@" not in address:
            self.add_status.setText("Укажите адрес почты.")
            return
        if not password:
            self.add_status.setText("Укажите пароль.")
            return
        resolved = self._resolved_host()
        if resolved is None:
            self.add_status.setText(
                "Сервер не определён автоматически — нажмите «Указать сервер вручную».")
            return
        imap_host, imap_port, smtp_host, smtp_port = resolved

        self.test_btn.setEnabled(False)
        self.save_btn.setEnabled(False)
        self.add_status.setText("Проверяю подключение…")

        async def _check():
            return await run_blocking(
                self.ctx.mail_service.test_connection, imap_host, imap_port, address, password)

        def on_error(e):
            self.test_btn.setEnabled(True)
            self.save_btn.setEnabled(True)
            self.add_status.setText(f"Не удалось подключиться: {e}")

        task = fire(_check(), parent=self, on_error=on_error)

        def _apply(t):
            self.test_btn.setEnabled(True)
            self.save_btn.setEnabled(True)
            if t.cancelled() or t.exception() is not None:
                return
            self.add_status.setText(str(t.result()))
            if save_after:
                self._save_mailbox(imap_host, imap_port, smtp_host, smtp_port, address, password)

        task.add_done_callback(_apply)

    def _save_mailbox(self, imap_host: str, imap_port: int, smtp_host: str | None,
                       smtp_port: int, address: str, password: str) -> None:
        if self.ctx.db.get_mailbox_by_address(address) is not None:
            self.add_status.setText("Такой ящик уже добавлен.")
            return
        password_enc = mail_credentials.encrypt_password(self.ctx.security, password)
        self.ctx.db.add_mailbox(address, imap_host, imap_port, smtp_host, smtp_port, password_enc)
        self.password_field.set_text("")
        self.add_status.setText(f"Ящик {address} добавлен, синхронизация начнётся в фоне.")
        self._refresh_list()

    # ---- список ------------------------------------------------------
    def _build_list_card(self) -> QWidget:
        c = card()
        lay = QVBoxLayout(c)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)
        lay.addWidget(muted("ЯЩИКИ"))
        self.list_box = QVBoxLayout()
        self.list_box.setSpacing(6)
        lay.addLayout(self.list_box)
        self._refresh_list()
        return c

    def on_show(self, **kwargs) -> None:
        self._refresh_list()

    def _refresh_list(self) -> None:
        while self.list_box.count():
            item = self.list_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

        mailboxes = self.ctx.db.list_mailboxes()
        if not mailboxes:
            self.list_box.addWidget(muted("Пока ни одного ящика."))
            return
        for mb in mailboxes:
            self.list_box.addWidget(self._build_mailbox_row(mb))

    def _build_mailbox_row(self, mb) -> QWidget:
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(8)

        info = QVBoxLayout()
        info.setSpacing(1)
        title = mb["address"] + ("" if mb["enabled"] else " · выключен")
        info.addWidget(QLabel(title))
        count = self.ctx.db.count_mail_messages(mb["id"])
        detail = f"писем: {count}"
        detail += f" · синхронизирован {short_dt(mb['last_sync_at'])}" if mb["last_sync_at"] \
            else " · ещё не синхронизирован"
        if mb["last_error"]:
            detail += f" · ошибка: {mb['last_error']}"
        info.addWidget(muted(detail))
        rl.addLayout(info, 1)

        folders_btn = button("Папки", "ghost")
        folders_btn.clicked.connect(lambda _c, m=mb["id"], a=mb["address"]: self._on_manage_folders(m, a))
        rl.addWidget(folders_btn)
        toggle_btn = button("Выключить" if mb["enabled"] else "Включить", "ghost")
        toggle_btn.clicked.connect(lambda _c, m=mb["id"], en=mb["enabled"]: self._on_toggle(m, en))
        rl.addWidget(toggle_btn)
        del_btn = button("Удалить", "ghost")
        del_btn.clicked.connect(lambda _c, m=mb["id"], a=mb["address"]: self._on_delete(m, a))
        rl.addWidget(del_btn)
        return row

    def _on_manage_folders(self, mailbox_id: int, address: str) -> None:
        FolderManagerDialog(self.ctx, mailbox_id, address, parent=self).exec()

    def _on_toggle(self, mailbox_id: int, currently_enabled: int) -> None:
        self.ctx.db.set_mailbox_field(mailbox_id, enabled=0 if currently_enabled else 1)
        self._refresh_list()

    def _on_delete(self, mailbox_id: int, address: str) -> None:
        if QMessageBox.question(
            self, "Удалить ящик",
            f"Удалить «{address}» и всю собранную из него почту? Отменить нельзя."
        ) != QMessageBox.Yes:
            return
        self.ctx.db.delete_mailbox(mailbox_id)
        self._refresh_list()
