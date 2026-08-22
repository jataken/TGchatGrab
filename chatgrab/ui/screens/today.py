"""«Сегодня» — the app's home screen. Answers the question a user wants
answered in the first five seconds after opening the app: how are the two
halves of the app doing right now, and is there one thing to go fix.

The screen is two parallel columns — Парсер on the left, Боты on the
right — so the state of both blocks is readable side by side without
switching anywhere. Each column carries its own headline state, a chart
of the last 16 days, its own totals, and a short list of rows that are
each a link to the screen that acts on them."""
from __future__ import annotations

import datetime as dt

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget,
)

from .. import theme
from ..context import AppContext
from ..format import short_dt
from ..widgets import ActivityBars, Card, button, fmt_int as _fmt, h1, label, muted, plural as _plural
from ...core import lead as lead_domain

# Д4 («Плотный рефреш»): цвета тона строк — из theme.py, не хардкод-хекс
# (design-brief.md §9.7). Раньше "bad" был #c98a9a — тем самым устаревшим
# красным, который Д1 уже поправил в самом theme.py (BAD/BAD_FG); здесь он
# был отдельной копией того же значения и не подхватил правку автоматически.
_TONE_COLORS = {
    "accent": theme.ACCENT,
    "good": theme.GOOD,
    "warn": theme.WARN,
    "bad": theme.BAD,
    "faint": theme.TEXT_FAINT,
}


class StatCell(QWidget):
    """Uppercase kicker over a tabular-figure value — the column's own
    small totals row."""

    def __init__(self, key: str, value: str = "", tone: str = ""):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addWidget(label(key, "kicker"))
        self.value_label = label(value)
        color = _TONE_COLORS.get(tone, "")
        self.value_label.setStyleSheet(
            "font-size: 18px;" + (f" color: {color};" if color else "")
        )
        lay.addWidget(self.value_label)

    def set_value(self, value: str, tone: str = "") -> None:
        self.value_label.setText(value)
        color = _TONE_COLORS.get(tone, "")
        self.value_label.setStyleSheet(
            "font-size: 18px;" + (f" color: {color};" if color else "")
        )


class ActionRow(QFrame):
    """One line inside a column: a status dot, a title + detail, and the
    action that resolves it. Clicking anywhere on the row does the same
    thing as the action link — the whole row is the target."""

    def __init__(self, title: str, detail: str, action_text: str, on_act, tone: str = "faint"):
        super().__init__()
        color = _TONE_COLORS.get(tone, _TONE_COLORS["faint"])
        # QLabel subclasses QFrame, so a bare `QFrame { background: … }`
        # rule would paint behind every child label too — scope the rule
        # to this widget by object name.
        self.setObjectName("actionRow")
        self.setStyleSheet(
            f"QFrame#actionRow {{ background: {theme.CHECKBOX_OFF_BG}; border-radius: 9px; }}"
            f"QFrame#actionRow:hover {{ background: {theme.HOVER_NEUTRAL}; }}"
        )
        self.setCursor(Qt.PointingHandCursor)
        self._on_act = on_act

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(11)

        dot = label("●")
        dot.setStyleSheet(f"color: {color}; font-size: 11px;")
        lay.addWidget(dot, alignment=Qt.AlignTop)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        title_label = label(title)
        title_label.setWordWrap(True)
        title_label.setStyleSheet("font-size: 13.5px;")
        text_col.addWidget(title_label)
        detail_label = muted(detail)
        detail_label.setWordWrap(True)
        text_col.addWidget(detail_label)
        lay.addLayout(text_col, 1)

        act = label(action_text)
        act.setStyleSheet(f"color: {theme.ACCENT_400}; font-size: 12px;")
        lay.addWidget(act, alignment=Qt.AlignVCenter)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._on_act()


