from __future__ import annotations

from PySide6.QtCore import QDate, QPoint, QRect, QSize, QTimer, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QDateEdit, QHBoxLayout, QLabel, QLayout, QListWidget,
    QListWidgetItem, QMessageBox, QRadioButton, QScrollArea, QVBoxLayout, QWidget,
)

from .. import theme
from ..context import AppContext
from ..format import short_dt
from ..util import fire
from ..widgets import (
    AnimatedProgressBar, Card, LiveChart, LogPanel, MetricsBar,
    StatusPill, button, chip, h1, label, muted, plural,
)


class _FlowLayout(QLayout):
    """Wraps children onto additional rows instead of clipping or
    scrolling — Qt has no built-in flow layout. Used for the chat-chip row
    (design-brief.md §4.4: "с переносом на вторую строку"), the standard
    recipe from Qt's own flow-layout example, adapted for PySide6."""

    def __init__(self, parent=None, margin: int = 0, spacing: int = 6):
        super().__init__(parent)
        self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)
        self._items: list = []

    def addItem(self, item) -> None:  # noqa: N802 (Qt override)
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientations:  # noqa: N802
        return Qt.Orientations(Qt.Orientation(0))

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        x, y = rect.x(), rect.y()
        line_height = 0
        spacing = self.spacing()
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + spacing
            if next_x - spacing > rect.right() and line_height > 0:
                x = rect.x()
                y += line_height + spacing
                next_x = x + hint.width() + spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y()


def _dot_icon(color: str, diameter: int = 5) -> QIcon:
    """A tiny solid-color circle as a QIcon — QPushButton lays an icon out
    left of its text natively, which is the simplest way to give the chat
    chip row's chips a status dot (design-brief.md §4.4) without turning
    the "chip" QSS-styled QPushButton into a composite widget."""
    pm = QPixmap(diameter, diameter)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.NoPen)
    p.drawEllipse(0, 0, diameter, diameter)
    p.end()
    return QIcon(pm)


# «паузы»/«ошибки» — оба тона в col.log_entries приходят как tone="warn"
# (design-brief.md §4.4), различаются только текстом. Эвристика простая и
# текстовая, не заводит новый tone в самом коллекторе — паузы FloodWait
# сообщают о себе словом «подожда(ть)»/«пауза», всё остальное warn — ошибка.
def _log_bucket(entry: dict) -> str | None:
    if entry.get("tone") != "warn":
        return None
    text = entry.get("text", "")
    return "pauses" if ("подожда" in text or "пауза" in text) else "errors"


