from __future__ import annotations

import datetime as dt

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QScrollArea, QStackedLayout, QVBoxLayout, QWidget,
)

from ..context import AppContext
from ..util import fire
from ..widgets import FieldRow, button, card, h1, label, muted


class ConnectScreen(QWidget):
    def __init__(self, ctx: AppContext, navigate):
        super().__init__()
        self.ctx = ctx
        self.navigate = navigate

        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 34, 40, 32)
        outer.setSpacing(0)

        kicker = label("ШАГ 1 ИЗ 3", "kicker")
        kicker.setStyleSheet(f"color: #9184d9; font-size: 10px; letter-spacing: 1px;")
        outer.addWidget(kicker)
        outer.addWidget(h1("Вход в Telegram"))
        sub = QLabel(
            "Один вход на всё приложение. Приложение работает от вашего аккаунта "
            "и читает только те чаты, которые вы добавите в список."
        )
        sub.setWordWrap(True)
        sub.setMaximumWidth(460)
        sub.setProperty("class", "muted")
        outer.addSpacing(4)
        outer.addWidget(sub)
        outer.addSpacing(18)

        self.stack = QStackedLayout()
        stack_widget = QWidget()
        stack_widget.setLayout(self.stack)
        outer.addWidget(stack_widget)
        outer.addStretch(1)

        self._build_authed_page()
        self._build_phone_page()
        self._build_code_page()
        self._build_pwd_page()

        self.stack.setCurrentWidget(self.phone_page)

    # ---- pages ---------------------------------------------------------
    def _build_authed_page(self) -> None:
        self.authed_page = QWidget()
        lay = QVBoxLayout(self.authed_page)
        lay.setContentsMargins(0, 0, 0, 0)
        c = card()
        c.setMaximumWidth(520)
        row = QHBoxLayout(c)
        row.setContentsMargins(16, 16, 16, 16)
        avatar = QLabel("СБ")
        avatar.setFixedSize(44, 44)
        avatar.setAlignment(Qt.AlignCenter)
        avatar.setStyleSheet(
            "border-radius: 22px; background: #5d5294; color: #f5f4ff; font-size: 14px;"
        )
        row.addWidget(avatar)
        info = QVBoxLayout()
        self.account_name_label = QLabel("Аккаунт подключён")
        self.account_meta_label = QLabel("")
        self.account_meta_label.setProperty("class", "muted")
        info.addWidget(self.account_name_label)
        info.addWidget(self.account_meta_label)
        row.addLayout(info, 1)
        self.sign_out_btn = button("Сменить аккаунт", "secondary")
        self.sign_out_btn.clicked.connect(self._on_sign_out)
        row.addWidget(self.sign_out_btn)
        lay.addWidget(c)
        note = QLabel(
            "Файл входа хранится только на этом компьютере и никогда не попадает в "
            "выгружаемые файлы. Для сбора лучше использовать отдельный номер, не основной личный."
        )
        note.setWordWrap(True)
        note.setMaximumWidth(460)
        note.setProperty("class", "muted")
        lay.addSpacing(12)
        lay.addWidget(note)
        lay.addStretch(1)
        self.stack.addWidget(self.authed_page)

    def _build_phone_page(self) -> None:
        self.phone_page = QWidget()
        self.phone_page.setMaximumWidth(360)
        lay = QVBoxLayout(self.phone_page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        self.phone_field = FieldRow("Номер телефона", "+7 921 000 00 00")
        lay.addWidget(self.phone_field)
        self.phone_error = QLabel("")
        self.phone_error.setWordWrap(True)
        self.phone_error.setStyleSheet("color: #f0c6cf; font-size: 12px;")
        lay.addWidget(self.phone_error)
        row = QHBoxLayout()
        self.send_code_btn = button("Получить код", "primary")
        self.send_code_btn.clicked.connect(self._on_send_code)
        row.addWidget(self.send_code_btn)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)
        self.stack.addWidget(self.phone_page)

    def _build_code_page(self) -> None:
        self.code_page = QWidget()
        self.code_page.setMaximumWidth(360)
        lay = QVBoxLayout(self.code_page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        self.code_field = FieldRow("Код из сообщения Telegram", "5 цифр")
        lay.addWidget(self.code_field)
        self.code_hint = QLabel("")
        self.code_hint.setWordWrap(True)
        self.code_hint.setProperty("class", "muted")
        lay.addWidget(self.code_hint)
        self.code_error = QLabel("")
        self.code_error.setWordWrap(True)
        self.code_error.setStyleSheet(
            "background: rgba(180,70,90,36); color: #f0c6cf; border-radius: 8px; "
            "padding: 10px 12px; font-size: 12.5px;"
        )
        self.code_error.hide()
        lay.addWidget(self.code_error)
        row = QHBoxLayout()
        self.submit_code_btn = button("Продолжить", "primary")
        self.submit_code_btn.clicked.connect(self._on_submit_code)
        row.addWidget(self.submit_code_btn)
        self.resend_btn = button("Прислать новый код", "secondary")
        self.resend_btn.clicked.connect(self._on_resend_code)
        row.addWidget(self.resend_btn)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)
        self.stack.addWidget(self.code_page)

    def _build_pwd_page(self) -> None:
        self.pwd_page = QWidget()
        self.pwd_page.setMaximumWidth(360)
        lay = QVBoxLayout(self.pwd_page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        self.pwd_field = FieldRow("Пароль двухэтапной проверки", "••••••••", password=True)
        lay.addWidget(self.pwd_field)
        hint = QLabel("Тот пароль, который вы задали в самом Telegram. Приложение его не сохраняет.")
        hint.setWordWrap(True)
        hint.setProperty("class", "muted")
        lay.addWidget(hint)
        self.pwd_error = QLabel("")
        self.pwd_error.setWordWrap(True)
        self.pwd_error.setStyleSheet("color: #f0c6cf; font-size: 12px;")
        lay.addWidget(self.pwd_error)
        row = QHBoxLayout()
        self.submit_pwd_btn = button("Войти", "primary")
        self.submit_pwd_btn.clicked.connect(self._on_submit_pwd)
        row.addWidget(self.submit_pwd_btn)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)
        self.stack.addWidget(self.pwd_page)

    # ---- lifecycle -----------------------------------------------------
    def on_show(self, **kwargs) -> None:
        if not self.ctx.config.is_configured:
            self.phone_error.setText(
                "Сначала укажите ключ приложения (api_id) и секрет (api_hash) на экране «Настройки» — "
                "их выдаёт my.telegram.org."
            )
            self.stack.setCurrentWidget(self.phone_page)
            return
        fire(self._check_auth(), parent=self)

    async def _check_auth(self) -> None:
        authed = await self.ctx.tg.is_authorized()
        if authed:
            me = await self.ctx.tg.me()
            name = " ".join(p for p in [me.first_name, me.last_name] if p) or "Аккаунт Telegram"
            self.account_name_label.setText(name)
            phone = f"+{me.phone}" if getattr(me, "phone", None) else ""
            now = dt.datetime.now().strftime("%d.%m.%Y, %H:%M")
            self.account_meta_label.setText(f"{phone} · вход выполнен {now}" if phone else f"вход выполнен {now}")
            self.stack.setCurrentWidget(self.authed_page)
            await self.ctx.collector.start()
        else:
            self.stack.setCurrentWidget(self.phone_page)

    # ---- actions ---------------------------------------------------------
    def _on_send_code(self) -> None:
        phone = self.phone_field.text().strip()
        if not phone:
            self.phone_error.setText("Введите номер телефона.")
            return
        self.phone_error.setText("")

        def done():
            self.code_hint.setText(f"Код отправлен на {phone}. Он живёт около двух минут — если не успели, запросите новый.")
            self.code_field.set_text("")
            self.stack.setCurrentWidget(self.code_page)

        def on_error(e):
            from ...telegram.errors import humanize_error
            self.phone_error.setText(humanize_error(e))

        fire(self.ctx.tg.send_code(phone), parent=self, on_error=on_error, on_done=done)

    def _on_resend_code(self) -> None:
        self._on_send_code()

    def _on_submit_code(self) -> None:
        code = self.code_field.text().strip()
        if len(code) < 5:
            self.code_error.setText("Похоже, код неполный — проверьте цифры.")
            self.code_error.show()
            return
        self.code_error.hide()

        async def go() -> None:
            signed_in = await self.ctx.tg.submit_code(code)
            if signed_in:
                await self._check_auth()
            else:
                self.stack.setCurrentWidget(self.pwd_page)

        def on_error(e):
            from telethon.errors import PhoneCodeExpiredError
            from ...telegram.errors import humanize_error
            if isinstance(e, PhoneCodeExpiredError):
                self.code_error.setText("Срок действия кода истёк. Нажмите «Прислать новый код» и введите свежий.")
            else:
                self.code_error.setText(humanize_error(e))
            self.code_error.show()

        fire(go(), parent=self, on_error=on_error)

    def _on_submit_pwd(self) -> None:
        pwd = self.pwd_field.text()
        if not pwd:
            self.pwd_error.setText("Введите пароль.")
            return
        self.pwd_error.setText("")

        def on_error(e):
            from ...telegram.errors import humanize_error
            self.pwd_error.setText(humanize_error(e))

        def on_done():
            fire(self._check_auth(), parent=self)

        fire(self.ctx.tg.submit_password(pwd), parent=self, on_error=on_error, on_done=on_done)

    def _on_sign_out(self) -> None:
        def on_done():
            self.stack.setCurrentWidget(self.phone_page)

        fire(self.ctx.tg.sign_out(), parent=self, on_done=on_done)