class BlockColumn(Card):
    """One of the two panels. Holds a header (title + live state), a
    16-day chart, a totals row, and the rows list. `Card` (Д2) gives the
    top inner blik for free — this class only adds its own layout."""

    def __init__(self, title: str, chart_caption: str):
        super().__init__()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(0)

        head = QHBoxLayout()
        head.addWidget(label(title.upper(), "kicker"))
        head.addStretch(1)
        self.state_label = label("")
        self.state_label.setStyleSheet("font-size: 12px;")
        head.addWidget(self.state_label)
        lay.addLayout(head)
        lay.addSpacing(12)

        self.chart = ActivityBars(height=56)
        lay.addWidget(self.chart)
        self.chart_caption = muted(chart_caption)
        lay.addWidget(self.chart_caption)
        lay.addSpacing(14)

        self.stats_row = QHBoxLayout()
        self.stats_row.setSpacing(22)
        lay.addLayout(self.stats_row)
        lay.addSpacing(16)

        self.rows_lay = QVBoxLayout()
        self.rows_lay.setSpacing(6)
        lay.addLayout(self.rows_lay)
        lay.addStretch(1)

        self._rows: list[QWidget] = []

    def set_state(self, text: str, tone: str = "faint") -> None:
        self.state_label.setText(text)
        self.state_label.setStyleSheet(
            f"font-size: 12px; color: {_TONE_COLORS.get(tone, _TONE_COLORS['faint'])};"
        )

    def clear_rows(self) -> None:
        for w in self._rows:
            w.setParent(None)
            w.deleteLater()
        self._rows.clear()

    def add_row(self, title: str, detail: str, action: str, on_act, tone: str = "faint") -> None:
        row = ActionRow(title, detail, action, on_act, tone)
        self.rows_lay.addWidget(row)
        self._rows.append(row)


