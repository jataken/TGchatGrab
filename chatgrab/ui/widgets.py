"""Small reusable widgets shared across screens.

Д2 («Плотный рефреш», см. DESIGN_PLAN.md) adds the component library from
design-brief.md §3 on top of Д1's tokens/QSS: TabletCheckBox, an animated
ToggleSwitch, Sparkline, MetricsBar, Card, LogPanel, PulseDot, an animated
progress bar, and a rebuilt StatusPill (separate dot + pulsing). Existing
factories/classes already imported by ~40 screen files (button, chip, card,
h1/label/muted/hline, FieldRow, StatusPill, LeadStatusPill, ToggleSwitch,
KeyValue, ActivityBars, LiveChart) keep their exact constructor signatures
and public methods — no screen is touched this session, that's Д3+.
"""
from __future__ import annotations

import math
import re

from PySide6.QtCore import (
    Property, QEasingCurve, QElapsedTimer, QPointF, QPropertyAnimation,
    QRectF, QSize, Qt, QTimer, Signal,
)
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractButton, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget,
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


_RGBA_RE = re.compile(r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)")


def _qcolor(spec: str) -> QColor:
    """Turn a theme.py color constant into a QColor. theme.py deliberately
    takes no PySide6 import itself (Д1) and stores its overlays as
    "rgba(r,g,b,a)" strings (0–255 alpha, not the 0–1 CSS float) — this is
    the one place that parses them back, for the custom-painted widgets
    below that can't reach those colors through QSS at all."""
    m = _RGBA_RE.match(spec)
    if m:
        r, g, b, a = (int(x) for x in m.groups())
        return QColor(r, g, b, a)
    return QColor(spec)


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


def dashed_button(text: str) -> QPushButton:
    """design-brief.md §4.7 — the dashed "＋ {action}" button under a
    list-left panel."""
    return button(text, "dashed")


def icon_button(text: str = "✕", tooltip: str = "") -> QPushButton:
    """24×24 icon-only button (design-brief.md §3.3 «Иконка-кнопка») — the
    QSS `class="icon"` rule already exists (Д1); this just fixes the size
    other icon buttons in the brief share (a table row's ✕, a filter
    chip's remove action) so call sites don't repeat setFixedSize."""
    btn = button(text, "icon")
    btn.setFixedSize(24, 24)
    if tooltip:
        btn.setToolTip(tooltip)
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


def hline_vertical() -> QFrame:
    """A 1px vertical divider — MetricsBar's between-cell separator
    (design-brief.md §3.7)."""
    line = QFrame()
    line.setFrameShape(QFrame.VLine)
    line.setStyleSheet(
        f"color: {theme.DIVIDER_SOFT}; background: {theme.DIVIDER_SOFT}; max-width: 1px;"
    )
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


