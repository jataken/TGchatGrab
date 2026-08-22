"""Почта → ящики: добавить, проверить подключение, включить/выключить,
удалить, управлять папками (П4) и личностями отправителя (П5).
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QDialog, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from ..context import AppContext
from ..format import short_dt
from ..util import fire, run_blocking
from ..widgets import Card, FieldRow, StatusPill, ToggleSwitch, button, h1, label, muted
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

        row_label = folder["name"]
        if folder["special_use"]:
            row_label += f"  · {_SPECIAL_USE_LABELS.get(folder['special_use'], folder['special_use'])}"
        rl.addWidget(QLabel(row_label), 1)

        rl.addWidget(muted("синхронизировать"))
        subscribed = ToggleSwitch(bool(folder["enabled"]))
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


class IdentityManagerDialog(QDialog):
    """Add/edit/delete "From" personas for one mailbox, and pick which
    one is the default a new draft starts with — П5's "несколько
    личностей на ящик" (own name vs. a department alias, each its own
    signature)."""

    def __init__(self, ctx: AppContext, mailbox_id: int, mailbox_address: str, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.mailbox_id = mailbox_id
        self.setWindowTitle(f"Личности — {mailbox_address}")
        self.resize(560, 560)

        outer = QVBoxLayout(self)
        outer.addWidget(muted(
            "Личность — это «от кого» и подпись письма. Первая добавленная "
            "становится личностью по умолчанию."))

        form = Card()
        form_lay = QVBoxLayout(form)
        self.name_field = FieldRow("Имя", placeholder="Иван Иванов")
        form_lay.addWidget(self.name_field)
        self.address_field = FieldRow("Адрес отправителя", placeholder=mailbox_address)
        self.address_field.set_text(mailbox_address)
        form_lay.addWidget(self.address_field)
        form_lay.addWidget(muted("Подпись — можно использовать {имя}, {email}, {дата}"))
        self.signature_edit = QPlainTextEdit()
        self.signature_edit.setMaximumHeight(100)
        form_lay.addWidget(self.signature_edit)
        add_btn = button("Добавить личность", "secondary")
        add_btn.clicked.connect(self._on_add)
        form_lay.addWidget(add_btn)
        outer.addWidget(form)

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
        identities = self.ctx.db.list_mail_identities(self.mailbox_id)
        if not identities:
            self.list_box.addWidget(muted("Пока ни одной личности — используется адрес ящика без подписи."))
        for identity in identities:
            self.list_box.addWidget(self._build_row(identity))
        self.list_box.addStretch(1)

    def _build_row(self, identity) -> QWidget:
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        title = f"{identity['display_name']} <{identity['from_address']}>"
        if identity["is_default"]:
            title += "  · по умолчанию"
        rl.addWidget(QLabel(title), 1)
        if not identity["is_default"]:
            default_btn = button("Сделать основной", "ghost")
            default_btn.clicked.connect(lambda _c, i=identity["id"]: self._on_set_default(i))
            rl.addWidget(default_btn)
        del_btn = button("Удалить", "ghost")
        del_btn.clicked.connect(lambda _c, i=identity["id"]: self._on_delete(i))
        rl.addWidget(del_btn)
        return row

    def _on_add(self) -> None:
        name = self.name_field.text().strip()
        address = self.address_field.text().strip()
        if not name or not address:
            return
        is_first = not self.ctx.db.list_mail_identities(self.mailbox_id)
        self.ctx.db.add_mail_identity(
            self.mailbox_id, name, address,
            signature=self.signature_edit.toPlainText().strip() or None, is_default=is_first)
        self.name_field.set_text("")
        self.signature_edit.setPlainText("")
        self._refresh()

    def _on_set_default(self, identity_id: int) -> None:
        self.ctx.db.set_mail_identity_default(identity_id, self.mailbox_id)
        self._refresh()

    def _on_delete(self, identity_id: int) -> None:
        self.ctx.db.delete_mail_identity(identity_id)
        self._refresh()


_LABEL_COLOR_SWATCHES = [
    "#4f7cff", "#f0a63a", "#28a99e", "#e5484d",
    "#8a8f98", "#a875e8", "#2f9e44", "#d6336c",
]


class LabelManagerDialog(QDialog):
    """Ярлыки на цепочках, П6 — создание/переименование/перекраска/смена
    горячей цифры и удаление. Создание/переименование/перекраска — прямые
    вызовы в `ctx.db` (чисто локально, серверу нечего сообщать: keyword
    построен по id ярлыка, не по имени, см. core/mail_labels.py), но
    удаление снимает ярлык со всех цепочек И пробует снять
    соответствующий IMAP-флаг на сервере — идёт через
    `ctx.mail_service.delete_label()` и `run_blocking`, как и другие
    сетевые операции П4/П5, а не обращается к БД напрямую."""

    def __init__(self, ctx: AppContext, mailbox_id: int, mailbox_address: str, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.mailbox_id = mailbox_id
        self.setWindowTitle(f"Ярлыки — {mailbox_address}")
        self.resize(560, 560)
        self._selected_color = _LABEL_COLOR_SWATCHES[0]

        outer = QVBoxLayout(self)

        form = Card()
        form_lay = QVBoxLayout(form)
        self.name_field = FieldRow("Название", placeholder="Например, «Заказ»")
        form_lay.addWidget(self.name_field)

        form_lay.addWidget(muted("Цвет"))
        swatch_row = QHBoxLayout()
        self.swatch_group = QButtonGroup(self)
        self.swatch_group.setExclusive(True)
        for color in _LABEL_COLOR_SWATCHES:
            swatch_btn = QPushButton()
            swatch_btn.setCheckable(True)
            swatch_btn.setFixedSize(26, 26)
            swatch_btn.setCursor(Qt.PointingHandCursor)
            swatch_btn.setStyleSheet(
                f"QPushButton {{ background-color: {color}; border-radius: 13px; "
                f"border: 2px solid transparent; }}"
                f"QPushButton:checked {{ border: 2px solid white; }}")
            swatch_btn.setChecked(color == self._selected_color)
            swatch_btn.clicked.connect(lambda _c, col=color: self._on_pick_color(col))
            self.swatch_group.addButton(swatch_btn)
            swatch_row.addWidget(swatch_btn)
        swatch_row.addStretch(1)
        form_lay.addLayout(swatch_row)

        form_lay.addWidget(muted("Горячая цифра — вешает ярлык в режиме триажа"))
        self.hotkey_combo = QComboBox()
        form_lay.addWidget(self.hotkey_combo)

        add_btn = button("Добавить ярлык", "secondary")
        add_btn.clicked.connect(self._on_add)
        form_lay.addWidget(add_btn)
        outer.addWidget(form)

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

    def _on_pick_color(self, color: str) -> None:
        self._selected_color = color

    def _refresh_hotkey_options(self, exclude_label_id: int | None = None) -> None:
        taken = {l["hotkey"] for l in self.ctx.db.list_mail_labels(self.mailbox_id)
                 if l["hotkey"] is not None and l["id"] != exclude_label_id}
        self.hotkey_combo.clear()
        self.hotkey_combo.addItem("Без цифры", None)
        for n in range(1, 10):
            if n not in taken:
                self.hotkey_combo.addItem(str(n), n)

    def _refresh(self) -> None:
        self._refresh_hotkey_options()
        while self.list_box.count():
            item = self.list_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        labels = self.ctx.db.list_mail_labels(self.mailbox_id)
        if not labels:
            self.list_box.addWidget(muted("Пока ни одного ярлыка."))
        for lb in labels:
            self.list_box.addWidget(self._build_row(lb))
        self.list_box.addStretch(1)

    def _build_row(self, lb) -> QWidget:
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(8)

        swatch = QLabel()
        swatch.setFixedSize(16, 16)
        swatch.setStyleSheet(f"QLabel {{ background-color: {lb['color']}; border-radius: 8px; }}")
        rl.addWidget(swatch)

        title = lb["name"]
        if lb["hotkey"]:
            title += f"  ·  {lb['hotkey']}"
        rl.addWidget(QLabel(title), 1)

        rename_btn = button("✎", "ghost")
        rename_btn.setToolTip("Переименовать")
        rename_btn.clicked.connect(lambda _c, i=lb["id"], n=lb["name"]: self._on_rename(i, n))
        rl.addWidget(rename_btn)

        del_btn = button("✕", "ghost")
        del_btn.setToolTip("Удалить")
        del_btn.clicked.connect(lambda _c, i=lb["id"], n=lb["name"]: self._on_delete(i, n))
        rl.addWidget(del_btn)
        return row

    def _on_add(self) -> None:
        name = self.name_field.text().strip()
        if not name:
            return
        hotkey = self.hotkey_combo.currentData()
        label_id = self.ctx.mail_service.create_label(self.mailbox_id, name, self._selected_color, hotkey)
        if label_id is None:
            QMessageBox.warning(self, "Цифра занята", "Эта горячая цифра уже занята другим ярлыком.")
            return
        self.name_field.set_text("")
        self._refresh()

    def _on_rename(self, label_id: int, old_name: str) -> None:
        new_name, ok = _ask_text(self, "Переименовать ярлык", "Новое название:", old_name)
        if not ok or not new_name.strip() or new_name.strip() == old_name:
            return
        self.ctx.mail_service.update_label(label_id, name=new_name.strip())
        self._refresh()

    def _on_delete(self, label_id: int, name: str) -> None:
        if QMessageBox.question(
            self, "Удалить ярлык",
            f"Удалить ярлык «{name}» со всех цепочек? Отменить нельзя."
        ) != QMessageBox.Yes:
            return
        self.status.setText("Удаляю…")

        async def _go():
            return await run_blocking(self.ctx.mail_service.delete_label, label_id)

        def on_error(e):
            self.status.setText(f"Не получилось: {e}")

        task = fire(_go(), parent=self, on_error=on_error)

        def _apply(t):
            if t.cancelled() or t.exception() is not None:
                return
            self.status.setText("")
            self._refresh()

        task.add_done_callback(_apply)


def _ask_text(parent, title: str, prompt: str, initial: str) -> tuple[str, bool]:
    from PySide6.QtWidgets import QInputDialog
    return QInputDialog.getText(parent, title, prompt, text=initial)


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
        c = Card()
        lay = QVBoxLayout(c)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)
        lay.addWidget(label("ДОБАВИТЬ ЯЩИК", "kicker"))
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
        adv_outer = QVBoxLayout(self.advanced_box)
        adv_outer.setContentsMargins(0, 0, 0, 0)
        adv_outer.setSpacing(8)
        adv_lay = QHBoxLayout()
        adv_lay.setSpacing(10)
        self.imap_host_field = FieldRow("IMAP-сервер", placeholder="imap.example.com")
        adv_lay.addWidget(self.imap_host_field, 1)
        self.imap_port_field = FieldRow("Порт")
        self.imap_port_field.set_text("993")
        adv_lay.addWidget(self.imap_port_field)
        adv_outer.addLayout(adv_lay)
        # П5: без SMTP-сервера отправка невозможна — для известных
        # провайдеров (KNOWN_PROVIDERS) он определяется вместе с IMAP,
        # но при ручном вводе сервера угадывать SMTP-хост так же, как
        # IMAP-хост, было бы неверно чаще, чем верно.
        smtp_lay = QHBoxLayout()
        smtp_lay.setSpacing(10)
        self.smtp_host_field = FieldRow("SMTP-сервер (для отправки)", placeholder="smtp.example.com")
        smtp_lay.addWidget(self.smtp_host_field, 1)
        self.smtp_port_field = FieldRow("Порт")
        self.smtp_port_field.set_text("465")
        smtp_lay.addWidget(self.smtp_port_field)
        adv_outer.addLayout(smtp_lay)
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
            smtp_host = self.smtp_host_field.text().strip() or None
            try:
                smtp_port = int(self.smtp_port_field.text().strip() or "465")
            except ValueError:
                smtp_port = 465
            return host, port, smtp_host, smtp_port
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
        mailbox_id = self.ctx.db.add_mailbox(address, imap_host, imap_port, smtp_host, smtp_port, password_enc)
        self.ctx.db.seed_default_mail_labels(mailbox_id)
        self.password_field.set_text("")
        self.add_status.setText(f"Ящик {address} добавлен, синхронизация начнётся в фоне.")
        self._refresh_list()

    # ---- список ------------------------------------------------------
    def _build_list_card(self) -> QWidget:
        c = Card()
        lay = QVBoxLayout(c)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)
        lay.addWidget(label("ЯЩИКИ", "kicker"))
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
        info.addWidget(QLabel(mb["address"]))
        count = self.ctx.db.count_mail_messages(mb["id"])
        detail = f"писем: {count}"
        detail += f" · синхронизирован {short_dt(mb['last_sync_at'])}" if mb["last_sync_at"] \
            else " · ещё не синхронизирован"
        if mb["last_error"]:
            detail += f" · ошибка: {mb['last_error']}"
        info.addWidget(muted(detail))
        rl.addLayout(info, 1)

        # «Плашка состояния подключения» (design-brief.md §4.2) — та же
        # StatusPill, что и chats.py/collect.py, на статусах её же словаря
        # (error/listening/queued/off), а не своя новая палитра.
        if mb["last_error"]:
            status = "error"
        elif not mb["enabled"]:
            status = "off"
        elif mb["last_sync_at"]:
            status = "listening"
        else:
            status = "queued"
        rl.addWidget(StatusPill(status))

        folders_btn = button("Папки", "ghost")
        folders_btn.clicked.connect(lambda _c, m=mb["id"], a=mb["address"]: self._on_manage_folders(m, a))
        rl.addWidget(folders_btn)
        identities_btn = button("Личности", "ghost")
        identities_btn.clicked.connect(lambda _c, m=mb["id"], a=mb["address"]: self._on_manage_identities(m, a))
        rl.addWidget(identities_btn)
        labels_btn = button("Ярлыки", "ghost")
        labels_btn.clicked.connect(lambda _c, m=mb["id"], a=mb["address"]: self._on_manage_labels(m, a))
        rl.addWidget(labels_btn)
        toggle_btn = button("Выключить" if mb["enabled"] else "Включить", "ghost")
        toggle_btn.clicked.connect(lambda _c, m=mb["id"], en=mb["enabled"]: self._on_toggle(m, en))
        rl.addWidget(toggle_btn)
        del_btn = button("Удалить", "ghost")
        del_btn.clicked.connect(lambda _c, m=mb["id"], a=mb["address"]: self._on_delete(m, a))
        rl.addWidget(del_btn)
        return row

    def _on_manage_folders(self, mailbox_id: int, address: str) -> None:
        FolderManagerDialog(self.ctx, mailbox_id, address, parent=self).exec()

    def _on_manage_identities(self, mailbox_id: int, address: str) -> None:
        IdentityManagerDialog(self.ctx, mailbox_id, address, parent=self).exec()

    def _on_manage_labels(self, mailbox_id: int, address: str) -> None:
        LabelManagerDialog(self.ctx, mailbox_id, address, parent=self).exec()

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
