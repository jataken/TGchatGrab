from __future__ import annotations

from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout, QWidget

from ... import theme
from ...context import AppContext
from ...widgets import Card, button, h1, label, muted
from .leads_tab import LeadsTab


class LeadsScreen(QWidget):
    """Заявки — standalone top-level screen (was a tab under «Боты»). Ends
    with the bridge cards from the redesign: the two ways a bot can meet
    the parser's collected chats, one of them a spam-risk warning rather
    than a feature to build."""

    def __init__(self, ctx: AppContext, navigate):
        super().__init__()
        self.ctx = ctx
        self.navigate = navigate

        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 28, 40, 24)
        outer.addWidget(h1("Заявки"))
        outer.addSpacing(8)

        self.leads_tab = LeadsTab(ctx)
        outer.addWidget(self.leads_tab, 1)

        outer.addSpacing(20)
        outer.addWidget(label("МОСТ СО СБОРОМ · ДВА ПУТИ, ОБА НА ВЫБОР", "kicker"))
        outer.addSpacing(8)
        bridges = QGridLayout()
        bridges.setSpacing(12)

        good = Card()
        good_lay = QVBoxLayout(good)
        good_lay.setContentsMargins(16, 14, 16, 14)
        good_row = QHBoxLayout()
        good_row.addWidget(label("Найденное в чате — в заявки"))
        good_tag = label("рекомендую")
        good_tag.setStyleSheet(
            f"background: rgba(120,190,150,40); color: {theme.GOOD_FG}; border-radius: 6px; "
            "padding: 2px 9px; font-size: 11px;"
        )
        good_row.addStretch(1)
        good_row.addWidget(good_tag)
        good_lay.addLayout(good_row)
        good_text = muted(
            "Правило ловит ключевые слова в отслеживаемых чатах и складывает находки "
            "черновиками заявок. Бот никому не пишет — вы разбираете сами. "
            "Аккаунту ничего не грозит."
        )
        good_text.setWordWrap(True)
        good_lay.addWidget(good_text)
        good_btn = button("Настроить правило", "secondary")
        good_btn.clicked.connect(lambda: self.navigate("rules"))
        good_lay.addWidget(good_btn)
        bridges.addWidget(good, 0, 0)

        risky = Card()
        risky_lay = QVBoxLayout(risky)
        risky_lay.setContentsMargins(16, 14, 16, 14)
        risky_row = QHBoxLayout()
        risky_row.addWidget(label("Написать авторам из собранного"))
        risky_tag = label("риск блокировки")
        risky_tag.setStyleSheet(
            f"background: rgba(180,70,90,40); color: {theme.BAD_FG}; border-radius: 6px; "
            "padding: 2px 9px; font-size: 11px;"
        )
        risky_row.addStretch(1)
        risky_row.addWidget(risky_tag)
        risky_lay.addLayout(risky_row)
        risky_text = muted(
            "Рассылка в личку людям, которые вам не писали, идёт от вашего же номера. "
            "Telegram считает это спамом: пауза между сообщениями помогает, но не спасает. "
            "Если делать — то десятками, а не сотнями."
        )
        risky_text.setWordWrap(True)
        risky_lay.addWidget(risky_text)
        risky_btn = button("Настроить триггер юзербота", "secondary")
        risky_btn.clicked.connect(lambda: self.navigate("rules"))
        risky_lay.addWidget(risky_btn)
        bridges.addWidget(risky, 0, 1)

        outer.addLayout(bridges)

    def on_show(self, **kwargs) -> None:
        self.leads_tab.on_show()