class PulseDot(QWidget):
    """A small filled circle that can breathe (opacity 1→0.35, scale
    1→0.82, 1.7s, "InOutSine") and optionally show an expanding halo ring
    (radius 0→9px, opacity .45→0, 2.2s, "OutQuad") — design-brief.md §5's
    two dot animations, sharing one continuous clock rather than two
    separate looping QPropertyAnimations. Both phases are derived from
    elapsed time on every repaint instead of animation playback state, so
    toggling `set_pulsing` on/off never leaves a visible jump mid-cycle.

    A ping-pong "1 → 0.35 → 1" curve, not Qt's native `InOutSine` easing
    (which only runs one direction per loop and would snap back at the
    loop boundary), is built here as `sin(t·π)`: 0 at the cycle's start
    and end, 1 at its midpoint — smooth in both directions, no discrete
    QPropertyAnimation needed for an infinite loop.
    """

    _PULSE_MS = 1700
    _HALO_MS = 2200
    _FRAME_MS = 33  # ~30fps — plenty smooth for a 5–8px dot, cheap per instance

    def __init__(self, color: str = theme.ACCENT_400, diameter: int = 8, halo: bool = False):
        super().__init__()
        self._color = _qcolor(color)
        self._diameter = diameter
        self._halo = halo
        self._pulsing = False
        pad = 10 if halo else 1
        self.setFixedSize(diameter + pad * 2, diameter + pad * 2)
        self._clock = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setInterval(self._FRAME_MS)
        self._timer.timeout.connect(self.update)

    def set_color(self, color: str) -> None:
        self._color = _qcolor(color)
        self.update()

    def is_pulsing(self) -> bool:
        return self._pulsing

    def set_pulsing(self, pulsing: bool) -> None:
        if pulsing == self._pulsing:
            return
        self._pulsing = pulsing
        if pulsing:
            self._clock.start()
            if self.isVisible():
                self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._pulsing:
            self._clock.restart()
            self._timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._timer.stop()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        cx, cy = self.width() / 2, self.height() / 2
        elapsed = self._clock.elapsed() if self._pulsing else 0

        if self._halo and self._pulsing:
            t = (elapsed % self._HALO_MS) / self._HALO_MS
            eased = 1 - (1 - t) ** 2  # OutQuad
            radius = self._diameter / 2 + eased * 9
            ring = QColor(self._color)
            ring.setAlphaF(max(0.0, min(1.0, 0.45 * (1 - eased))))
            pen = QPen(ring)
            pen.setWidthF(1.5)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(cx, cy), radius, radius)

        opacity, scale = 1.0, 1.0
        if self._pulsing:
            t = (elapsed % self._PULSE_MS) / self._PULSE_MS
            phase = math.sin(t * math.pi)
            opacity = 1.0 - phase * 0.65
            scale = 1.0 - phase * 0.18
        dot_color = QColor(self._color)
        dot_color.setAlphaF(max(0.0, min(1.0, opacity)))
        p.setBrush(dot_color)
        p.setPen(Qt.NoPen)
        d = self._diameter * scale
        p.drawEllipse(QPointF(cx, cy), d / 2, d / 2)


class StatusPill(QWidget):
    """design-brief.md §3.2. Против прежней версии: точка — отдельный
    кружок (`PulseDot`), не символ `●` внутри текста, и она пульсирует
    для «активных» статусов (loading/listening/running/error). Публичный
    контракт не меняется — `StatusPill(status)` + `.set_status(status)` —
    оба существующих места (`chats.py`, `bots/list_tab.py`, `collect.py`)
    используют только это."""

    _ACTIVE_STATUSES = {"loading", "listening", "running", "error"}

    def __init__(self, status: str = "idle"):
        super().__init__()
        self.setObjectName("statuspill")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(9, 3, 9, 3)
        lay.setSpacing(6)
        self._dot = PulseDot(theme.TEXT_FAINT, diameter=5, halo=False)
        lay.addWidget(self._dot)
        self._text = label("")
        lay.addWidget(self._text)
        self.set_status(status)

    def set_status(self, status: str) -> None:
        s = theme.STATUS_STYLES.get(status, theme.STATUS_STYLES["idle"])
        self._text.setText(s["label"])
        self._text.setStyleSheet(f"color: {s['fg']}; background: transparent; font-size: 11px;")
        self._dot.set_color(s.get("dot", s["fg"]))
        self._dot.set_pulsing(status in self._ACTIVE_STATUSES)
        # Selector scoped by objectName (same technique as QWidget#root in
        # theme.py) rather than a bare property list — an unscoped
        # setStyleSheet() on a widget with children is exactly the leak
        # test_stylesheet_leaks.py guards against.
        self.setStyleSheet(f"QWidget#statuspill {{ background: {s['bg']}; border-radius: 6px; }}")


