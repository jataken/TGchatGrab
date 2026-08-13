from __future__ import annotations

from PySide6.QtWidgets import QScrollArea, QVBoxLayout, QWidget

from ...context import AppContext
from ...widgets import label
from .analytics_tab import AnalyticsTab
from .list_tab import BotsListTab


class BotsScreen(QWidget):
    """Боты — the block's landing screen: every bot in this instance, each
    with its own type, rules and data (мастер создания — «Правила»,
    «Сценарий» — reached from here or from the sidebar). Contact ranking
    stays visible below the list rather than living behind its own nav
    item, since it's read-only context for the same bots, not a separate
    task."""

    def __init__(self, ctx: AppContext, navigate):
        super().__init__()
        self.ctx = ctx
        self.navigate = navigate

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        scroll.setWidget(container)
        wrap = QVBoxLayout(self)
        wrap.setContentsMargins(0, 0, 0, 0)
        wrap.addWidget(scroll)

        outer = QVBoxLayout(container)
        outer.setContentsMargins(40, 28, 40, 30)

        self.list_tab = BotsListTab(ctx)
        outer.addWidget(self.list_tab)

        outer.addSpacing(18)
        outer.addWidget(label("АНАЛИТИКА ПО КОНТАКТАМ", "kicker"))
        outer.addSpacing(8)
        self.analytics_tab = AnalyticsTab(ctx)
        self.analytics_tab.setMinimumHeight(360)
        outer.addWidget(self.analytics_tab)

    def on_show(self, **kwargs) -> None:
        self.list_tab.on_show()
        self.analytics_tab.on_show()
