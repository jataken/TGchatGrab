from __future__ import annotations

import datetime as dt

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QGraphicsOpacityEffect, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox,
    QStackedLayout, QVBoxLayout, QWidget,
)

from .. import theme
from ..context import AppContext
from ..util import fire
from ..widgets import Card, FieldRow, PulseDot, button, h1, label, muted
from ...telegram.accounts import session_file_for


class _CodeCell(QLineEdit):
    """One digit box of the 5-cell code input. Overrides paste so a whole
    code copied from elsewhere distributes across cells instead of just
    truncating to the first digit that fits this one box."""

    pasted = Signal(str)

    def insertFromMimeData(self, source) -> None:  # noqa: N802
        digits = "".join(ch for ch in source.text() if ch.isdigit())
        if len(digits) > 1:
            self.pasted.emit(digits)
        else:
            super().insertFromMimeData(source)


class _CodeInput(QWidget):
    """design-brief.md §4.2 — five separate 34×36px digit cells instead of
    one text field: auto-advance on digit entry, Backspace steps back into
    the previous cell, arrow keys move focus, and pasting a full code
    distributes it across cells starting from wherever the paste landed."""

    def __init__(self, length: int = 5):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self._cells: list[_CodeCell] = []
        for i in range(length):
            cell = _CodeCell()
            cell.setFixedSize(34, 36)
            cell.setAlignment(Qt.AlignCenter)
            cell.setMaxLength(1)
            cell.setValidator(QIntValidator(0, 9, cell))
            cell.textChanged.connect(lambda text, idx=i: self._on_text_changed(idx, text))
            cell.pasted.connect(lambda digits, idx=i: self._on_pasted(idx, digits))
            cell.installEventFilter(self)
            lay.addWidget(cell)
            self._cells.append(cell)
        lay.addStretch(1)
        self._restyle()

    def text(self) -> str:
        return "".join(c.text() for c in self._cells)

    def set_text(self, value: str) -> None:
        digits = "".join(ch for ch in value if ch.isdigit())
        for i, cell in enumerate(self._cells):
            cell.blockSignals(True)
            cell.setText(digits[i] if i < len(digits) else "")
            cell.blockSignals(False)
        self._restyle()

    def clear(self) -> None:
        self.set_text("")
        self._cells[0].setFocus()

    def setFocus(self) -> None:  # noqa: N802
        self._cells[0].setFocus()

    def _on_text_changed(self, idx: int, text: str) -> None:
        self._restyle()
        if text and idx + 1 < len(self._cells):
            self._cells[idx + 1].setFocus()
            self._cells[idx + 1].selectAll()

    def _on_pasted(self, start_idx: int, digits: str) -> None:
        for offset, ch in enumerate(digits):
            idx = start_idx + offset
            if idx >= len(self._cells):
                break
            self._cells[idx].setText(ch)
        next_idx = min(start_idx + len(digits), len(self._cells) - 1)
        self._cells[next_idx].setFocus()
        self._cells[next_idx].selectAll()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj in self._cells:
            idx = self._cells.index(obj)
            if event.type() == QEvent.KeyPress:
                if event.key() == Qt.Key_Backspace and not obj.text() and idx > 0:
                    self._cells[idx - 1].setFocus()
                    self._cells[idx - 1].selectAll()
                elif event.key() == Qt.Key_Left and idx > 0:
                    self._cells[idx - 1].setFocus()
                elif event.key() == Qt.Key_Right and idx + 1 < len(self._cells):
                    self._cells[idx + 1].setFocus()
            elif event.type() in (QEvent.FocusIn, QEvent.FocusOut):
                self._restyle()
        return super().eventFilter(obj, event)

    def _restyle(self) -> None:
        for cell in self._cells:
            if cell.hasFocus():
                cell.setStyleSheet(
                    f"border: 1px solid {theme.ACCENT}; border-radius: 8px; "
                    f"background: rgba(145,132,217,31); color: {theme.ACCENT_300}; "
                    f"font-family: {theme.FONT_MONO}; font-size: 15px;"
                )
            elif cell.text():
                cell.setStyleSheet(
                    f"border: 1px solid {theme.BORDER_HOVER}; border-radius: 8px; "
                    f"background: {theme.SURFACE_INPUT}; color: {theme.TEXT}; "
                    f"font-family: {theme.FONT_MONO}; font-size: 15px;"
                )
            else:
                cell.setStyleSheet(
                    f"border: 1px solid {theme.DIVIDER}; border-radius: 8px; "
                    f"background: {theme.SURFACE_INPUT}; color: {theme.TEXT}; "
                    f"font-family: {theme.FONT_MONO}; font-size: 15px;"
                )


