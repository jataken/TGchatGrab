"""Small reusable widgets shared across screens."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from . import theme


def plural(n: int, one: str, few: str, many: str) -> str:
    """Russian noun agreement: one form for 1, another for 2–4, a third
    for 5+ and for the whole 11–14 range."""
    a = abs(n) % 100
    b = a % 10
    if 10 < a < 20:
        return many
    if 1 < b < 5:
        return few
    if b == 1:
        return one
    return many


def fmt_int(n: int) -> str:
    """Thousands separated by a space, the Russian convention."""
    return f"{n:,}".replace(",", " ")


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


class ActivityBars(QWidget):
    """Small bar chart of per-day message counts, painted directly. Used on
    Сегодня to make each block's recent volume readable at a glance."""

    def __init__(self, values: list[int] | None = None, height: int = 44):
        super().__init__()
        self._values = values or []
        self.setMinimumHeight(height)
        self.setMinimumWidth(120)

    def set_values(self, values: list[int]) -> None:
        self._values = values
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        values = self._values or [0]
        vmax = max(values) or 1
        w, h = self.width(), self.height()
        n = len(values)
        gap = 3
        bar_w = max(2.0, (w - gap * (n - 1)) / n)
        for i, v in enumerate(values):
            bar_h = max(2, (v / vmax) * (h - 4)) if vmax else 2
            x = i * (bar_w + gap)
            # The last three days read brighter — "what just happened" is
            # the part of the chart a user actually scans for.
            color = QColor(theme.ACCENT_400) if i >= n - 3 else QColor(theme.ACCENT)
            color.setAlpha(235 if i >= n - 3 else 140)
            p.setBrush(color)
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(int(x), int(h - bar_h), int(bar_w), int(bar_h), 2, 2)


class LiveChart(QWidget):
    """A rolling line chart of collection speed (messages per sample tick).
    Values are pushed by the screen from a client-side ring buffer, so it
    costs nothing in the database even while a backfill is running."""

    def __init__(self, capacity: int = 60, height: int = 92):
        super().__init__()
        self._values: list[float] = []
        self._capacity = capacity
        self.setMinimumHeight(height)

    def push(self, value: float) -> None:
        self._values.append(max(0.0, value))
        if len(self._values) > self._capacity:
            del self._values[: len(self._values) - self._capacity]
        self.update()

    def reset(self) -> None:
        self._values.clear()
        self.update()

    def values(self) -> list[float]:
        return list(self._values)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        pad = 4

        grid = QColor(theme.DIVIDER)
        grid.setAlpha(110)
        p.setPen(grid)
        for i in range(1, 4):
            y = pad + (h - 2 * pad) * i / 4
            p.drawLine(0, int(y), w, int(y))

        if len(self._values) < 2:
            p.setPen(QColor(theme.TEXT_FAINT))
            p.drawText(self.rect(), Qt.AlignCenter, "ждём первых сообщений…")
            return

        vmax = max(self._values) or 1.0
        n = len(self._values)
        step = w / max(1, self._capacity - 1)
        # Right-align the trace so a partially filled buffer still grows
        # from the right edge like a live monitor, not from the left.
        x0 = w - (n - 1) * step

        points = []
        for i, v in enumerate(self._values):
            x = x0 + i * step
            y = pad + (1 - v / vmax) * (h - 2 * pad)
            points.append((x, y))

        fill = QPainterPath()
        fill.moveTo(points[0][0], h)
        for x, y in points:
            fill.lineTo(x, y)
        fill.lineTo(points[-1][0], h)
        fill.closeSubpath()
        fill_color = QColor(theme.ACCENT)
        fill_color.setAlpha(46)
        p.fillPath(fill, fill_color)

        line = QPainterPath()
        line.moveTo(*points[0])
        for pt in points[1:]:
            line.lineTo(*pt)
        pen = p.pen()
        pen.setColor(QColor(theme.ACCENT_400))
        pen.setWidth(2)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawPath(line)

        p.setBrush(QColor(theme.ACCENT_400))
        p.setPen(Qt.NoPen)
        p.drawEllipse(int(points[-1][0]) - 3, int(points[-1][1]) - 3, 6, 6)


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
