from __future__ import annotations

from PySide6.QtWidgets import QVBoxLayout, QWidget

from ...context import AppContext
from ...widgets import h1
from .log_tab import LogTab


class BotLogScreen(QWidget):
    """Журнал ботов — same log-panel pattern as the parser's Сбор screen,
    now its own screen instead of a tab under «Боты»."""

    def __init__(self, ctx: AppContext, navigate):
        super().__init__()
        self.ctx = ctx
        self.navigate = navigate

        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 28, 40, 24)
        outer.addWidget(h1("Журнал"))
        outer.addSpacing(8)

        self.log_tab = LogTab(ctx)
        outer.addWidget(self.log_tab, 1)

    def on_show(self, **kwargs) -> None:
        self.log_tab.on_show()