class CollectScreen(QWidget):
    """Two columns: the selected chat's card + the queue of chats waiting
    their turn on the left, the shared event log on the right — chats
    load one at a time because Telegram's rate limit is per-account, not
    per-chat, so seeing the queue next to the active job matters."""

    SPEED_SAMPLE_MS = 2000

    def __init__(self, ctx: AppContext, navigate):
        super().__init__()
        self.ctx = ctx
        self.navigate = navigate
        self.selected_chat_id: int | None = None
        self.log_filter = "all"
        self.chat_chips: dict[int, QWidget] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 26, 40, 24)
        outer.setSpacing(0)

        head = QHBoxLayout()
        head.setSpacing(14)
        head_col = QVBoxLayout()
        head_col.setSpacing(2)
        head_col.addWidget(label("ВЫБРАННЫЙ ЧАТ", "kicker"))
        head_col.addWidget(h1("Сбор"))
        head.addLayout(head_col)
        self.headline_label = muted("")
        self.headline_label.setWordWrap(True)
        head.addWidget(self.headline_label, alignment=Qt.AlignBottom, stretch=1)
        # Кнопки — в собственном _FlowLayout, а не в этой же строке: на
        # минимальном размере окна (980×620, чек-лист Д4) три подписи
        # вместе с заголовком не помещаются и обрезаются за краем окна.
        # _FlowLayout переносит их на вторую строку вместо этого.
        outer.addLayout(head)
        outer.addSpacing(8)
        actions_host = QWidget()
        actions_flow = _FlowLayout(actions_host, margin=0, spacing=8)
        self.load_btn = button("Загрузить историю", "primary")
        self.load_btn.clicked.connect(self._on_toggle_load)
        actions_flow.addWidget(self.load_btn)
        self.listen_btn = button("Слушать новые сообщения", "secondary")
        self.listen_btn.clicked.connect(self._on_toggle_listen)
        actions_flow.addWidget(self.listen_btn)
        self.results_btn = button("Смотреть собранное", "ghost")
        self.results_btn.clicked.connect(self._on_open_results)
        actions_flow.addWidget(self.results_btn)
        outer.addWidget(actions_host)
        outer.addSpacing(10)

        # «Выпадающий список чатов заменён на ряд чипов» (design-brief.md
        # §4.4) — перенос на вторую строку через _FlowLayout, а не
        # горизонтальная прокрутка.
        chip_host = QWidget()
        self.chip_flow = _FlowLayout(chip_host, margin=0, spacing=6)
        outer.addWidget(chip_host)
        outer.addSpacing(12)

        body = QHBoxLayout()
        body.setSpacing(18)
        outer.addLayout(body, 1)

        # ---- left column: current chat + queue ----
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        # AsNeeded, not AlwaysOff: real chat titles/dates vary in width, and
        # a hidden scrollbar over AlwaysOff would silently clip content
        # instead of making it reachable — safer than tuning column ratios
        # to a single test window's exact content.
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        left_host = QWidget()
        left_scroll.setWidget(left_host)
        left_col = QVBoxLayout(left_host)
        left_col.setContentsMargins(0, 0, 8, 0)
        left_col.setSpacing(12)

        cur_frame = Card()
        cur_lay = QVBoxLayout(cur_frame)
        cur_lay.setContentsMargins(18, 16, 18, 16)
        cur_lay.setSpacing(0)

        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        self.title_label = QLabel("—")
        self.title_label.setTextFormat(Qt.PlainText)
        self.title_label.setStyleSheet("font-size: 20px; font-weight: 600;")
        self.title_label.setWordWrap(True)
        title_row.addWidget(self.title_label, 1)
        self.status_pill = StatusPill("idle")
        title_row.addWidget(self.status_pill, alignment=Qt.AlignVCenter)
        cur_lay.addLayout(title_row)
        cur_lay.addSpacing(14)

        self.metrics = MetricsBar([
            ("СОБРАНО", "0", ""),
            ("МЕДИАФАЙЛОВ НА ДИСКЕ", "0", ""),
            ("ПОСЛЕДНЕЕ СООБЩЕНИЕ", "—", ""),
            ("ГЛУБИНА ИСТОРИИ", "вся история", ""),
        ])
        cur_lay.addWidget(self.metrics)
        cur_lay.addSpacing(14)

        prog_row = QHBoxLayout()
        self.prog_label = label("Загрузка не запущена")
        self.prog_label.setStyleSheet(f"font-family: {theme.FONT_MONO}; font-size: 11px; color: {theme.TEXT_MUTED};")
        prog_row.addWidget(self.prog_label)
        prog_row.addStretch(1)
        self.prog_pct = label("0%")
        self.prog_pct.setStyleSheet(f"font-family: {theme.FONT_MONO}; font-size: 13px; color: {theme.ACCENT_300};")
        prog_row.addWidget(self.prog_pct)
        cur_lay.addLayout(prog_row)
        cur_lay.addSpacing(6)
        self.progress = AnimatedProgressBar(height=8)
        cur_lay.addWidget(self.progress)
        cur_lay.addSpacing(8)

        health_row = QHBoxLayout()
        self.health_label = label("")
        self.health_label.setStyleSheet(f"font-family: {theme.FONT_MONO}; font-size: 11px; color: {theme.TEXT_FAINT};")
        health_row.addWidget(self.health_label)
        health_row.addStretch(1)
        self.delay_label = label("")
        self.delay_label.setStyleSheet(f"font-family: {theme.FONT_MONO}; font-size: 11px; color: {theme.TEXT_FAINT};")
        health_row.addWidget(self.delay_label)
        cur_lay.addLayout(health_row)
        left_col.addWidget(cur_frame)

        # ---- live speed chart ----
        chart_frame = Card()
        chart_lay = QVBoxLayout(chart_frame)
        chart_lay.setContentsMargins(16, 12, 16, 12)
        chart_lay.setSpacing(6)
        chart_head = QHBoxLayout()
        chart_head.addWidget(label("СКОРОСТЬ СБОРА", "kicker"))
        chart_head.addStretch(1)
        self.chart_now_label = muted("")
        chart_head.addWidget(self.chart_now_label)
        chart_lay.addLayout(chart_head)
        self.speed_chart = LiveChart()
        chart_lay.addWidget(self.speed_chart)
        self.chart_foot_label = muted("сообщений в секунду · последние 2 минуты, по всем чатам")
        chart_lay.addWidget(self.chart_foot_label)
        left_col.addWidget(chart_frame)

        # ---- integrity: gaps in the collected id sequence ----
        gaps_frame = Card()
        gaps_lay = QVBoxLayout(gaps_frame)
        gaps_lay.setContentsMargins(16, 12, 16, 14)
        gaps_lay.setSpacing(8)
        gaps_head = QHBoxLayout()
        gaps_head.addWidget(label("ЦЕЛОСТНОСТЬ СОБРАННОГО", "kicker"))
        gaps_head.addStretch(1)
        self.patch_btn = button("Залатать пропуски", "secondary")
        self.patch_btn.clicked.connect(self._on_patch_gaps)
        gaps_head.addWidget(self.patch_btn)
        gaps_lay.addLayout(gaps_head)
        self.gaps_label = muted("")
        self.gaps_label.setWordWrap(True)
        gaps_lay.addWidget(self.gaps_label)
        left_col.addWidget(gaps_frame)

        # ---- history depth (date range) ----
        depth_frame = Card()
        depth_lay = QVBoxLayout(depth_frame)
        depth_lay.setContentsMargins(16, 12, 16, 14)
        depth_lay.setSpacing(8)
        depth_lay.addWidget(label("ЗА КАКОЙ ПЕРИОД СОБИРАТЬ", "kicker"))
        self.depth_all = QRadioButton("Всю историю с начала чата")
        self.depth_all.toggled.connect(self._on_depth_mode_changed)
        depth_lay.addWidget(self.depth_all)
        depth_row = QHBoxLayout()
        depth_row.setSpacing(10)
        self.depth_from = QRadioButton("Начиная с даты")
        depth_row.addWidget(self.depth_from)
        self.depth_date = QDateEdit()
        self.depth_date.setCalendarPopup(True)
        self.depth_date.setDisplayFormat("dd.MM.yyyy")
        self.depth_date.setDate(QDate.currentDate().addMonths(-3))
        depth_row.addWidget(self.depth_date)
        depth_row.addStretch(1)
        self.depth_save_btn = button("Применить", "primary")
        self.depth_save_btn.clicked.connect(self._on_save_depth)
        depth_row.addWidget(self.depth_save_btn)
        depth_lay.addLayout(depth_row)
        self.depth_hint = muted("")
        self.depth_hint.setWordWrap(True)
        depth_lay.addWidget(self.depth_hint)
        left_col.addWidget(depth_frame)

        # ---- queue panel ----
        queue_frame = Card()
        queue_lay = QVBoxLayout(queue_frame)
        queue_lay.setContentsMargins(16, 12, 16, 14)
        queue_lay.setSpacing(8)
        queue_head = QHBoxLayout()
        queue_head.addWidget(label("ОЧЕРЕДЬ", "kicker"))
        self.queue_count_label = muted("")
        queue_head.addWidget(self.queue_count_label)
        queue_head.addStretch(1)
        queue_lay.addLayout(queue_head)
        self.queue_list = QListWidget()
        self.queue_list.setStyleSheet(
            "QListWidget { border: none; background: transparent; font-size: 13px; }"
            "QListWidget::item { padding: 5px 6px; border-radius: 6px; }"
        )
        self.queue_list.setMaximumHeight(170)
        queue_lay.addWidget(self.queue_list)
        left_col.addWidget(queue_frame)
        left_col.addStretch(1)

        left_wrap = QVBoxLayout()
        left_wrap.setSpacing(8)
        left_wrap.addWidget(left_scroll, 1)

        body.addLayout(left_wrap, 66)

        # ---- right column: log ----
        log_head_row = QHBoxLayout()
        log_head_row.setSpacing(6)
        self.log_filter_group = QButtonGroup(self)
        self.log_filter_group.setExclusive(True)
        self.log_filter_chips: dict[str, QWidget] = {}
        for key, title in (("all", "всё"), ("pauses", "паузы"), ("errors", "ошибки")):
            c = chip(title)
            c.setChecked(key == "all")
            c.clicked.connect(lambda _c, k=key: self._on_log_filter(k))
            self.log_filter_group.addButton(c)
            log_head_row.addWidget(c)
            self.log_filter_chips[key] = c
        log_head_row.addStretch(1)
        body_right = QVBoxLayout()
        body_right.setSpacing(8)
        body_right.addLayout(log_head_row)
        # Уже колонка, чем брифом заданные 230px под чат (§3.9) — этой
        # правой колонке столько не выделить одновременно с левой картой
        # метрик на ширине окна 1320px, экран у́же мокапа брифа.
        self.log_panel = LogPanel(kicker="ЖУРНАЛ", chat_col_width=120)
        body_right.addWidget(self.log_panel, 1)
        body.addLayout(body_right, 34)

        ctx.collector.chats_changed.connect(self.refresh)
        ctx.collector.log_event.connect(self._on_log_event)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_health)
        self._timer.start(2000)

        # The speed chart samples the total message count on its own short
        # tick and plots the delta — one cheap COUNT(*) every two seconds
        # rather than any per-message signal wiring.
        self._last_total: int | None = None
        self._speed_timer = QTimer(self)
        self._speed_timer.timeout.connect(self._sample_speed)
        self._speed_timer.start(self.SPEED_SAMPLE_MS)

        self._reload_log()
        self._populate_chips()

    def _sample_speed(self) -> None:
        total = self.ctx.db.message_count()
        if self._last_total is None:
            self._last_total = total
            return
        delta = max(0, total - self._last_total)
        self._last_total = total
        per_second = delta / (self.SPEED_SAMPLE_MS / 1000)
        self.speed_chart.push(per_second)

        values = self.speed_chart.values()
        recent = values[-5:] if values else [0.0]
        avg = sum(recent) / len(recent)
        self.chart_now_label.setText(f"сейчас {per_second:.1f}/с · в среднем {avg:.1f}/с")

    def on_show(self, chat_id: int | None = None, **kwargs) -> None:
        self._populate_chips()
        if chat_id is not None:
            self.selected_chat_id = chat_id
        elif self.selected_chat_id is None:
            chats = self.ctx.db.list_chats()
            if chats:
                self.selected_chat_id = chats[0]["chat_id"]
        self._sync_chip_selection()
        self.refresh()

    def _populate_chips(self) -> None:
        for c in self.chat_chips.values():
            c.setParent(None)
            c.deleteLater()
        self.chat_chips.clear()
        for chat in self.ctx.db.list_chats():
            s = theme.STATUS_STYLES.get(chat["status"], theme.STATUS_STYLES["idle"])
            btn = chip(chat["title"])
            btn.setIcon(_dot_icon(s["dot"]))
            btn.setIconSize(QSize(5, 5))
            btn.clicked.connect(lambda _c, cid=chat["chat_id"]: self._on_pick_chip(cid))
            self.chip_flow.addWidget(btn)
            self.chat_chips[chat["chat_id"]] = btn
        self._sync_chip_selection()

    def _sync_chip_selection(self) -> None:
        for cid, btn in self.chat_chips.items():
            btn.setChecked(cid == self.selected_chat_id)

    def _on_pick_chip(self, chat_id: int) -> None:
        self.selected_chat_id = chat_id
        self._sync_chip_selection()
        self.refresh()

    def refresh(self) -> None:
        db = self.ctx.db
        chats = db.list_chats()
        loading = next((c for c in chats if c["status"] == "loading"), None)
        queued = [c for c in chats if c["status"] == "queued"]
        self.headline_label.setText(
            "один чат за раз — лимит Telegram общий на аккаунт" if loading else
            "история собрана, идёт только приём новых сообщений"
        )

        self.queue_list.clear()
        rows = ([{"title": loading["title"], "note": "грузится"}] if loading else []) + \
               [{"title": c["title"], "note": "ждёт"} for c in queued]
        self.queue_count_label.setText(
            f"{len(queued)} в очереди" if queued else ("грузится один" if loading else "пусто")
        )
        for r in rows:
            item = QListWidgetItem(f"{r['title']}   —   {r['note']}")
            if r["note"] == "грузится":
                from PySide6.QtGui import QColor
                item.setForeground(QColor(theme.ACCENT_400))
            self.queue_list.addItem(item)

        if not chats:
            self._sync_chip_selection()
        if self.selected_chat_id is None:
            return
        chat = db.get_chat(self.selected_chat_id)
        if not chat:
            return
        self._sync_chip_selection()
        self.title_label.setText(chat["title"])
        self.status_pill.set_status(chat["status"])
        count = db.message_count(chat["chat_id"])
        self.metrics.set_cell(0, f"{count:,}".replace(",", " "))
        cfg = self.ctx.config
        media_enabled = cfg.photos_enabled or cfg.videos_enabled or cfg.voice_enabled or cfg.documents_enabled
        media = db.media_count(chat["chat_id"]) if media_enabled else 0
        self.metrics.set_cell(1, f"{media:,}".replace(",", " ") if media_enabled else "выключено")
        last = db.last_message_date(chat["chat_id"])
        self.metrics.set_cell(2, short_dt(last) or "—")
        depth_text = "вся история" if chat["depth_mode"] == "all" else f"с {str(chat['depth_from_date'])[:10]}"
        if chat["history_done"]:
            depth_text += " · собрана"
        self.metrics.set_cell(3, depth_text)
        self._sync_depth_controls(chat)
        self._refresh_gaps(chat)

        chat_loading = chat["status"] == "loading"
        self.load_btn.setText("Приостановить загрузку" if chat_loading else
                               ("Догрузить историю" if chat["history_done"] else "Загрузить историю"))
        self.listen_btn.setText("Не слушать новые" if chat["enabled"] else "Слушать новые сообщения")

        if chat_loading:
            approx = chat["approx_total"]
            pct = min(100, round(100 * count / max(approx, 1))) if approx else None
            self.progress.set_progress(pct)
            self.progress.set_active(True)
            self.prog_pct.setText(f"{pct}%" if pct is not None else "…")
            self.prog_label.setText(
                f"Загрузка истории · пауза между запросами {self.ctx.collector.delay.current:.1f} с"
            )
        elif chat["history_done"]:
            self.progress.set_active(False)
            self.progress.set_progress(100)
            self.prog_pct.setText("100%")
            self.prog_label.setText("История собрана полностью")
        else:
            self.progress.set_active(False)
            self.progress.set_progress(0)
            self.prog_pct.setText("0%")
            self.prog_label.setText("Загрузка не запущена")

        self._refresh_health()

    # ---- integrity ----------------------------------------------------
    def _refresh_gaps(self, chat) -> None:
        summary = self.ctx.db.gap_summary(chat["chat_id"])
        gaps, missing = summary["gaps"], summary["missing"]
        if not gaps:
            self.gaps_label.setText(
                "Разрывов в собранной последовательности нет — между самым старым "
                "и самым новым сообщением ничего не пропущено."
            )
            self.patch_btn.setEnabled(False)
            return
        self.gaps_label.setText(
            f"Найдено {gaps} {plural(gaps, 'разрыв', 'разрыва', 'разрывов')} "
            f"на {missing} {plural(missing, 'сообщение', 'сообщения', 'сообщений')}. "
            "Такое остаётся после обрыва связи или остановки на середине загрузки. "
            "Часть разрывов — удалённые в Telegram сообщения: их не вернуть, "
            "и после проверки они останутся на месте."
        )
        self.patch_btn.setEnabled(True)

    def _on_patch_gaps(self) -> None:
        if self.selected_chat_id is None:
            return
        chat = self.ctx.db.get_chat(self.selected_chat_id)
        if not chat:
            return
        summary = self.ctx.db.gap_summary(self.selected_chat_id)
        if QMessageBox.question(
            self, "Залатать пропуски",
            f"Догрузить {summary['missing']} пропущенных сообщений в «{chat['title']}»?\n\n"
            "Запросы пойдут в общую очередь и учитываются в лимите Telegram — "
            "на время проверки загрузка истории других чатов подождёт."
        ) != QMessageBox.Yes:
            return

        self.patch_btn.setEnabled(False)
        self.patch_btn.setText("Проверяю…")

        def restore() -> None:
            self.patch_btn.setText("Залатать пропуски")
            self.refresh()

        def on_error(e) -> None:
            restore()
            from ...telegram.errors import humanize_error
            QMessageBox.warning(self, "Не получилось проверить", humanize_error(e))

        task = fire(self.ctx.collector.patch_gaps(self.selected_chat_id),
                    parent=self, on_error=on_error)

        def _done(t) -> None:
            if t.cancelled() or t.exception() is not None:
                return
            restore()
            patched = t.result()
            after = self.ctx.db.gap_summary(self.selected_chat_id)
            if patched:
                text = f"Догружено {patched} " + plural(patched, "сообщение", "сообщения", "сообщений") + "."
            else:
                text = "Ни одного сообщения догрузить не удалось."
            if after["gaps"]:
                text += (f"\n\nОсталось {after['gaps']} "
                          + plural(after["gaps"], "разрыв", "разрыва", "разрывов")
                          + " — скорее всего, это удалённые в Telegram сообщения.")
            else:
                text += "\n\nРазрывов больше нет."
            QMessageBox.information(self, "Проверка целостности", text)

        task.add_done_callback(_done)

    # ---- history depth ------------------------------------------------
    def _sync_depth_controls(self, chat) -> None:
        """Mirror the stored depth into the radio/date pair without
        echoing back a change signal (which would immediately re-save)."""
        from_date = chat["depth_from_date"]
        is_from = chat["depth_mode"] == "from_date" and bool(from_date)
        for w in (self.depth_all, self.depth_from, self.depth_date):
            w.blockSignals(True)
        self.depth_all.setChecked(not is_from)
        self.depth_from.setChecked(is_from)
        if is_from:
            parsed = QDate.fromString(str(from_date)[:10], "yyyy-MM-dd")
            if parsed.isValid():
                self.depth_date.setDate(parsed)
        for w in (self.depth_all, self.depth_from, self.depth_date):
            w.blockSignals(False)
        self.depth_date.setEnabled(is_from)

        if is_from:
            hint = f"Сейчас собирается всё, что новее {self.depth_date.date().toString('dd.MM.yyyy')}."
        else:
            hint = "Сейчас собирается вся доступная история чата с самого начала."
        if chat["history_done"]:
            hint += " История уже дочитана до этой границы — после смены периода нажмите «Догрузить историю»."
        self.depth_hint.setText(hint)

    def _on_depth_mode_changed(self, _checked: bool) -> None:
        self.depth_date.setEnabled(self.depth_from.isChecked())

    def _on_save_depth(self) -> None:
        if self.selected_chat_id is None:
            return
        db = self.ctx.db
        chat = db.get_chat(self.selected_chat_id)
        if not chat:
            return
        if self.depth_from.isChecked():
            mode, from_date = "from_date", self.depth_date.date().toString("yyyy-MM-dd")
        else:
            mode, from_date = "all", None

        # Widening the window (an earlier start, or "all") means there is
        # older history to fetch again, so history_done has to be cleared
        # or the backfill worker would consider the chat finished and skip
        # it. Narrowing needs no such reset — already-stored messages stay.
        old_mode = chat["depth_mode"]
        old_from = str(chat["depth_from_date"])[:10] if chat["depth_from_date"] else None
        if mode == "all":
            widened = old_mode != "all"
        elif old_mode == "all":
            widened = False  # adding a cutoff only narrows
        else:
            widened = bool(old_from) and from_date < old_from

        fields = {"depth_mode": mode, "depth_from_date": from_date}
        if widened:
            fields["history_done"] = 0
        db.set_chat_field(self.selected_chat_id, **fields)

        note = "Период обновлён."
        if widened:
            note += " Нужно догрузить более старые сообщения — нажмите «Догрузить историю»."
        else:
            note += " Уже собранные сообщения остаются в базе."
        QMessageBox.information(self, "Период сбора", note)
        self.refresh()

    def _refresh_health(self) -> None:
        h = self.ctx.collector.health
        self.health_label.setText(
            f"запросов за час: {h.requests_last_hour()} · пауз сегодня: {h.floodwaits_today} "
            f"· время в паузах: {h.pause_seconds_today} с"
        )
        self.delay_label.setText(f"текущая пауза: {self.ctx.collector.delay.current:.1f} с")

    def _on_toggle_load(self) -> None:
        if self.selected_chat_id is not None:
            self.ctx.collector.toggle_history_loading(self.selected_chat_id)
            self.refresh()

    def _on_toggle_listen(self) -> None:
        if self.selected_chat_id is not None:
            self.ctx.collector.toggle_listen(self.selected_chat_id)
            self.refresh()

    def _on_open_results(self) -> None:
        self.navigate("browse", chat_id=self.selected_chat_id)

    # ---- log ------------------------------------------------------
    def _on_log_filter(self, key: str) -> None:
        self.log_filter = key
        self._reload_log()

    def _visible_entries(self, entries: list[dict]) -> list[dict]:
        if self.log_filter == "all":
            return entries
        return [e for e in entries if _log_bucket(e) == self.log_filter]

    def _reload_log(self) -> None:
        entries = self.ctx.collector.log_entries[:200]
        self.log_panel.set_entries(self._visible_entries(entries))
        self._set_log_count()

    def _on_log_event(self, entry: dict) -> None:
        if self.log_filter == "all" or _log_bucket(entry) == self.log_filter:
            self.log_panel.add_entry(entry)
        self._set_log_count()

    def _set_log_count(self) -> None:
        # design-brief.md §4.4: счётчик считает *все* записи коллектора,
        # не только те, что проходят текущий фильтр — LogPanel.set_entries
        # само по себе посчитало бы только показанные, поэтому переопределяю
        # текст напрямую поверх его собственного счётчика.
        self.log_panel._count_label.setText(
            f"по всем чатам · {len(self.ctx.collector.log_entries)} записей"
        )
