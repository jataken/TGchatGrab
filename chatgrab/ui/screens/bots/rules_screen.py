from __future__ import annotations

import json

from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLineEdit, QPlainTextEdit, QVBoxLayout, QWidget,
)

from ...context import AppContext
from ...widgets import button, h1, muted
from ....bots.rules_engine import IncomingEvent, RulesEngine
from .rules_tab import RulesTab
from .templates_tab import TemplatesTab


class TemplatesDialog(QDialog):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Шаблоны сообщений")
        self.setMinimumSize(760, 480)
        lay = QVBoxLayout(self)
        self.templates_tab = TemplatesTab(ctx)
        lay.addWidget(self.templates_tab)
        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = button("Закрыть", "secondary")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        lay.addLayout(close_row)
        self.templates_tab.on_show()


class RuleDryRunDialog(QDialog):
    """Dry-run: shows what triggers *would* fire on a sample message,
    without touching leads or contacts."""

    def __init__(self, ctx: AppContext, bot_id: int | None, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.bot_id = bot_id
        self.rules = RulesEngine(ctx.db)
        self.setWindowTitle("Проверить на примере сообщения")
        self.setMinimumWidth(480)

        lay = QVBoxLayout(self)
        lay.addWidget(muted(
            "Введите сообщение, как будто его написал контакт, — покажем, "
            "какие триггеры сработают и что бы сделали действия."
        ))
        self.input = QLineEdit()
        self.input.setPlaceholderText("Текст входящего сообщения")
        lay.addWidget(self.input)
        run_btn = button("Прогнать", "primary")
        run_btn.clicked.connect(self._on_run)
        lay.addWidget(run_btn)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        lay.addWidget(self.output, 1)

    def _on_run(self) -> None:
        if self.bot_id is None:
            self.output.setPlainText("Сначала выберите бота вверху экрана.")
            return
        text = self.input.text()
        event = IncomingEvent(contact_telegram_id=0, username="тест", text=text, chat_type="dm")
        triggers = self.rules.triggers_for(self.bot_id, event)
        if not triggers:
            self.output.setPlainText("Ни один триггер не сработал бы на это сообщение.")
            return
        lines = []
        for trig in triggers:
            lines.append(f"Триггер «{trig['type']}» сработал бы. Действия:")
            for action in self.ctx.db.list_actions(trig["id"]):
                cfg = json.loads(action["config"])
                lines.append(f"  → {action['type']}: {cfg}")
        self.output.setPlainText("\n".join(lines))


class RulesScreen(QWidget):
    """Правила — standalone top-level screen (was a tab under «Боты»)."""

    def __init__(self, ctx: AppContext, navigate):
        super().__init__()
        self.ctx = ctx
        self.navigate = navigate

        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 28, 40, 24)
        outer.setSpacing(0)

        header = QHBoxLayout()
        header.addWidget(h1("Правила"))
        header.addStretch(1)
        templates_btn = button("Шаблоны сообщений", "ghost")
        templates_btn.clicked.connect(self._open_templates)
        header.addWidget(templates_btn)
        test_btn = button("Проверить сообщением", "secondary")
        test_btn.clicked.connect(self._open_dry_run)
        header.addWidget(test_btn)
        outer.addLayout(header)
        outer.addSpacing(4)
        outer.addWidget(muted(
            "Что бот делает, когда что-то происходит. Читается одной строкой: "
            "событие — и дальше действия по порядку."
        ))
        outer.addSpacing(10)

        self.rules_tab = RulesTab(ctx)
        outer.addWidget(self.rules_tab, 1)

    def on_show(self, **kwargs) -> None:
        self.rules_tab.on_show()

    def _open_templates(self) -> None:
        TemplatesDialog(self.ctx, parent=self).exec()

    def _open_dry_run(self) -> None:
        RuleDryRunDialog(self.ctx, self.rules_tab.selected_bot_id, parent=self).exec()