_STEP_ORDER = ("phone", "code", "pwd")
_STEP_TITLES = {
    "phone": "ШАГ 1 · НОМЕР",
    "code": "ШАГ 2 · КОД",
    "pwd": "ШАГ 3 · ПАРОЛЬ 2FA",
}


class ConnectScreen(QWidget):
    """design-brief.md §4.2. The wizard's three steps sit side by side as
    equal-width cards instead of one QStackedLayout page at a time — the
    active step holds the real, interactive page; the other two show a
    dimmed, non-interactive preview of the same shape (kicker + a blank
    input outline), so the whole three-step journey stays visible while
    only one step is actually live. The authenticated state stays a
    separate page swapped in via `self.stack`, same as before."""

    def __init__(self, ctx: AppContext, navigate):
        super().__init__()
        self.ctx = ctx
        self.navigate = navigate
        # Аккаунт, в который сейчас идёт вход. Для основного — None: тогда
        # мастер работает ровно так же, как когда аккаунт был один.
        self._target_account_id: int | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 34, 40, 32)
        outer.setSpacing(0)

        top_row = QHBoxLayout()
        top_row.setSpacing(10)
        kicker = label("ВХОД В TELEGRAM", "kicker")
        kicker.setStyleSheet(f"color: {theme.ACCENT}; font-size: 10px; letter-spacing: 1px;")
        top_row.addWidget(kicker)
        bars_row = QHBoxLayout()
        bars_row.setSpacing(4)
        self._step_bars: list[QWidget] = []
        for _ in range(3):
            bar = QWidget()
            bar.setFixedSize(16, 3)
            bar.setAttribute(Qt.WA_StyledBackground, True)
            bars_row.addWidget(bar)
            self._step_bars.append(bar)
        top_row.addLayout(bars_row)
        self.step_label = label("", "kicker")
        self.step_label.setStyleSheet(f"color: {theme.TEXT_FAINT}; font-size: 10px;")
        top_row.addWidget(self.step_label)
        top_row.addStretch(1)
        outer.addLayout(top_row)
        outer.addSpacing(10)

        self.title_label = h1("Вход в Telegram")
        outer.addWidget(self.title_label)
        sub = QLabel(
            "Один вход на всё приложение. Приложение работает от вашего аккаунта "
            "и читает только те чаты, которые вы добавите в список."
        )
        sub.setWordWrap(True)
        sub.setMaximumWidth(520)
        sub.setProperty("class", "muted")
        outer.addSpacing(4)
        outer.addWidget(sub)
        outer.addSpacing(18)

        self.stack = QStackedLayout()
        stack_widget = QWidget()
        self._stack_widget = stack_widget
        stack_widget.setLayout(self.stack)
        outer.addWidget(stack_widget)

        self._build_authed_page()
        self._build_wizard_page()
        self.stack.addWidget(self.authed_page)
        self.stack.addWidget(self.wizard_page)

        self.accounts_card = self._build_accounts_card()
        outer.addSpacing(14)
        outer.addWidget(self.accounts_card)
        outer.addStretch(1)

        # QStackedLayout only sizes its container to the *currently shown*
        # page's own size hint, not the tallest of all of them — combined
        # with a stretch inside every page competing against the one
        # already in `outer` below, that made the container shrink out
        # from under whichever page happened to be showing (observed: the
        # password field's bottom few pixels clipped by its own
        # container). Pin the container to the tallest page once, up
        # front, so every page always gets enough room no matter which is
        # current. Measured from each step card's own sizeHint (not from
        # the row they end up sharing) — the row's per-card width shrinks
        # once three cards sit side by side, which would otherwise wrap
        # the hint text taller than this upfront measurement expects.
        pages = (self.authed_page, self.phone_page, self.code_page, self.pwd_page)
        self._tallest_page = max(p.sizeHint().height() for p in pages) + 24
        stack_widget.setMinimumHeight(self._tallest_page)

        self._show_page("phone")

    def _show_page(self, name: str) -> None:
        """`name` is `"authed"` or one of `_STEP_ORDER`. Список аккаунтов
        относится к вошедшему состоянию, а не к шагу мастера — прячется
        вместе с ним, одним движением, чтобы порядок вызовов не решал,
        видно его или нет."""
        self.accounts_card.setVisible(name == "authed")
        if name == "authed":
            self.stack.setCurrentWidget(self.authed_page)
            self.title_label.setText("Аккаунт подключён")
            self._set_step_indicator(3, done=True)
            self._stack_widget.setMinimumHeight(self.authed_page.sizeHint().height() + 8)
            return
        self.stack.setCurrentWidget(self.wizard_page)
        self.title_label.setText("Вход в Telegram")
        self._refresh_steps_row(name)
        self._set_step_indicator(_STEP_ORDER.index(name) + 1)
        self._stack_widget.setMinimumHeight(self._tallest_page)

    def _set_step_indicator(self, n: int, done: bool = False) -> None:
        for i, bar in enumerate(self._step_bars):
            bar.setStyleSheet(
                f"background: {theme.ACCENT if i < n else theme.DIVIDER}; border-radius: 2px;"
            )
        self.step_label.setText(f"ШАГ {n} ИЗ 3 · ГОТОВО" if done else f"ШАГ {n} ИЗ 3")

    # ---- pages ---------------------------------------------------------
    def _build_authed_page(self) -> None:
        self.authed_page = QWidget()
        lay = QVBoxLayout(self.authed_page)
        lay.setContentsMargins(0, 0, 0, 0)
        c = Card(stripe_color=theme.GOOD)
        c.setMaximumWidth(560)
        row = QHBoxLayout(c)
        row.setContentsMargins(16, 16, 16, 16)
        self.avatar_label = QLabel("")
        self.avatar_label.setFixedSize(44, 44)
        self.avatar_label.setAlignment(Qt.AlignCenter)
        self.avatar_label.setStyleSheet(
            "border-radius: 22px; "
            f"background: qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            f"stop:0 {theme.ACCENT_700}, stop:1 {theme.ACCENT_800}); "
            f"color: {theme.ACCENT_100}; font-size: 14px; font-weight: 600;"
        )
        row.addWidget(self.avatar_label)
        info = QVBoxLayout()
        info.setSpacing(3)
        self.account_name_label = QLabel("Аккаунт подключён")
        self.account_name_label.setStyleSheet("font-size: 14px;")
        info.addWidget(self.account_name_label)
        self.account_meta_label = QLabel("")
        self.account_meta_label.setStyleSheet(
            f"font-family: {theme.FONT_MONO}; font-size: 11px; color: {theme.TEXT_MUTED};"
        )
        info.addWidget(self.account_meta_label)
        info.addWidget(self._build_session_pill(), 0, Qt.AlignLeft)
        row.addLayout(info, 1)
        self.sign_out_btn = button("Сменить аккаунт", "secondary")
        self.sign_out_btn.clicked.connect(self._on_sign_out)
        row.addWidget(self.sign_out_btn, 0, Qt.AlignTop)
        lay.addWidget(c)

        warn = QLabel(
            "⚠ Файл входа хранится только на этом компьютере и никогда не попадает в "
            "выгружаемые файлы. Для сбора лучше использовать отдельный номер, не основной личный."
        )
        warn.setWordWrap(True)
        warn.setMaximumWidth(560)
        warn.setStyleSheet(
            "background: rgba(240,198,160,.07); border: 1px solid rgba(240,198,160,.22); "
            f"border-radius: 10px; padding: 8px 10px; color: {theme.WARN_FG}; font-size: 12px;"
        )
        lay.addSpacing(12)
        lay.addWidget(warn)
        lay.addStretch(1)

    def _build_session_pill(self) -> QWidget:
        pill = QWidget()
        pill.setObjectName("sessionpill")
        lay = QHBoxLayout(pill)
        lay.setContentsMargins(9, 3, 9, 3)
        lay.setSpacing(6)
        dot = PulseDot(theme.GOOD, diameter=5, halo=False)
        dot.set_pulsing(True)
        lay.addWidget(dot)
        txt = label("сессия активна")
        txt.setStyleSheet(f"color: {theme.GOOD_FG}; background: transparent; font-size: 11px;")
        lay.addWidget(txt)
        pill.setStyleSheet(f"QWidget#sessionpill {{ background: {theme.GOOD_BG}; border-radius: 6px; }}")
        return pill

    def _build_accounts_card(self) -> QWidget:
        """Живёт рядом со стеком, а не внутри него.

        QStackedLayout выдаёт контейнеру высоту текущей страницы, и
        карточка со списком переменной длины внутри него схлопывалась:
        подсказка налезала на строки. Снаружи она просто занимает
        столько, сколько ей нужно.
        """
        acc_card = Card()
        acc_card.setMaximumWidth(560)
        acc_lay = QVBoxLayout(acc_card)
        acc_lay.setContentsMargins(16, 14, 16, 14)
        acc_lay.setSpacing(8)
        acc_lay.addWidget(label("АККАУНТЫ", "kicker"))
        acc_hint = muted(
            "Ограничения Telegram считаются на аккаунт. Если рассылка идёт с "
            "того же номера, что и сбор, ограничение на отправку отнимает и "
            "доступ к чатам. Второй аккаунт разводит эти риски: чаты можно "
            "оставить на одном номере, а ботов посадить на другой."
        )
        acc_hint.setWordWrap(True)
        acc_lay.addWidget(acc_hint)

        self.accounts_box = QVBoxLayout()
        self.accounts_box.setSpacing(6)
        acc_lay.addLayout(self.accounts_box)

        add_row = QHBoxLayout()
        self.add_account_btn = button("Добавить аккаунт", "secondary")
        self.add_account_btn.clicked.connect(self._on_add_account)
        add_row.addWidget(self.add_account_btn)
        add_row.addStretch(1)
        acc_lay.addLayout(add_row)
        return acc_card

    def _build_wizard_page(self) -> None:
        self.wizard_page = QWidget()
        self._steps_row = QHBoxLayout(self.wizard_page)
        self._steps_row.setContentsMargins(0, 0, 0, 0)
        self._steps_row.setSpacing(14)
        self._build_phone_page()
        self._build_code_page()
        self._build_pwd_page()

    def _refresh_steps_row(self, active: str) -> None:
        while self._steps_row.count():
            item = self._steps_row.takeAt(0)
            w = item.widget()
            if w is None:
                continue
            w.setParent(None)
            if w not in (self.phone_page, self.code_page, self.pwd_page):
                w.deleteLater()
        real = {"phone": self.phone_page, "code": self.code_page, "pwd": self.pwd_page}
        for name in _STEP_ORDER:
            if name == active:
                self._steps_row.addWidget(real[name], 1)
            else:
                self._steps_row.addWidget(self._passive_step_preview(_STEP_TITLES[name]), 1)

    @staticmethod
    def _passive_step_preview(title: str) -> QWidget:
        c = Card()
        lay = QVBoxLayout(c)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)
        lay.addWidget(label(title, "kicker"))
        ph = QWidget()
        ph.setFixedHeight(32)
        ph.setAttribute(Qt.WA_StyledBackground, True)
        ph.setStyleSheet(
            f"background: {theme.SURFACE_INPUT}; border: 1px solid {theme.DIVIDER}; border-radius: 8px;"
        )
        lay.addWidget(ph)
        lay.addStretch(1)
        effect = QGraphicsOpacityEffect(c)
        effect.setOpacity(0.4)
        c.setGraphicsEffect(effect)
        return c

    def _build_phone_page(self) -> None:
        self.phone_page = Card()
        lay = QVBoxLayout(self.phone_page)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(12)
        lay.addWidget(label(_STEP_TITLES["phone"], "kicker"))
        self.phone_field = FieldRow("Номер телефона", "+7 921 000 00 00")
        self.phone_field.input.setStyleSheet(f"font-family: {theme.FONT_MONO}; font-size: 12.5px;")
        lay.addWidget(self.phone_field)
        self.phone_error = QLabel("")
        self.phone_error.setWordWrap(True)
        self.phone_error.setStyleSheet(f"color: {theme.BAD_FG}; font-size: 12px;")
        lay.addWidget(self.phone_error)
        row = QHBoxLayout()
        self.send_code_btn = button("Получить код", "primary")
        self.send_code_btn.clicked.connect(self._on_send_code)
        row.addWidget(self.send_code_btn)
        self.goto_settings_btn = button("Перейти в настройки", "secondary")
        self.goto_settings_btn.clicked.connect(lambda: self.navigate("settings"))
        self.goto_settings_btn.hide()
        row.addWidget(self.goto_settings_btn)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)

    def _build_code_page(self) -> None:
        self.code_page = Card()
        lay = QVBoxLayout(self.code_page)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)
        lay.addWidget(label(_STEP_TITLES["code"], "kicker"))
        self.code_hint = QLabel("")
        self.code_hint.setWordWrap(True)
        self.code_hint.setProperty("class", "muted")
        lay.addWidget(self.code_hint)
        self.code_input = _CodeInput()
        lay.addWidget(self.code_input)
        caption_row = QHBoxLayout()
        caption_row.setSpacing(4)
        caption = label("код живёт ~2 минуты ·")
        caption.setStyleSheet(
            f"font-family: {theme.FONT_MONO}; font-size: 11px; color: {theme.TEXT_FAINT};"
        )
        caption_row.addWidget(caption)
        self.resend_btn = button("прислать новый", "ghost")
        self.resend_btn.clicked.connect(self._on_resend_code)
        caption_row.addWidget(self.resend_btn)
        caption_row.addStretch(1)
        lay.addLayout(caption_row)
        self.code_error = QLabel("")
        self.code_error.setWordWrap(True)
        self.code_error.setStyleSheet(
            f"background: rgba(180,70,90,36); color: {theme.BAD_FG}; border-radius: 8px; "
            "padding: 10px 12px; font-size: 12.5px;"
        )
        self.code_error.hide()
        lay.addWidget(self.code_error)
        row = QHBoxLayout()
        self.submit_code_btn = button("Продолжить", "primary")
        self.submit_code_btn.clicked.connect(self._on_submit_code)
        row.addWidget(self.submit_code_btn)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)

    def _build_pwd_page(self) -> None:
        self.pwd_page = Card()
        lay = QVBoxLayout(self.pwd_page)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(12)
        lay.addWidget(label(_STEP_TITLES["pwd"], "kicker"))
        self.pwd_field = FieldRow("Пароль двухэтапной проверки", "••••••••", password=True)
        self.pwd_field.input.setStyleSheet(f"font-family: {theme.FONT_MONO}; font-size: 12.5px;")
        lay.addWidget(self.pwd_field)
        hint = QLabel("Тот пароль, который вы задали в самом Telegram. Приложение его не сохраняет.")
        hint.setWordWrap(True)
        hint.setProperty("class", "muted")
        lay.addWidget(hint)
        self.pwd_error = QLabel("")
        self.pwd_error.setWordWrap(True)
        self.pwd_error.setStyleSheet(f"color: {theme.BAD_FG}; font-size: 12px;")
        lay.addWidget(self.pwd_error)
        row = QHBoxLayout()
        self.submit_pwd_btn = button("Войти", "primary")
        self.submit_pwd_btn.clicked.connect(self._on_submit_pwd)
        row.addWidget(self.submit_pwd_btn)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)

    # ---- accounts -------------------------------------------------------
    @property
    def _target(self):
        """Сервис, в который сейчас логинимся."""
        if self._target_account_id is None or self.ctx.accounts is None:
            return self.ctx.tg
        return self.ctx.accounts.service_for(self._target_account_id)

    def _refresh_accounts(self) -> None:
        while self.accounts_box.count():
            item = self.accounts_box.takeAt(0)
            w = item.widget()
            if w is not None:
                # setParent(None) до deleteLater: отложенное удаление
                # выполняется только когда цикл событий до него дойдёт, и
                # до тех пор старая строка продолжает рисоваться поверх
                # новой.
                w.setParent(None)
                w.deleteLater()
            elif item.layout() is not None:
                _clear_layout(item.layout())

        accounts = self.ctx.db.list_accounts()
        self.add_account_btn.setEnabled(self.ctx.accounts is not None)
        if not accounts:
            self.accounts_box.addWidget(muted("Пока только этот аккаунт."))
            return
        for acc in accounts:
            usage = self.ctx.db.account_usage(acc["id"])
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(8)
            title = acc["name"] + (" · основной" if acc["is_default"] else "")
            name_col = QVBoxLayout()
            name_col.setSpacing(1)
            name_col.addWidget(QLabel(title))
            detail = f"чатов: {usage['chats']} · ботов: {usage['bots']}"
            if acc["last_error"]:
                detail += f" · {acc['last_error']}"
            name_col.addWidget(muted(detail))
            rl.addLayout(name_col, 1)

            if not acc["is_default"]:
                main_btn = button("Сделать основным", "ghost")
                main_btn.clicked.connect(lambda _c, a=acc["id"]: self._on_make_default(a))
                rl.addWidget(main_btn)
            login_btn = button("Войти", "ghost")
            login_btn.clicked.connect(lambda _c, a=acc["id"], n=acc["name"]: self._on_login_as(a, n))
            rl.addWidget(login_btn)
            del_btn = button("Удалить", "ghost")
            del_btn.clicked.connect(lambda _c, a=acc["id"]: self._on_delete_account(a))
            rl.addWidget(del_btn)
            self.accounts_box.addWidget(row)

    def _on_add_account(self) -> None:
        if self.ctx.accounts is None:
            return
        name, ok = QInputDialog.getText(
            self, "Новый аккаунт",
            "Как назвать аккаунт? Имя нужно только вам — например «для ботов».")
        name = (name or "").strip()
        if not ok or not name:
            return
        taken = {a["session_file"] for a in self.ctx.db.list_accounts()}
        account_id = self.ctx.db.add_account(name, session_file_for(name, taken))
        self._start_login_for(account_id, name)

    def _on_login_as(self, account_id: int, name: str) -> None:
        self._start_login_for(account_id, name)

    def _start_login_for(self, account_id: int, name: str) -> None:
        default = self.ctx.db.default_account()
        # Вход в основной аккаунт — это обычный мастер, без цели.
        self._target_account_id = None if (default and default["id"] == account_id) else account_id
        self.phone_error.setText(f"Вход в аккаунт «{name}».")
        self.phone_field.set_text("")
        self._show_page("phone")

    def _on_make_default(self, account_id: int) -> None:
        self.ctx.db.set_default_account(account_id)
        self._refresh_accounts()
        QMessageBox.information(
            self, "Основной аккаунт изменён",
            "Чаты и боты без явно выбранного аккаунта теперь работают через него. "
            "Изменение вступит в силу после перезапуска приложения.")

    def _on_delete_account(self, account_id: int) -> None:
        usage = self.ctx.db.account_usage(account_id)
        acc = self.ctx.db.get_account(account_id)
        if acc is None:
            return
        if QMessageBox.question(
            self, "Убрать аккаунт",
            f"Убрать «{acc['name']}» из приложения? Чаты ({usage['chats']}) и боты "
            f"({usage['bots']}), закреплённые за ним, вернутся на основной аккаунт. "
            "Сам аккаунт в Telegram и уже собранные сообщения не трогаются; "
            "файл входа останется на диске."
        ) != QMessageBox.Yes:
            return
        self.ctx.db.delete_account(account_id)
        if self.ctx.accounts is not None:
            self.ctx.accounts.forget(account_id)
        self._refresh_accounts()

    # ---- lifecycle -----------------------------------------------------
    def on_show(self, **kwargs) -> None:
        self._refresh_accounts()
        if not self.ctx.config.is_configured:
            self.phone_error.setText(
                "Сначала укажите ключ приложения (api_id) и секрет (api_hash) на экране «Настройки» — "
                "их выдаёт my.telegram.org."
            )
            self.goto_settings_btn.show()
            self._show_page("phone")
            return
        self.goto_settings_btn.hide()
        fire(self._check_auth(), parent=self)

    async def _check_auth(self) -> None:
        authed = await self._target.is_authorized()
        if not authed:
            self._show_page("phone")
            return

        me = await self._target.me()
        phone = f"+{me.phone}" if getattr(me, "phone", None) else ""
        if self._target_account_id is not None:
            # Вошли в дополнительный аккаунт: записываем номер в строку,
            # возвращаемся к списку и оставляем основной как был.
            self.ctx.db.set_account_field(self._target_account_id, phone=phone or None,
                                          last_error=None)
            self._target_account_id = None
            self.phone_error.setText("")
            self._refresh_accounts()
            self._show_page("authed")
            return

        name = " ".join(p for p in [me.first_name, me.last_name] if p) or "Аккаунт Telegram"
        self.account_name_label.setText(name)
        initials = "".join(p[0] for p in [me.first_name, me.last_name] if p)[:2].upper() or "?"
        self.avatar_label.setText(initials)
        now = dt.datetime.now().strftime("%d.%m.%Y, %H:%M")
        self.account_meta_label.setText(f"{phone} · вход выполнен {now}" if phone else f"вход выполнен {now}")
        if self.ctx.accounts is not None:
            self.ctx.accounts.ensure_primary_row()
        self._refresh_accounts()
        self._show_page("authed")
        await self.ctx.collector.start()
        await self.ctx.bot_manager.start()

    # ---- actions ---------------------------------------------------------
    def _on_send_code(self) -> None:
        if not self.ctx.config.is_configured:
            self.phone_error.setText(
                "Сначала укажите ключ приложения (api_id) и секрет (api_hash) на экране "
                "«Настройки» — их выдаёт my.telegram.org."
            )
            self.goto_settings_btn.show()
            return
        phone = self.phone_field.text().strip()
        if not phone:
            self.phone_error.setText("Введите номер телефона.")
            return
        self.phone_error.setText("")

        def done():
            self.code_hint.setText(f"Код отправлен на {phone}.")
            self.code_input.clear()
            self._show_page("code")

        def on_error(e):
            from ...telegram.errors import humanize_error
            self.phone_error.setText(humanize_error(e))

        fire(self._target.send_code(phone), parent=self, on_error=on_error, on_done=done)

    def _on_resend_code(self) -> None:
        self._on_send_code()

    def _on_submit_code(self) -> None:
        code = self.code_input.text()
        if len(code) < 5:
            self.code_error.setText("Похоже, код неполный — проверьте цифры.")
            self.code_error.show()
            return
        self.code_error.hide()

        async def go() -> None:
            signed_in = await self._target.submit_code(code)
            if signed_in:
                await self._check_auth()
            else:
                self._show_page("pwd")

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

        fire(self._target.submit_password(pwd), parent=self, on_error=on_error, on_done=on_done)

    def _on_sign_out(self) -> None:
        def on_done():
            self._show_page("phone")

        fire(self.ctx.tg.sign_out(), parent=self, on_done=on_done)


def _clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()
        elif item.layout() is not None:
            _clear_layout(item.layout())
