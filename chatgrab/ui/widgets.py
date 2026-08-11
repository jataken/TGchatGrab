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
    """Labeled input, matching the mockup's .field pattern."""

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
        lay.addWidget(self.input)

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


class ActivityBars(QWidget):
    """Small 16-bar activity chart, painted directly (matches the mockup's
    per-tile bar chart on the Обзор screen)."""

    def __init__(self, values: list[int] | None = None):
        super().__init__()
        self._values = values or []
        self.setMinimumHeight(42)
        self.setMinimumWidth(140)

    def set_values(self, values: list[int]) -> None:
        self._values = values
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        values = self._values or [0]
        vmax = max(values) or 1
        w = self.width()
        h = self.height()
        n = len(values)
        gap = 3
        bar_w = max(2.0, (w - gap * (n - 1)) / n)
        for i, v in enumerate(values):
            ratio = v / vmax if vmax else 0
            bar_h = max(3, ratio * (h - 4))
            x = i * (bar_w + gap)
            y = h - bar_h
            color = QColor(theme.ACCENT_400) if i >= n - 3 else QColor(theme.ACCENT)
            color.setAlpha(230 if i >= n - 3 else 150)
            p.setBrush(color)
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(int(x), int(y), int(bar_w), int(bar_h), 2, 2)


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