class LeadStatusPill(QLabel):
    """Same idea as StatusPill above, but keyed on a funnel_stage row's
    own label/colors (С10 — every funnel defines its own stages now,
    there's no single global status→color table any more) rather than
    theme.STATUS_STYLES (chat/bot lifecycle status — idle/running/error).
    Two different domains with two different color sources — not merged
    into one, same as StatusPill itself stays scoped to its own domain.

    Out of scope for Д2 (not in design-brief.md §3's component list, and
    a funnel-stage color is arbitrary user data, not one of the fixed
    STATUS_STYLES entries — there's no dot color to key a pulse off of),
    left exactly as before.

    font_size is a constructor argument, not hardcoded like StatusPill's
    — leads_tab.py's table-row pills and lead_card.py's header pill used
    slightly different sizes (11.5px vs 12px) before this was shared, and
    unifying the color/text logic shouldn't force them to also become
    pixel-identical.
    """

    _FALLBACK = {"label": "—", "color_bg": "rgba(140,140,150,40)", "color_fg": "#cfd0d8"}

    def __init__(self, stage=None, font_size: str = "12px"):
        super().__init__()
        # §8: stage["label"] — название этапа воронки, заведённое
        # пользователем в конструкторе воронок, тем же путём, что и любой
        # другой текст в приложении — без исключений для «своих» данных.
        self.setTextFormat(Qt.PlainText)
        self._font_size = font_size
        self.set_stage(stage)

    def set_stage(self, stage) -> None:
        """stage: a funnel_stage row/dict (exposing label/color_bg/
        color_fg), or None for a lead whose stage couldn't be resolved
        (a stale/foreign status, or funnel_id not set) — renders a
        neutral placeholder rather than guessing at a color."""
        data = stage if stage is not None else self._FALLBACK
        self.setText(f"●  {data['label']}")
        self.setStyleSheet(
            f"color: {data['color_fg']}; background: {data['color_bg']}; border-radius: 6px; "
            f"padding: 3px 10px; font-size: {self._font_size};"
        )


class ToggleSwitch(QWidget):
    """design-brief.md §3.5. Публичный контракт не меняется — только
    добавлена анимация ручки (`QPropertyAnimation`, 180мс, `OutCubic`),
    которую chats.py's единственный вызывающий код не видит: `is_checked()`
    отражает логическое состояние сразу же, анимируется только отрисовка."""

    toggled = Signal(bool)

    def __init__(self, checked: bool = False):
        super().__init__()
        self._checked = checked
        self._pos = 1.0 if checked else 0.0
        self.setFixedSize(34, 19)
        self.setCursor(Qt.PointingHandCursor)
        self._anim = QPropertyAnimation(self, b"knob_pos", self)
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

    def is_checked(self) -> bool:
        return self._checked

    def set_checked(self, value: bool, emit: bool = False) -> None:
        if self._checked == value:
            return
        self._checked = value
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if value else 0.0)
        self._anim.start()
        if emit:
            self.toggled.emit(value)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.set_checked(not self._checked, emit=True)

    def _get_knob_pos(self) -> float:
        return self._pos

    def _set_knob_pos(self, value: float) -> None:
        self._pos = value
        self.update()

    knob_pos = Property(float, _get_knob_pos, _set_knob_pos)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0, 0, -1, -1)
        off_bg = QColor(255, 255, 255, 22)
        on_bg = _qcolor(theme.ACCENT_700)
        t = self._pos
        bg = QColor(
            round(off_bg.red() + (on_bg.red() - off_bg.red()) * t),
            round(off_bg.green() + (on_bg.green() - off_bg.green()) * t),
            round(off_bg.blue() + (on_bg.blue() - off_bg.blue()) * t),
            round(off_bg.alpha() + (on_bg.alpha() - off_bg.alpha()) * t),
        )
        path = QPainterPath()
        path.addRoundedRect(rect, 9, 9)
        p.fillPath(path, bg)
        knob_x = 2 + t * (rect.width() - 17 - 2)
        off_knob, on_knob = QColor("#75798c"), QColor("#f5f4ff")
        knob_color = QColor(
            round(off_knob.red() + (on_knob.red() - off_knob.red()) * t),
            round(off_knob.green() + (on_knob.green() - off_knob.green()) * t),
            round(off_knob.blue() + (on_knob.blue() - off_knob.blue()) * t),
        )
        p.setBrush(knob_color)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QRectF(knob_x, 2, 15, 15))