class TodayScreen(QWidget):
    def __init__(self, ctx: AppContext, navigate):
        super().__init__()
        self.ctx = ctx
        self.navigate = navigate

        outer_scroll = QScrollArea()
        outer_scroll.setWidgetResizable(True)
        container = QWidget()
        outer_scroll.setWidget(container)
        wrap = QVBoxLayout(self)
        wrap.setContentsMargins(0, 0, 0, 0)
        wrap.addWidget(outer_scroll)

        outer = QVBoxLayout(container)
        outer.setContentsMargins(40, 28, 40, 30)

        head = QHBoxLayout()
        head.setSpacing(14)
        head.addWidget(h1("Сегодня"))
        self.date_label = muted("")
        head.addWidget(self.date_label)
        head.addStretch(1)
        outer.addLayout(head)
        outer.addSpacing(10)

        # design-brief.md §7 «Не авторизован»: плашка WARN со ссылкой на
        # «Подключение» — раньше нигде не показывалась, единственным
        # индикатором была точка в сайдбаре (main_window.py). Объектное
        # имя, а не голый setStyleSheet() на контейнере с детьми — тот же
        # приём, что у StatusPill (Д2), чтобы фон не протёк на кнопку.
        self.auth_warning = QWidget()
        self.auth_warning.setObjectName("todayauthwarn")
        warn_lay = QHBoxLayout(self.auth_warning)
        warn_lay.setContentsMargins(12, 9, 10, 9)
        warn_lay.setSpacing(10)
        self.auth_warning_label = QLabel("Аккаунт не подключён — сбор остановлен.")
        self.auth_warning_label.setTextFormat(Qt.PlainText)
        self.auth_warning_label.setStyleSheet(f"color: {theme.WARN_FG}; font-size: 12px; background: transparent;")
        warn_lay.addWidget(self.auth_warning_label, 1)
        goto_connect_btn = button("Подключение", "ghost")
        goto_connect_btn.clicked.connect(lambda: self.navigate("connect"))
        warn_lay.addWidget(goto_connect_btn)
        self.auth_warning.setStyleSheet(
            "QWidget#todayauthwarn { background: rgba(240,198,160,.07); "
            "border: 1px solid rgba(240,198,160,.22); border-radius: 10px; }"
        )
        self.auth_warning.setVisible(False)
        outer.addWidget(self.auth_warning)
        outer.addSpacing(8)

        # design-brief.md §9.6: at 980×620 (минимум приложения) две колонки
        # side by side обрезали правую — ниже порога ширины они встают друг
        # под друга вместо горизонтальной пары, тот же QGridLayout-приём,
        # что и у сетки ботов (bots/list_tab.py), только 1×2 против 2×1.
        self._columns_grid = QGridLayout()
        self._columns_grid.setHorizontalSpacing(18)
        self._columns_grid.setVerticalSpacing(18)
        self.collect_col = BlockColumn("Сбор", "сообщений в день, последние 16 суток")
        self.bots_col = BlockColumn("Боты", "заявок в день, последние 16 суток")
        outer.addLayout(self._columns_grid)
        outer.addStretch(1)
        self._columns_wide = None
        self._relayout_columns()

        self.collect_msgs = StatCell("Сообщений")
        self.collect_chats = StatCell("Чатов в работе")
        self.collect_media = StatCell("Медиафайлов")
        for cell in (self.collect_msgs, self.collect_chats, self.collect_media):
            self.collect_col.stats_row.addWidget(cell)
        self.collect_col.stats_row.addStretch(1)

        self.bots_running = StatCell("Ботов работает")
        self.bots_new = StatCell("Новых заявок")
        self.bots_total = StatCell("Заявок всего")
        for cell in (self.bots_running, self.bots_new, self.bots_total):
            self.bots_col.stats_row.addWidget(cell)
        self.bots_col.stats_row.addStretch(1)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(3000)
        ctx.collector.stats_changed.connect(self.refresh)
        ctx.collector.chats_changed.connect(self.refresh)
        ctx.bot_manager.bots_changed.connect(self.refresh)

    _STACK_BELOW_WIDTH = 900

    def _relayout_columns(self) -> None:
        wide = self.width() >= self._STACK_BELOW_WIDTH
        if wide == self._columns_wide:
            return
        self._columns_wide = wide
        self._columns_grid.removeWidget(self.collect_col)
        self._columns_grid.removeWidget(self.bots_col)
        if wide:
            self._columns_grid.addWidget(self.collect_col, 0, 0)
            self._columns_grid.addWidget(self.bots_col, 0, 1)
            self._columns_grid.setColumnStretch(0, 1)
            self._columns_grid.setColumnStretch(1, 1)
        else:
            self._columns_grid.addWidget(self.collect_col, 0, 0)
            self._columns_grid.addWidget(self.bots_col, 1, 0)
            self._columns_grid.setColumnStretch(0, 1)
            self._columns_grid.setColumnStretch(1, 0)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._relayout_columns()

    def on_show(self, **kwargs) -> None:
        self._relayout_columns()
        self.refresh()

    def refresh(self) -> None:
        now = dt.datetime.now()
        month_name = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
                      "августа", "сентября", "октября", "ноября", "декабря"][now.month - 1]
        self.date_label.setText(f"{now.day} {month_name}")
        self.auth_warning.setVisible(not self.ctx.tg.authorized)

        self._refresh_collect()
        self._refresh_bots()

    # ---- left column: the parser ------------------------------------
    def _refresh_collect(self) -> None:
        db = self.ctx.db
        chats = db.list_chats()
        col = self.collect_col
        col.clear_rows()

        loading = next((c for c in chats if c["status"] == "loading"), None)
        queued = [c for c in chats if c["status"] == "queued"]
        silent = [c for c in chats if c["status"] == "off"]
        listening = [c for c in chats if c["status"] == "listening"]

        if loading:
            col.set_state("грузит историю", "accent")
        elif listening:
            col.set_state("слушает новые", "good")
        elif not chats:
            col.set_state("нет источников", "warn")
        else:
            col.set_state("простаивает", "faint")

        # Sum the per-chat 16-day activity series into one block-level chart.
        series = [0] * 16
        for c in chats:
            bars = db.activity_bars(c["chat_id"])
            for i, v in enumerate(bars[-16:]):
                series[i] += v
        col.chart.set_values(series)

        total_msgs = sum(db.message_count(c["chat_id"]) for c in chats)
        total_media = sum(db.media_count(c["chat_id"]) for c in chats)
        enabled_n = len([c for c in chats if c["enabled"]])
        self.collect_msgs.set_value(_fmt(total_msgs))
        self.collect_chats.set_value(f"{enabled_n} / {len(chats)}")
        self.collect_media.set_value(_fmt(total_media))

        if not chats:
            col.add_row(
                "Пока нет ни одного источника",
                "Добавьте чат — история встанет в очередь и начнёт собираться.",
                "добавить", lambda: self.navigate("chats"), "warn",
            )
            return

        if loading:
            count = db.message_count(loading["chat_id"])
            approx = loading["approx_total"]
            detail = f"Собрано {_fmt(count)}"
            detail += f" из ≈{_fmt(approx)}" if approx else ""
            detail += f", в очереди ещё {len(queued)}" if queued else ""
            col.add_row(loading["title"], detail, "открыть",
                        lambda cid=loading["chat_id"]: self.navigate("collect", chat_id=cid), "accent")

        for c in silent:
            last = db.last_message_date(c["chat_id"])
            detail = "сбор выключен вручную"
            detail += f", с {short_dt(last)}" if last else ""
            col.add_row(c["title"], detail, "включить",
                        (lambda cid=c["chat_id"]: self.ctx.collector.set_chat_enabled(cid, True)), "warn")

        errored = [c for c in chats if c["last_error"]]
        for c in errored[:2]:
            col.add_row(c["title"], f"чат стал недоступен: {c['last_error']}", "открыть",
                        lambda cid=c["chat_id"]: self.navigate("collect", chat_id=cid), "bad")

        col.add_row(
            "Собранное", f"{_fmt(total_msgs)} " +
            _plural(total_msgs, "сообщение готово", "сообщения готовы", "сообщений готовы") + " к выгрузке",
            "открыть", lambda: self.navigate("browse"),
        )

    # ---- right column: the bots --------------------------------------
    def _refresh_bots(self) -> None:
        db = self.ctx.db
        bots = db.list_bots()
        leads = db.list_leads()
        # С10: "новая"/"в работе" are derived per-lead against that
        # lead's *own* funnel (stages cached by funnel_id — leads span
        # bots, which can in principle span different funnels), not a
        # hardcoded status set that only ever matched one funnel.
        stages_cache: dict[int | None, list] = {}

        def _stages_for(funnel_id):
            if funnel_id not in stages_cache:
                stages_cache[funnel_id] = db.list_funnel_stages(funnel_id) if funnel_id else []
            return stages_cache[funnel_id]

        def _bucket(l):
            return lead_domain.bucket_for_stage(_stages_for(l["funnel_id"]), l["status"])

        new_leads = [l for l in leads if _bucket(l) == "new"]
        bad_bots = [b for b in bots if b["status"] == "error"]
        running = [b for b in bots if b["status"] == "running"]
        col = self.bots_col
        col.clear_rows()

        if bad_bots:
            col.set_state("есть ошибка", "bad")
        elif running:
            col.set_state("работают", "good")
        elif not bots:
            col.set_state("ботов нет", "faint")
        else:
            col.set_state("остановлены", "faint")

        col.chart.set_values(self._leads_series(leads))

        self.bots_running.set_value(f"{len(running)} / {len(bots)}")
        self.bots_new.set_value(_fmt(len(new_leads)), "good" if new_leads else "")
        self.bots_total.set_value(_fmt(len(leads)))

        if not bots:
            col.add_row(
                "Ботов пока нет",
                "Бот может ловить ключевые слова в собираемых чатах и складывать находки в заявки.",
                "создать", lambda: self.navigate("bots"), "faint",
            )
            return

        if new_leads:
            latest = new_leads[0]
            contact = db.get_contact(latest["contact_id"]) if latest["contact_id"] else None
            handle = latest["display_name"] or \
                (f"@{latest['username']}" if latest["username"] else None) or \
                (f"@{contact['username']}" if contact and contact["username"] else None) or "контакта"
            col.add_row(
                f"{len(new_leads)} " +
                _plural(len(new_leads), "новая заявка", "новые заявки", "новых заявок"),
                f"самая свежая — от {handle}", "разобрать",
                lambda: self.navigate("leads"), "good",
            )

        for b in bad_bots:
            col.add_row(b["name"], b["last_error"] or "бот остановился с ошибкой",
                        "починить", lambda: self.navigate("bots"), "bad")

        in_progress = [l for l in leads if _bucket(l) == "in_progress"]
        if in_progress:
            col.add_row(
                f"{len(in_progress)} " + _plural(len(in_progress), "заявка", "заявки", "заявок") + " в работе",
                "уже разбираются менеджером", "смотреть", lambda: self.navigate("leads"),
            )

        col.add_row("Правила", "что бот делает, когда что-то происходит",
                    "настроить", lambda: self.navigate("rules"))

    def _leads_series(self, leads) -> list[int]:
        """Leads per day over the last 16 days — the bots column's
        counterpart to the parser's message volume."""
        today = dt.date.today()
        buckets = {today - dt.timedelta(days=i): 0 for i in range(16)}
        for lead in leads:
            try:
                created = dt.datetime.fromisoformat(str(lead["created_at"])).date()
            except (ValueError, TypeError):
                continue
            if created in buckets:
                buckets[created] += 1
        return [buckets[today - dt.timedelta(days=i)] for i in range(15, -1, -1)]
