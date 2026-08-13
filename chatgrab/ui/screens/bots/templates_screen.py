from __future__ import annotations

from PySide6.QtWidgets import QVBoxLayout, QWidget

from ...context import AppContext
from ...widgets import h1, muted
from .templates_tab import TemplatesTab


class TemplatesScreen(QWidget):
    """Шаблоны — its own screen rather than a dialog reached from Правила.

    Templates became load-bearing once the engine started reading them:
    sending actions reference them, and a scenario's closing message is
    one. A thing other objects point at should be somewhere you can get to
    directly."""

    def __init__(self, ctx: AppContext, navigate):
        super().__init__()
        self.ctx = ctx
        self.navigate = navigate

        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 28, 40, 24)
        outer.setSpacing(0)
        outer.addWidget(h1("Шаблоны"))
        outer.addSpacing(4)
        hint = muted(
            "Готовые тексты, которые бот отправляет. Переменные вида {name} или {company} "
            "подставляются из данных контакта и его ответов в сценарии — то, что не удалось "
            "подставить, останется в тексте как есть."
        )
        hint.setWordWrap(True)
        outer.addWidget(hint)
        outer.addSpacing(12)

        self.templates_tab = TemplatesTab(ctx)
        outer.addWidget(self.templates_tab, 1)

    def on_show(self, **kwargs) -> None:
        self.templates_tab.on_show()