class TabletCheckBox(QAbstractButton):
    """design-brief.md §3.4 — a full-row pill, not a small square + label:
    padding 6px 9px, radius 8, 13px text, a 15×15 square (radius 5) to
    the left. `QAbstractButton` gives click-toggles-anywhere-in-the-row
    and a native `toggled(bool)` signal for free; only the paint is
    custom."""

    def __init__(self, text: str = ""):
        super().__init__()
        self.setText(text)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(30)
        # sizeHint() below measures with this exact font — paintEvent used
        # to build its own local QFont copy instead of setting it on the
        # widget, so fontMetrics() (used for sizing) and the font actually
        # painted disagreed and text clipped past the computed width.
        font = self.font()
        font.setPixelSize(13)
        self.setFont(font)

    def sizeHint(self) -> QSize:  # noqa: N802
        fm = self.fontMetrics()
        width = fm.horizontalAdvance(self.text()) + 15 + 9 * 2 + 8
        return QSize(width, 30)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        checked = self.isChecked()

        bg = _qcolor(theme.OVERLAY_ACCENT_WEAK) if checked else _qcolor(theme.CHECKBOX_OFF_BG)
        border = _qcolor("rgba(145,132,217,102)") if checked else _qcolor(theme.DIVIDER_SOFT)
        path = QPainterPath()
        path.addRoundedRect(rect, 8, 8)
        p.fillPath(path, bg)
        pen = QPen(border)
        pen.setWidthF(1)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)

        sq = QRectF(rect.left() + 9, rect.center().y() - 7.5, 15, 15)
        sq_path = QPainterPath()
        sq_path.addRoundedRect(sq, 5, 5)
        if checked:
            p.fillPath(sq_path, _qcolor(theme.ACCENT_FILL))
            font = p.font()
            font.setPixelSize(10)
            font.setBold(True)
            p.setFont(font)
            p.setPen(QColor("#F5F4FF"))
            p.drawText(sq, Qt.AlignCenter, "✓")
        else:
            pen2 = QPen(_qcolor(theme.BORDER_HOVER))
            pen2.setWidthF(1)
            p.setPen(pen2)
            p.setBrush(Qt.NoBrush)
            p.drawPath(sq_path)

        text_rect = QRectF(
            rect.left() + 9 + 15 + 8, rect.top(), rect.width() - (9 + 15 + 8 + 9), rect.height()
        )
        font = p.font()
        font.setPixelSize(13)
        font.setBold(False)
        p.setFont(font)
        p.setPen(_qcolor(theme.TEXT if checked else theme.TEXT_MUTED))
        p.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, self.text())


class Card(QFrame):
    """design-brief.md §3.8 — the base card. Reuses the existing
    `class="card"` QSS (background/border/radius, from Д1's `build_qss`)
    for the flat look, then paints the one thing QSS structurally can't
    express: the brief's `box-shadow: inset 0 1px 0 rgba(255,255,255,.04)`
    top highlight (Qt Style Sheets have no `box-shadow` at all — see
    theme.py's Д1 docstring), plus an optional 2px colored status stripe
    down the left edge."""

    def __init__(self, stripe_color: str | None = None):
        super().__init__()
        self.setProperty("class", "card")
        self._stripe_color = stripe_color

    def set_stripe_color(self, color: str | None) -> None:
        self._stripe_color = color
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = theme.RADIUS_CARD
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        pen = QPen(_qcolor(theme.CARD_INNER_GLINT))
        pen.setWidthF(1)
        p.setPen(pen)
        p.drawLine(QPointF(rect.left() + r, rect.top() + 1), QPointF(rect.right() - r, rect.top() + 1))

        if self._stripe_color:
            stripe = QRectF(rect.left(), rect.top() + 2, 2, rect.height() - 4)
            stripe_path = QPainterPath()
            stripe_path.addRoundedRect(stripe, 1, 1)
            p.fillPath(stripe_path, _qcolor(self._stripe_color))


class MetricsBar(Card):
    """design-brief.md §3.7 — a single card-strip of N equal metric cells
    separated by a 1px vertical divider (no divider after the last cell),
    replacing a bare row of `KeyValue` widgets. `KeyValue` itself is left
    alone below — several screens still use its plain layout and aren't
    touched until their own Д4+ session."""

    def __init__(self, cells: list[tuple[str, str, str]] | None = None):
        super().__init__()
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(0)
        self._value_labels: list[QLabel] = []
        self._unit_labels: list[QLabel] = []
        if cells:
            self.set_cells(cells)

    def set_cells(self, cells: list[tuple[str, str, str]]) -> None:
        """cells: list of (kicker, value, unit) — unit may be "" if the
        value has none (e.g. a plain count)."""
        while self._lay.count():
            item = self._lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._value_labels.clear()
        self._unit_labels.clear()
        for i, (kicker, value, unit) in enumerate(cells):
            cell = QWidget()
            cl = QVBoxLayout(cell)
            cl.setContentsMargins(12, 11, 12, 11)
            cl.setSpacing(4)
            cl.addWidget(label(kicker, "kicker"))
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(4)
            value_lbl = label(value, "metric")
            row.addWidget(value_lbl)
            unit_lbl = label(unit, "faint")
            row.addWidget(unit_lbl)
            row.addStretch(1)
            cl.addLayout(row)
            self._lay.addWidget(cell, 1)
            self._value_labels.append(value_lbl)
            self._unit_labels.append(unit_lbl)
            if i < len(cells) - 1:
                self._lay.addWidget(hline_vertical())

    def set_cell(self, index: int, value: str, unit: str | None = None) -> None:
        self._value_labels[index].setText(value)
        if unit is not None:
            self._unit_labels[index].setText(unit)


