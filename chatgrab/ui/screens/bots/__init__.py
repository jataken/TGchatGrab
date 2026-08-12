from __future__ import annotations

from PySide6.QtWidgets import QTabWidget, QVBoxLayout, QWidget

from ...context import AppContext
from ...widgets import h1
from .analytics_tab import AnalyticsTab
from .leads_tab import LeadsTab
from .list_tab import BotsListTab
from .log_tab import LogTab
from .rules_tab import RulesTab
from .scenarios_tab import ScenariosTab
from .templates_tab import TemplatesTab
from .test_tab import TestModeTab


class BotsScreen(QWidget):
    def __init__(self, ctx: AppContext, navigate):
        super().__init__()
        self.ctx = ctx
        self.navigate = navigate

        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 28, 40, 18)
        outer.setSpacing(0)
        outer.addWidget(h1("Конструктор ботов"))

        self.tabs = QTabWidget()
        self.list_tab = BotsListTab(ctx)
        self.leads_tab = LeadsTab(ctx)
        self.rules_tab = RulesTab(ctx)
        self.scenarios_tab = ScenariosTab(ctx)
        self.templates_tab = TemplatesTab(ctx)
        self.test_tab = TestModeTab(ctx)
        self.log_tab = LogTab(ctx)
        self.analytics_tab = AnalyticsTab(ctx)

        self.tabs.addTab(self.list_tab, "Боты")
        self.tabs.addTab(self.leads_tab, "Заявки")
        self.tabs.addTab(self.rules_tab, "Правила")
        self.tabs.addTab(self.scenarios_tab, "Сценарии")
        self.tabs.addTab(self.templates_tab, "Шаблоны")
        self.tabs.addTab(self.test_tab, "Тест")
        self.tabs.addTab(self.log_tab, "Журнал")
        self.tabs.addTab(self.analytics_tab, "Аналитика")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        outer.addWidget(self.tabs, 1)

    def on_show(self, **kwargs) -> None:
        self._on_tab_changed(self.tabs.currentIndex())

    def _on_tab_changed(self, index: int) -> None:
        widget = self.tabs.widget(index)
        on_show = getattr(widget, "on_show", None)
        if on_show:
            on_show()
