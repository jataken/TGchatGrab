"""Small reusable widgets shared across screens."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from . import theme


def label(text: str, cls: str = "") -> QLabel:
    lbl = QLabel(text)
    # QLabel auto-detects rich text (Qt.AutoText default) — a chat title,
    # display name, or message body containing something that merely
    # *looks* like a tag (e.g. a stray "<b>") would otherwise render as
    # real HTML, not literal text. None of this app's own labels need
    # rich text, so it's off unconditionally rather than per call site.
    lbl.setTextFormat(Qt.PlainText)
    if cls:
        lbl.setProperty("class", cls)
    return lbl


def h1(text: str) -> QLabel:
    return label(text, "h1")


def muted(text: str) -> QLabel:
    return label(text, "muted")


def button(text: str, cls: str = "secondary") -> QPushButton:
    btn = QPushButton(text)
    btn.setProperty("class", cls)
    btn.setCursor(Qt.PointingHandCursor)
    return btn


def chip(text: str, checkable: bool = True) -> QPushButton:
    """A small pill toggle — used for the chat filter row on Собранное and
    similar single-choice-of-many filters."""
    btn = button(text, "chip")
    btn.setCheckable(checkable)
    return btn


def card() -> QFrame:
    frame = QFrame()
    frame.setProperty("class", "card")
    return frame


def hline() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setStyleSheet(f"color: {theme.DIVIDER}; background: {theme.DIVIDER}; max-height: 1px;")
    return line


class FieldRow(QWidget):
    """Labeled input, matching the mockup's .field pattern. Password
    fields get a "Показать"/"Скрыть" toggle next to them, so what was
    typed can be checked before submitting."""

    def __init__(self, caption: str, placeholder: str = "", password: bool = False):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lay.addWidget(label(caption, "muted"))
        self.input = QLineEdit()
        self.input.setPlaceholderText(placeholder)
        if password:
            self.input.setEchoMode(QLineEdit.Password)
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            row.addWidget(self.input, 1)
            self.toggle_btn = button("Показать", "secondary")
            self.toggle_btn.setCheckable(True)
            self.toggle_btn.clicked.connect(self._toggle_visibility)
            row.addWidget(self.toggle_btn)
            lay.addLayout(row)
        else:
            self.toggle_btn = None
            lay.addWidget(self.input)

    def _toggle_visibility(self, checked: bool) -> None:
        self.input.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password)
        self.toggle_btn.setText("Скрыть" if checked else "Показать")

    def text(self) -> str:
        return self.input.text()

    def set_text(self, text: str) -> None:
        self.input.setText(text)


class StatusPill(QLabel):
    def __init__(self, status: str = "idle"):
        super().__init__()
        self.set_status(status)

    def set_status(self, status: str) -> None:
        s = theme.STATUS_STYLES.get(status, theme.STATUS_STYLES["idle"])
        self.setText(f"●  {s['label']}")
        self.setStyleSheet(
            f"color: {s['fg']}; background: {s['bg']}; border-radius: 6px; "
            f"padding: 3px 10px; font-size: 11px;"
        )


class ToggleSwitch(QWidget):
    toggled = Signal(bool)

    def __init__(self, checked: bool = False):
        super().__init__()
        self._checked = checked
        self.setFixedSize(34, 19)
        self.setCursor(Qt.PointingHandCursor)

    def is_checked(self) -> bool:
        return self._checked

    def set_checked(self, value: bool, emit: bool = False) -> None:
        if self._checked == value:
            return
        self._checked = value
        self.update()
        if emit:
            self.toggled.emit(value)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.set_checked(not self._checked, emit=True)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(0, 0, -1, -1)
        bg = QColor(theme.ACCENT_700) if self._checked else QColor(255, 255, 255, 22)
        path = QPainterPath()
        path.addRoundedRect(rect, 9, 9)
        p.fillPath(path, bg)
        knob_x = rect.width() - 17 if self._checked else 2
        knob_color = QColor("#f5f4ff") if self._checked else QColor("#75798c")
        p.setBrush(knob_color)
        p.setPen(Qt.NoPen)
        p.drawEllipse(knob_x, 2, 15, 15)


class KeyValue(QWidget):
    """A small stat block: uppercase label above a big value — used for the
    totals row and per-chat stat cards."""

    def __init__(self, key: str, value: str = ""):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        lay.addWidget(label(key, "kicker"))
        self.value_label = QLabel(value)
        self.value_label.setStyleSheet("font-size: 20px;")
        lay.addWidget(self.value_label)

    def set_value(self, value: str) -> None:
        self.value_label.setText(value)