class AnimatedProgressBar(QWidget):
    """A progress track with an optional running highlight sweep —
    design-brief.md §5 «Бегущий блик на прогрессе», active only while
    `set_active(True)` (an actual operation is running — never idle, per
    the brief's explicit warning against animating on every refresh).
    `progress=None` renders the «неопределённый режим» from §4.1 (history
    load with `approx_total == 0`): just the sweep, no fill."""

    def __init__(self, height: int = 8):
        super().__init__()
        self._height = height
        self._radius = max(2, height // 2)
        self._progress: float | None = 0.0
        self._active = False
        self.setFixedHeight(height)
        self._clock = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self.update)

    def set_progress(self, progress: float | None) -> None:
        self._progress = progress
        self.update()

    def set_active(self, active: bool) -> None:
        if active == self._active:
            return
        self._active = active
        if active:
            self._clock.start()
            if self.isVisible():
                self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._active:
            self._timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._timer.stop()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect())
        track = QPainterPath()
        track.addRoundedRect(rect, self._radius, self._radius)
        p.fillPath(track, QColor(233, 233, 237, 20))

        if self._progress:
            fill_w = rect.width() * max(0.0, min(100.0, self._progress)) / 100.0
            if fill_w > 0:
                fill_rect = QRectF(rect.left(), rect.top(), fill_w, rect.height())
                grad = QLinearGradient(fill_rect.topLeft(), fill_rect.topRight())
                grad.setColorAt(0, _qcolor(theme.ACCENT_600))
                grad.setColorAt(1, _qcolor(theme.ACCENT_400))
                p.setClipPath(track)
                p.fillRect(fill_rect, grad)
                p.setClipping(False)

        if self._active:
            sweep_w = rect.width() * 0.28
            period = 2500
            t = (self._clock.elapsed() % period) / period
            x = -sweep_w + t * (rect.width() + sweep_w)
            sweep_rect = QRectF(x, rect.top(), sweep_w, rect.height())
            grad = QLinearGradient(sweep_rect.topLeft(), sweep_rect.topRight())
            grad.setColorAt(0.0, QColor(255, 255, 255, 0))
            grad.setColorAt(0.5, QColor(255, 255, 255, 60))
            grad.setColorAt(1.0, QColor(255, 255, 255, 0))
            p.setClipPath(track)
            p.fillRect(sweep_rect, grad)
            p.setClipping(False)


class SkeletonRow(QWidget):
    """design-brief.md §7 «Загрузка данных для UI»: a placeholder row for
    content not back from an async call yet (Telegram's own dialog list,
    for instance) — rounded block, `rgba(233,233,237,8)` fill, a running
    shimmer sweeping left→right (§5: 1.6s, continuous). Same sweep-via-
    gradient technique as `AnimatedProgressBar` above, on a plain block
    instead of a progress track. Always animating while visible — there's
    no "idle" state for a loading placeholder the way there is for a
    progress bar, so no `set_active()` toggle to guard against."""

    _PERIOD_MS = 1600

    def __init__(self, height: int = 16):
        super().__init__()
        self.setFixedHeight(height)
        self._clock = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self.update)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._clock.start()
        self._timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._timer.stop()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect())
        block = QPainterPath()
        block.addRoundedRect(rect, 8, 8)
        p.fillPath(block, QColor(233, 233, 237, 8))

        sweep_w = rect.width() * 0.3
        t = (self._clock.elapsed() % self._PERIOD_MS) / self._PERIOD_MS
        x = -sweep_w + t * (rect.width() + sweep_w)
        sweep_rect = QRectF(x, rect.top(), sweep_w, rect.height())
        grad = QLinearGradient(sweep_rect.topLeft(), sweep_rect.topRight())
        grad.setColorAt(0.0, QColor(255, 255, 255, 0))
        grad.setColorAt(0.5, QColor(255, 255, 255, 22))
        grad.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setClipPath(block)
        p.fillRect(sweep_rect, grad)
        p.setClipping(False)


