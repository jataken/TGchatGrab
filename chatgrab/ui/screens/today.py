"""«Сегодня» — the app's home screen. Answers the question a user wants
answered in the first five seconds after opening the app: is anything
wrong, and if so, what's the one thing to do about it. Everything here
is a link to somewhere else — no widgets that don't lead to an action."""
from __future__ import annotations

import datetime as dt

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget,
)

from ..context import AppContext
from ..widgets import KeyValue, button, h1, label, muted

_TONE_COLORS = {
    "accent": "#9184d9",
    "good": "#7fc79b",
    "warn": "#f0c6a0",
    "bad": "#c98a9a",
}


def _plural(n: int, one: str, few: str, many: str) -> str:
    a = abs(n) % 100
    b = a % 10
    if 10 < a < 20:
        return many
    if 1 < b < 5:
        return few
    if b == 1:
        return one
    return many


class AttentionRow(QFrame):
    def __init__(self, title: str, detail: str, action_text: str, on_act, tone: str = "accent"):
        super().__init__()
        color = _TONE_COLORS.get(tone, _TONE_COLORS["accent"])
        self.setStyleSheet(
            f"QFrame {{ background: #232532; border-radius: 10px; "
            f"border-left: 3px solid {color}; }}"
        )
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 13, 16, 13)
        lay.setSpacing(16)

        text_col = QVBoxLayout()
        text_col.setSpacing(3)
        title_label = label(title)
        title_label.setStyleSheet("font-size: 14.5px;")
        title_label.setWordWrap(True)
        text_col.addWidget(title_label)
        detail_label = muted(detail)
        detail_label.setWordWrap(True)
        text_col.addWidget(detail_label)
        lay.addLayout(text_col, 1)

        act_btn = button(action_text, "primary")
        act_btn.clicked.connect(on_act)
        lay.addWidget(act_btn, alignment=Qt.AlignVCenter)


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
        outer.setContentsMargins(40, 28, 40, 32)

        head = QHBoxLayout()
        head.addWidget(h1("Сегодня"))
        self.date_label = muted("")
        head.addWidget(self.date_label)
        head.addStretch(1)
        outer.addLayout(head)
        outer.addSpacing(18)

        self.feed_lay = QVBoxLayout()
        self.feed_lay.setSpacing(8)
        outer.addLayout(self.feed_lay)

        self.nothing_wrong = muted(
            "Всё идёт своим ходом: история собрана, боты отвечают, заявки разобраны. "
            "Следующая проверка сама себя покажет здесь."
        )
        self.nothing_wrong.setWordWrap(True)
        self.nothing_wrong.setStyleSheet(
            "background: #232532; border-radius: 10px; padding: 24px 16px; font-size: 13.5px;"
        )
        outer.addWidget(self.nothing_wrong)

        outer.addSpacing(22)
        pulse_wrap = QWidget()
        pulse_wrap.setStyleSheet("border-top: 1px solid #33354a;")
        self.pulse_lay = QHBoxLayout(pulse_wrap)
        self.pulse_lay.setContentsMargins(0, 16, 0, 0)
        self.pulse_lay.setSpacing(28)
        self.kv_messages = KeyValue("Сообщений в базе")
        self.kv_chats = KeyValue("Чатов слушаем")
        self.kv_bots = KeyValue("Ботов работает")
        self.kv_leads = KeyValue("Заявок всего")
        self.kv_export = KeyValue("Прошлая выгрузка")
        for kv in (self.kv_messages, self.kv_chats, self.kv_bots, self.kv_leads, self.kv_export):
            self.pulse_lay.addWidget(kv)
        self.pulse_lay.addStretch(1)
        outer.addWidget(pulse_wrap)
        outer.addStretch(1)

        self._rows: list[QWidget] = []

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(3000)
        ctx.collector.stats_changed.connect(self.refresh)
        ctx.collector.chats_changed.connect(self.refresh)
        ctx.bot_manager.bots_changed.connect(self.refresh)

    def on_show(self, **kwargs) -> None:
        self.refresh()

    def _clear_feed(self) -> None:
        for w in self._rows:
            w.setParent(None)
            w.deleteLater()
        self._rows.clear()

    def _add_row(self, title: str, detail: str, action_text: str, on_act, tone: str = "accent") -> None:
        row = AttentionRow(title, detail, action_text, on_act, tone)
        self.feed_lay.addWidget(row)
        self._rows.append(row)

    def refresh(self) -> None:
        db = self.ctx.db
        chats = db.list_chats()
        bots = db.list_bots()
        leads = db.list_leads()
        new_leads = [l for l in leads if l["status"] == "new"]

        now = dt.datetime.now()
        month_name = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
                      "августа", "сентября", "октября", "ноября", "декабря"][now.month - 1]
        self.date_label.setText(f"{now.day} {month_name}")

        self._clear_feed()
        loading = next((c for c in chats if c["status"] == "loading"), None)
        queued = [c for c in chats if c["status"] == "queued"]
        if loading:
            count = db.message_count(loading["chat_id"])
            approx = loading["approx_total"]
            pct = min(100, round(100 * count / max(approx, 1))) if approx else 0
            detail = f"Собрано {count:,}".replace(",", " ")
            detail += f" из ≈{approx:,}".replace(",", " ") if approx else ""
            detail += f" сообщений, в очереди ещё {len(queued)}." if queued else " сообщений."
            self._add_row(
                f"История «{loading['title']}» ещё грузится", detail,
                "Открыть сбор", lambda cid=loading["chat_id"]: self.navigate("collect", chat_id=cid),
            )

        if new_leads:
            latest = new_leads[0]
            contact = db.get_contact(latest["contact_id"])
            handle = f"@{contact['username']}" if contact and contact["username"] else "контакта"
            self._add_row(
                f"{len(new_leads)} " +
                _plural(len(new_leads), "новая заявка ждёт", "новые заявки ждут", "новых заявок ждут") +
                " разбора",
                f"Самая свежая — от {handle}.",
                "Разобрать", lambda: self.navigate("leads"), tone="good",
            )

        for c in chats:
            if c["status"] == "off":
                last_txt = db.last_message_date(c["chat_id"])
                detail = "Сбор выключен вручную"
                detail += f", новые сообщения не пишутся с {str(last_txt)[:16].replace('T', ' ')}." \
                    if last_txt else "."
                detail += " Данные в базе целы."
                self._add_row(
                    f"Чат «{c['title']}» замолчал", detail, "Включить сбор",
                    (lambda cid=c["chat_id"]: self.ctx.collector.set_chat_enabled(cid, True)),
                    tone="warn",
                )

        for b in bots:
            if b["status"] == "error":
                self._add_row(
                    f"Бот «{b['name']}» остановился",
                    b["last_error"] or "Причина не указана — загляните в журнал бота.",
                    "Починить", lambda: self.navigate("bots"), tone="bad",
                )

        has_rows = bool(self._rows)
        self.nothing_wrong.setVisible(not has_rows)

        total_msgs = sum(db.message_count(c["chat_id"]) for c in chats)
        enabled_n = len([c for c in chats if c["enabled"]])
        running_bots = len([b for b in bots if b["status"] == "running"])
        logs = db.list_export_log(limit=1)
        last_export = logs[0]["created_at"][:16].replace("T", " ") if logs else "ещё не было"

        self.kv_messages.set_value(f"{total_msgs:,}".replace(",", " "))
        self.kv_chats.set_value(f"{enabled_n} / {len(chats)}")
        self.kv_bots.set_value(f"{running_bots} / {len(bots)}")
        self.kv_leads.set_value(f"{len(leads):,}".replace(",", " "))
        self.kv_export.set_value(last_export)
