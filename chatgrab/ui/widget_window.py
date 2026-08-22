"""П8: a small always-on-top desktop widget — mail/collection/bots at a
glance without opening the main window ("что нового", per PLAN.md).

Frameless (Qt.FramelessWindowHint) so it needs its own mousePressEvent/
mouseMoveEvent drag handling — there's no title bar to drag by — and
Qt.Tool so it never shows up in the taskbar, alongside
Qt.WindowStaysOnTopHint. All three flags together, no packaged widget
panel, no installer: this stays exactly as portable as the rest of the
app (invariant 1), unlike Windows 11's native widget panel, which needs
an MSIX package.

Reads the database on its own short timer and nothing else — no IMAP, no
Telegram, no network of any kind. Every number shown here was already
put in the database by MailService/Collector/BotManager on their own
schedules; this window is a read-only, occasionally-writing (labels)
view over it.
"""
from __future__ import annotations

import datetime as dt

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget,
)

from . import theme
from .context import AppContext
from .util import fire, run_blocking
from .widgets import Card, LiveChart, MetricsBar, Sparkline, StatusPill, button, chip, label, muted
from ..core import lead as lead_domain

_SETTINGS_KEY = "desktop_widget"
_REFRESH_MS = 5000
_SPEED_SAMPLE_MS = 4000
_MAIL_ROWS_LIMIT = 8
_MAIL_ONLY_WIDTH = 220

# No per-mailbox color field exists on the mailbox table (nothing in П1-П7
# needed one) — assigned by position in list_mailboxes(), stable as long
# as the mailbox list itself doesn't change, same trade-off the label
# swatch palette in mail_settings.py makes for the same reason.
_MAILBOX_COLORS = [
    "#4f7cff", "#f0a63a", "#28a99e", "#e5484d", "#a875e8", "#2f9e44", "#d6336c", "#8a8f98",
]

_DEFAULT_STATE = {
    "x": None, "y": None, "width": 340, "height": 520, "opacity": 95,
    "sections": {"mail": True, "collect": True, "bots": True},
    "mail_only": False,
}


def _load_state(db) -> dict:
    raw = db.get_setting(_SETTINGS_KEY, {})
    state = dict(_DEFAULT_STATE)
    if isinstance(raw, dict):
        state.update({k: v for k, v in raw.items() if k in _DEFAULT_STATE})
        sections = dict(_DEFAULT_STATE["sections"])
        if isinstance(raw.get("sections"), dict):
            sections.update({k: bool(v) for k, v in raw["sections"].items() if k in sections})
        state["sections"] = sections
    return state


class _ClickableFrame(Card):
    """A row that opens something on a plain click. A child button (the
    per-row "apply a label" toggle) still gets its own click first —
    Qt routes a press to the topmost widget under the cursor, so a click
    squarely on a QPushButton never reaches this frame's mousePressEvent
    at all; nothing extra to swallow here.

    `Card`, not a bare `QFrame` with `class="card"` — the triage-score
    stripe used to be a one-off `setStyleSheet(f"border-left: 3px solid
    ...")`; `Card.set_stripe_color()` (Д2) is the same 2px-stripe
    component every other card in the app already uses for exactly this."""

    def __init__(self, on_click, stripe_color: str | None = None):
        super().__init__(stripe_color)
        self._on_click = on_click
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self._on_click:
            self._on_click()
        super().mousePressEvent(event)