def skeleton_rows(count: int = 3, height: int = 16, spacing: int = 8) -> QWidget:
    """Three (or `count`) stacked `SkeletonRow`s in one ready-to-place
    widget — the shape every loading call site in §7 actually wants."""
    host = QWidget()
    lay = QVBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(spacing)
    for _ in range(count):
        lay.addWidget(SkeletonRow(height))
    return host


class Sparkline(QWidget):
    """design-brief.md §3.6 — 30-bar activity chart (up from
    `ActivityBars`' fewer/variable bars) with a grow-from-bottom entrance
    animation. `ActivityBars` below is untouched and still used as-is by
    Today/the desktop widget — swapping those call sites over is their
    own Д4/Д10 session, not this one.

    Bars animate in on first show or on `set_values()` — never on a bare
    repaint — per the brief's explicit warning that animating on every
    periodic refresh() would just make the chart flicker.
    """

    _BARS = 30
    _GROW_MS = 500
    _STAGGER_MS = 16
    _FRAME_MS = 33

    def __init__(self, values: list[int] | None = None, height: int = 34):
        super().__init__()
        self._values = (values or [])[-self._BARS:]
        self.setMinimumHeight(height)
        self.setMinimumWidth(60)
        self._clock = QElapsedTimer()
        self._animating = False
        self._timer = QTimer(self)
        self._timer.setInterval(self._FRAME_MS)
        self._timer.timeout.connect(self._on_tick)
        self._total_anim_ms = self._GROW_MS + self._STAGGER_MS * max(0, self._BARS - 1)

    def set_values(self, values: list[int]) -> None:
        self._values = (values or [])[-self._BARS:]
        self._start_animation()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._start_animation()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._timer.stop()

    def _start_animation(self) -> None:
        self._animating = True
        self._clock.start()
        self._timer.start()
        self.update()

    def _on_tick(self) -> None:
        if self._clock.elapsed() >= self._total_anim_ms:
            self._timer.stop()
            self._animating = False
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        values = self._values or [0]
        vmax = max(values) or 1
        w, h = self.width(), self.height()
        n = len(values)
        gap = 2
        bar_w = max(1.0, (w - gap * (n - 1)) / n)
        elapsed = self._clock.elapsed() if self._animating else self._total_anim_ms
        for i, v in enumerate(values):
            target_h = max(2.0, (v / vmax) * h) if vmax else 2.0
            local_t = (elapsed - i * self._STAGGER_MS) / self._GROW_MS
            local_t = max(0.0, min(1.0, local_t))
            eased = 1 - (1 - local_t) ** 3  # OutCubic
            scale = 0.15 + 0.85 * eased
            bar_h = target_h * scale
            x = i * (bar_w + gap)
            color = QColor(theme.ACCENT_400) if i >= n - 4 else QColor(theme.ACCENT)
            color.setAlpha(235 if i >= n - 4 else 128)
            p.setBrush(color)
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(QRectF(x, h - bar_h, bar_w, bar_h), 1, 1)


