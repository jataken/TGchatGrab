"""Blocking password prompt shown at startup when master-password
protection is on — runs before the Telegram client or main window exist,
so it's a plain synchronous QDialog, no async loop needed yet."""
from __future__ import annotations

from PySide6.QtWidgets import QDialog, QMessageBox, QVBoxLayout

from .. import APP_TITLE
from ..config import AppConfig
from ..paths import Paths
from ..security import SecurityService, WrongPasswordError
from .widgets import FieldRow, button, h1, muted


class UnlockDialog(QDialog):
    def __init__(self, security: SecurityService, config: AppConfig, paths: Paths):
        super().__init__()
        self.security = security
        self.config = config
        self.paths = paths
        self.setWindowTitle(APP_TITLE)
        self.setMinimumWidth(420)

        lay = QVBoxLayout(self)
        lay.addWidget(h1("Разблокировать ChatGrab"))
        sub = muted(
            "Файл входа в Telegram и ключ приложения защищены мастер-паролем. "
            "Введите его, чтобы продолжить."
        )
        sub.setWordWrap(True)
        lay.addWidget(sub)

        self.pwd_field = FieldRow("Мастер-пароль", "••••••••", password=True)
        lay.addWidget(self.pwd_field)
        self.pwd_field.input.returnPressed.connect(self._on_unlock)

        self.error_label = muted("")
        self.error_label.setStyleSheet("color: #f0c6cf; font-size: 12px;")
        self.error_label.setWordWrap(True)
        lay.addWidget(self.error_label)

        row = QVBoxLayout()
        unlock_btn = button("Разблокировать", "primary")
        unlock_btn.clicked.connect(self._on_unlock)
        row.addWidget(unlock_btn)
        forgot_btn = button("Забыли пароль?", "ghost")
        forgot_btn.clicked.connect(self._on_forgot)
        row.addWidget(forgot_btn)
        lay.addLayout(row)

    def _on_unlock(self) -> None:
        password = self.pwd_field.text()
        if not password:
            self.error_label.setText("Введите пароль.")
            return
        try:
            self.security.unlock(password)
        except WrongPasswordError:
            self.error_label.setText("Неверный пароль. Попробуйте ещё раз.")
            self.pwd_field.set_text("")
            return
        self.accept()

    def _on_forgot(self) -> None:
        confirm = QMessageBox.warning(
            self, "Забыли пароль?",
            "Восстановить мастер-пароль невозможно — он нигде не хранится.\n\n"
            "Можно сбросить защиту: сохранённый вход в Telegram и ключ приложения "
            "будут удалены без возможности восстановления. При следующем запуске "
            "понадобится заново войти в Telegram (номер телефона, код, при "
            "необходимости пароль двухэтапной проверки) и заново указать api_id/"
            "api_hash в Настройках — их можно посмотреть на my.telegram.org, они "
            "не пропадают там. Собранные сообщения и остальные настройки не "
            "пострадают.\n\nСбросить и продолжить?",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        )
        if confirm != QMessageBox.Yes:
            return
        self.security.reset_forgotten()
        self.accept()