class WidgetWindow(QWidget):
    def __init__(self, ctx: AppContext, on_open_thread=None, parent=None):
        super().__init__(parent, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.ctx = ctx
        self.on_open_thread = on_open_thread
        self._drag_offset = None
        self._state = _load_state(ctx.db)
        self._last_total_messages: int | None = None

        self.setWindowOpacity(self._state["opacity"] / 100)
        self.resize(self._state["width"], self._state["height"])
        self._restore_position()

        self._build_ui()
        self._apply_sections()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(_REFRESH_MS)
        self._speed_timer = QTimer(self)
        self._speed_timer.timeout.connect(self._sample_speed)
        self._speed_timer.start(_SPEED_SAMPLE_MS)

        self.refresh()

    # ---- геометрия: позиция/размер переживают перезапуск -----------------
    def _restore_position(self) -> None:
        x, y = self._state.get("x"), self._state.get("y")
        if x is not None and y is not None:
            self.move(int(x), int(y))
            return
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.move(geo.right() - self.width() - 24, geo.bottom() - self.height() - 24)

    def _save_geometry(self) -> None:
        self._state["x"] = self.x()
        self._state["y"] = self.y()
        if not self._state["mail_only"]:
            self._state["width"] = self.width()
        self._state["height"] = self.height()
        self.ctx.db.set_setting(_SETTINGS_KEY, self._state)

    # ---- перетаскивание за любую точку — рамки-то нет ---------------------
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is not None:
            self._drag_offset = None
            self._save_geometry()

    def closeEvent(self, event) -> None:  # noqa: N802
        # «Закрытие прячет в трей» — never a real close, only the app
        # quitting (which doesn't go through here) ends this window.
        event.ignore()
        self.hide()

    def show_and_raise(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    # ---- построение -------------------------------------------------------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 10)
        outer.setSpacing(8)

        header = QHBoxLayout()
        self.title_label = label("ChatGrab", "h1")
        header.addWidget(self.title_label)
        header.addStretch(1)
        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(40, 100)
        self.opacity_slider.setFixedWidth(64)
        self.opacity_slider.setToolTip("Прозрачность")
        self.opacity_slider.setValue(self._state["opacity"])
        self.opacity_slider.valueChanged.connect(self._on_opacity_changed)
        header.addWidget(self.opacity_slider)
        self.close_btn = button("✕", "ghost")
        self.close_btn.setFixedWidth(26)
        self.close_btn.clicked.connect(self.hide)
        header.addWidget(self.close_btn)
        outer.addLayout(header)

        pills = QHBoxLayout()
        self._section_pills = {}
        for key, title in (("mail", "Почта"), ("collect", "Сбор"), ("bots", "Боты")):
            pill = chip(title)
            pill.setChecked(self._state["sections"][key])
            pill.clicked.connect(lambda _c, k=key: self._on_toggle_section(k))
            pills.addWidget(pill)
            self._section_pills[key] = pill
        pills.addStretch(1)
        self.mail_only_btn = chip("узко")
        self.mail_only_btn.setToolTip("Только почта, узкой полосой сбоку")
        self.mail_only_btn.setChecked(self._state["mail_only"])
        self.mail_only_btn.clicked.connect(self._on_toggle_mail_only)
        pills.addWidget(self.mail_only_btn)
        self._pills_layout = pills
        outer.addLayout(pills)

        # ---- почта ----
        self.mail_section = QWidget()
        mail_lay = QVBoxLayout(self.mail_section)
        mail_lay.setContentsMargins(0, 0, 0, 0)
        mail_lay.setSpacing(4)
        self.mail_rows_layout = QVBoxLayout()
        self.mail_rows_layout.setSpacing(4)
        mail_lay.addLayout(self.mail_rows_layout)
        self.mail_empty_label = muted("Новых писем нет.")
        mail_lay.addWidget(self.mail_empty_label)
        outer.addWidget(self.mail_section)

        # ---- сбор ----
        self.collect_section = QWidget()
        collect_lay = QVBoxLayout(self.collect_section)
        collect_lay.setContentsMargins(0, 0, 0, 0)
        collect_lay.setSpacing(4)
        collect_head = QHBoxLayout()
        collect_head.addWidget(label("СБОР", "kicker"))
        collect_head.addStretch(1)
        self.collect_status_pill = StatusPill("idle")
        collect_head.addWidget(self.collect_status_pill)
        collect_lay.addLayout(collect_head)
        self.collect_chart = LiveChart(height=48)
        collect_lay.addWidget(self.collect_chart)
        self.collect_metrics = MetricsBar()
        collect_lay.addWidget(self.collect_metrics)
        outer.addWidget(self.collect_section)

        # ---- боты ----
        self.bots_section = QWidget()
        bots_lay = QVBoxLayout(self.bots_section)
        bots_lay.setContentsMargins(0, 0, 0, 0)
        bots_lay.setSpacing(4)
        bots_head = QHBoxLayout()
        bots_head.addWidget(label("БОТЫ", "kicker"))
        bots_head.addStretch(1)
        self.bots_status_pill = StatusPill("idle")
        bots_head.addWidget(self.bots_status_pill)
        bots_lay.addLayout(bots_head)
        # Sparkline (Д2, §3.6), не ActivityBars — тот самый отложенный с Д2
        # перевод виджета на неё; `_last_bot_series` ниже следит, чтобы
        # set_values() не дёргался на каждый тик таймера без реальных
        # изменений — Sparkline анимирует вход при каждом set_values(),
        # а брифовое предупреждение против анимации «на каждый рефреш»
        # относится именно к этому.
        self.bots_chart = Sparkline(height=36)
        self._last_bot_series: list[int] | None = None
        bots_lay.addWidget(self.bots_chart)
        self.bots_metrics = MetricsBar()
        bots_lay.addWidget(self.bots_metrics)
        outer.addWidget(self.bots_section)

        outer.addStretch(1)

    # ---- секции: сворачивание/отключение и «только почта» -----------------
    def _on_toggle_section(self, key: str) -> None:
        self._state["sections"][key] = self._section_pills[key].isChecked()
        self._apply_sections()
        self.ctx.db.set_setting(_SETTINGS_KEY, self._state)

    def _on_toggle_mail_only(self) -> None:
        self._state["mail_only"] = self.mail_only_btn.isChecked()
        self._apply_sections()
        self.ctx.db.set_setting(_SETTINGS_KEY, self._state)

    def _apply_sections(self) -> None:
        mail_only = self._state["mail_only"]
        self.mail_section.setVisible(self._state["sections"]["mail"])
        self.collect_section.setVisible(self._state["sections"]["collect"] and not mail_only)
        self.bots_section.setVisible(self._state["sections"]["bots"] and not mail_only)
        # The title, opacity slider and the three section pills don't fit
        # in a genuinely narrow strip — trimmed down to just the controls
        # that still mean something (toggle back out of "узко", close),
        # otherwise the layout's own minimum width refuses to shrink the
        # window past what that row needs, no matter what resize() asks for.
        self.title_label.setVisible(not mail_only)
        self.opacity_slider.setVisible(not mail_only)
        for pill in self._section_pills.values():
            pill.setVisible(not mail_only)
        # A bare resize() after a batch of setVisible() calls can lose to
        # the layout's own still-pending, stale-cached size hint — asking
        # it to recompute *now* is what makes the resize below actually
        # stick instead of silently snapping back.
        self.layout().invalidate()
        self.layout().activate()
        self.resize(_MAIL_ONLY_WIDTH if mail_only else self._state["width"], self.height())

    def _on_opacity_changed(self, value: int) -> None:
        self._state["opacity"] = value
        self.setWindowOpacity(value / 100)
        self.ctx.db.set_setting(_SETTINGS_KEY, self._state)

    # ---- обновление ---------------------------------------------------------
    def refresh(self) -> None:
        if self._state["sections"]["mail"]:
            self._refresh_mail()
        if self._state["sections"]["collect"] and not self._state["mail_only"]:
            self._refresh_collect_status()
        if self._state["sections"]["bots"] and not self._state["mail_only"]:
            self._refresh_bots()

    def _refresh_mail(self) -> None:
        while self.mail_rows_layout.count():
            item = self.mail_rows_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        mailboxes = self.ctx.db.list_mailboxes(enabled_only=True)
        colors = {mb["id"]: _MAILBOX_COLORS[i % len(_MAILBOX_COLORS)] for i, mb in enumerate(mailboxes)}
        threshold = self.ctx.mail_service.get_triage_settings()["threshold"]
        messages = self.ctx.db.list_recent_mail_messages(limit=_MAIL_ROWS_LIMIT)
        self.mail_empty_label.setVisible(not messages)
        for message in messages:
            row = self._build_mail_row(message, colors.get(message["mailbox_id"], _MAILBOX_COLORS[0]), threshold)
            self.mail_rows_layout.addWidget(row)

    def _build_mail_row(self, message, color: str, threshold: int) -> QWidget:
        thread_id, mailbox_id = message["thread_id"], message["mailbox_id"]

        def _open() -> None:
            if self.on_open_thread and thread_id is not None:
                self.on_open_thread(mailbox_id, thread_id)

        score = message["triage_score"]
        important = score is not None and score >= threshold
        frame = _ClickableFrame(_open, stripe_color=theme.ACCENT if important else None)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(2)

        top = QHBoxLayout()
        dot = QLabel()
        dot.setFixedSize(8, 8)
        dot.setStyleSheet(f"QLabel {{ background: {color}; border-radius: 4px; }}")
        top.addWidget(dot)
        unread_mark = "●" if not message["is_read"] else "○"
        who = message["sender_name"] or message["sender_address"] or "—"
        who_label = label(f"{unread_mark} {who}")
        # Wrapped, not left to its natural single-line width — otherwise
        # a longish sender name/address is exactly what stops «узкая
        # полоса» from actually narrowing (the row's own minimum width
        # would follow the longest name shown, regardless of what the
        # window itself is asked to resize to).
        who_label.setWordWrap(True)
        top.addWidget(who_label, 1)
        # П6: «ярлык в один клик прямо из виджета» — этот значок
        # разворачивает ряд плашек с ярлыками того же ящика, клик по
        # плашке применяет ярлык к цепочке и сворачивает обратно.
        tag_btn = button("🏷", "ghost")
        tag_btn.setFixedSize(24, 22)
        top.addWidget(tag_btn)
        lay.addLayout(top)

        subject = message["subject"] or "(без темы)"
        clip = " 📎" if message["has_attachments"] else ""
        subject_label = muted(f"{subject}{clip}")
        subject_label.setWordWrap(True)
        lay.addWidget(subject_label)

        chip_container = QWidget()
        chip_lay = QHBoxLayout(chip_container)
        chip_lay.setContentsMargins(0, 4, 0, 0)
        chip_lay.setSpacing(4)
        for lb in self.ctx.db.list_mail_labels(mailbox_id):
            label_btn = QPushButton(lb["name"])
            label_btn.setCursor(Qt.PointingHandCursor)
            label_btn.setStyleSheet(
                f"QPushButton {{ background-color: {lb['color']}; color: white; border: none; "
                f"border-radius: 8px; padding: 1px 8px; font-size: 11px; }}")
            label_btn.clicked.connect(
                lambda _c, lid=lb["id"]: self._apply_label(thread_id, lid, chip_container))
            chip_lay.addWidget(label_btn)
        chip_lay.addStretch(1)
        chip_container.setVisible(False)
        lay.addWidget(chip_container)
        tag_btn.clicked.connect(lambda: chip_container.setVisible(not chip_container.isVisible()))

        return frame

    def _apply_label(self, thread_id: int, label_id: int, chip_container: QWidget) -> None:
        async def _run():
            return await run_blocking(self.ctx.mail_service.set_thread_label, thread_id, label_id, True)

        def on_done() -> None:
            chip_container.setVisible(False)
            self.refresh()

        fire(_run(), parent=self, on_error=lambda e: None, on_done=on_done)

    def _sample_speed(self) -> None:
        total = self.ctx.db.message_count()
        if self._last_total_messages is None:
            self._last_total_messages = total
            return
        delta = max(0, total - self._last_total_messages)
        self._last_total_messages = total
        self.collect_chart.push(delta / (_SPEED_SAMPLE_MS / 1000))

    def _refresh_collect_status(self) -> None:
        chats = self.ctx.db.list_chats()
        active = [c for c in chats if c["enabled"]]
        errors = [c for c in chats if c["last_error"]]
        loading = any(c["status"] == "loading" for c in chats)
        if errors:
            status = "error"
        elif loading:
            status = "loading"
        elif active:
            status = "listening"
        else:
            status = "off"
        self.collect_status_pill.set_status(status)
        cells = [("ЧАТЫ", f"{len(active)}/{len(chats)}", "")]
        if errors:
            cells.append(("ОШИБОК", str(len(errors)), ""))
        self.collect_metrics.set_cells(cells)

    def _refresh_bots(self) -> None:
        db = self.ctx.db
        bots = db.list_bots()
        leads = db.list_leads()
        running = [b for b in bots if b["status"] == "running"]
        errors = [b for b in bots if b["status"] == "error"]
        # С10: "новая" is derived per-lead against that lead's own
        # funnel, not a hardcoded status — see today.py's identical
        # comment for why (leads can span more than one funnel).
        stages_cache: dict[int | None, list] = {}

        def _stages_for(funnel_id):
            if funnel_id not in stages_cache:
                stages_cache[funnel_id] = db.list_funnel_stages(funnel_id) if funnel_id else []
            return stages_cache[funnel_id]

        new_leads = [lead for lead in leads
                     if lead_domain.bucket_for_stage(_stages_for(lead["funnel_id"]), lead["status"]) == "new"]
        series = self._weekly_lead_series(leads)
        if series != self._last_bot_series:
            self._last_bot_series = series
            self.bots_chart.set_values(series)
        if errors:
            status = "error"
        elif running:
            status = "running"
        else:
            status = "stopped"
        self.bots_status_pill.set_status(status)
        cells = [("БОТЫ", f"{len(running)}/{len(bots)}", "")]
        if new_leads:
            cells.append(("НОВЫХ", str(len(new_leads)), ""))
        self.bots_metrics.set_cells(cells)

    def _weekly_lead_series(self, leads) -> list[int]:
        today = dt.date.today()
        buckets = {today - dt.timedelta(days=i): 0 for i in range(7)}
        for lead in leads:
            try:
                created = dt.datetime.fromisoformat(str(lead["created_at"])).date()
            except (ValueError, TypeError):
                continue
            if created in buckets:
                buckets[created] += 1
        return [buckets[today - dt.timedelta(days=i)] for i in range(6, -1, -1)]