class _LogRow(QWidget):
    """One LogPanel row. Carries its own `content_offset` property so the
    entrance animation can slide its *contents* in from the right without
    fighting the parent QVBoxLayout over the row's own position — animating
    a layout-managed widget's `pos` directly gets reset on the next layout
    pass, so the slide is done as a shrinking left margin instead."""

    def __init__(self, entry: dict, chat_col_width: int):
        super().__init__()
        self._base_left = 14
        self._offset = 0
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(self._base_left, 4, 14, 4)
        self._lay.setSpacing(10)

        tone = entry.get("tone", "")
        color = {"warn": theme.WARN, "ok": theme.GOOD_FG}.get(tone, theme.TEXT_LOG)

        time_lbl = label(str(entry.get("time", "")))
        time_lbl.setStyleSheet(f"color: {color}; font-family: {theme.FONT_MONO}; font-size: 11.5px;")
        self._lay.addWidget(time_lbl)

        chat = entry.get("chat")
        chat_lbl = label(chat if chat else "—")
        chat_lbl.setFixedWidth(chat_col_width)
        chat_color = theme.ACCENT_400 if chat else theme.TEXT_FAINT
        chat_lbl.setStyleSheet(f"color: {chat_color}; font-family: {theme.FONT_MONO}; font-size: 11.5px;")
        self._lay.addWidget(chat_lbl)

        text_lbl = label(str(entry.get("text", "")))
        text_lbl.setStyleSheet(f"color: {color}; font-family: {theme.FONT_MONO}; font-size: 11.5px;")
        text_lbl.setWordWrap(True)
        self._lay.addWidget(text_lbl, 1)

    def _get_offset(self) -> int:
        return self._offset

    def _set_offset(self, value: int) -> None:
        self._offset = value
        self._lay.setContentsMargins(self._base_left + value, 4, max(0, 14 - value), 4)

    content_offset = Property(int, _get_offset, _set_offset)


class LogPanel(QFrame):
    """design-brief.md §3.9 — structured journal panel: header with a
    kicker + entry counter, mono rows with время | чат | текст columns
    colored by tone. Not wired to any screen's real log data this session
    (that's Д4, `collect.py`'s history log) — this is the reusable shell
    with `set_entries`/`add_entry` for that session to call into."""

    def __init__(self, kicker: str = "ЖУРНАЛ", chat_col_width: int = 230):
        super().__init__()
        self.setProperty("class", "logpanel")
        self._chat_col_width = chat_col_width
        self._entries: list[dict] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QWidget()
        header.setStyleSheet("QWidget { border-bottom: 1px solid rgba(51,53,74,.6); }")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(16, 11, 16, 11)
        hl.addWidget(label(kicker, "kicker"))
        hl.addStretch(1)
        self._count_label = label("0 записей")
        self._count_label.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; font-size: 10.5px; font-family: {theme.FONT_MONO};"
        )
        hl.addWidget(self._count_label)
        outer.addWidget(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        self._rows_lay = QVBoxLayout(body)
        self._rows_lay.setContentsMargins(0, 4, 0, 4)
        self._rows_lay.setSpacing(0)
        self._rows_lay.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

    def _set_count_text(self) -> None:
        n = len(self._entries)
        self._count_label.setText(f"{n} " + plural(n, "запись", "записи", "записей"))

    def set_entries(self, entries: list[dict]) -> None:
        """entries: dicts with time/chat/text/tone keys, newest first."""
        self._entries = list(entries)
        while self._rows_lay.count() > 1:
            item = self._rows_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for entry in self._entries:
            self._rows_lay.insertWidget(self._rows_lay.count() - 1, _LogRow(entry, self._chat_col_width))
        self._set_count_text()

    def add_entry(self, entry: dict) -> None:
        """Prepend one new entry with the brief's insert animation (§5:
        slide 10px from the right + fade, 380мс) — only the new row
        animates."""
        self._entries.insert(0, entry)
        row = _LogRow(entry, self._chat_col_width)
        self._rows_lay.insertWidget(0, row)
        self._set_count_text()
        self._animate_row_in(row)

    @staticmethod
    def _animate_row_in(row: _LogRow) -> None:
        effect = QGraphicsOpacityEffect(row)
        row.setGraphicsEffect(effect)
        fade = QPropertyAnimation(effect, b"opacity", row)
        fade.setDuration(380)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        fade.setEasingCurve(QEasingCurve.OutCubic)

        slide = QPropertyAnimation(row, b"content_offset", row)
        slide.setDuration(380)
        slide.setStartValue(10)
        slide.setEndValue(0)
        slide.setEasingCurve(QEasingCurve.OutCubic)

        # Parented to `row` for C++-side lifetime, plus a Python-side
        # reference so a GC pass between here and the animation firing
        # can't collect the animation objects out from under the row.
        row._chatgrab_anims = (fade, slide)
        fade.start()
        slide.start()


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
