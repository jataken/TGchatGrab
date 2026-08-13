from __future__ import annotations

import json

from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QTabWidget,
    QVBoxLayout, QWidget,
)

from ...context import AppContext
from ...widgets import button, card, muted
from .common import populate_bot_picker
from ...util import fire
from ....bots.rules_engine import IncomingEvent, RulesEngine


class TestModeTab(QWidget):
    """Dry-run: shows what a rule or scenario *would* do, without sending
    anything or touching leads/contacts — matches the spec's "прогон
    правила без реальной отправки, с показом, что бот сделал бы"."""

    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.rules = RulesEngine(ctx.db)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 16, 0, 16)

        top_row = QHBoxLayout()
        top_row.addWidget(muted("Бот"))
        self.bot_picker = QComboBox()
        top_row.addWidget(self.bot_picker, 1)
        outer.addLayout(top_row)
        outer.addSpacing(10)

        inner_tabs = QTabWidget()
        outer.addWidget(inner_tabs, 1)

        # ---- rule dry run ----
        rule_page = QWidget()
        rule_lay = QVBoxLayout(rule_page)
        rule_lay.addWidget(muted("Введите сообщение, как будто его написал контакт — покажем, "
                                  "какие триггеры сработают и что бы сделали действия."))
        self.rule_input = QLineEdit()
        self.rule_input.setPlaceholderText("Текст входящего сообщения")
        rule_lay.addWidget(self.rule_input)
        run_rule_btn = button("Прогнать", "primary")
        run_rule_btn.clicked.connect(self._on_run_rule)
        rule_lay.addWidget(run_rule_btn)
        self.rule_output = QPlainTextEdit()
        self.rule_output.setReadOnly(True)
        rule_lay.addWidget(self.rule_output, 1)
        inner_tabs.addTab(rule_page, "Правило")

        # ---- scenario dry run ----
        scenario_page = QWidget()
        scenario_lay = QVBoxLayout(scenario_page)
        scenario_lay.addWidget(muted("Выберите сценарий и впишите пробные ответы (по одному на строку) — "
                                      "покажем вопросы и куда лягут ответы, ничего не отправляя и не сохраняя."))
        self.scenario_combo = QComboBox()
        scenario_lay.addWidget(self.scenario_combo)
        self.answers_input = QPlainTextEdit()
        self.answers_input.setPlaceholderText("Acme Corp\n500000 руб\nИван, снабжение")
        self.answers_input.setMaximumHeight(100)
        scenario_lay.addWidget(self.answers_input)
        run_scenario_btn = button("Прогнать сценарий", "primary")
        run_scenario_btn.clicked.connect(self._on_run_scenario)
        scenario_lay.addWidget(run_scenario_btn)
        self.scenario_output = QPlainTextEdit()
        self.scenario_output.setReadOnly(True)
        scenario_lay.addWidget(self.scenario_output, 1)
        inner_tabs.addTab(scenario_page, "Сценарий")

        self.bot_picker.currentIndexChanged.connect(self._reload_scenarios)

    def on_show(self) -> None:
        populate_bot_picker(self.ctx, self.bot_picker)
        self._reload_scenarios()

    def _reload_scenarios(self) -> None:
        self.scenario_combo.clear()
        bot_id = self.bot_picker.currentData()
        if bot_id is None:
            return
        for sc in self.ctx.db.list_scenarios(bot_id):
            self.scenario_combo.addItem(sc["name"], sc["id"])

    def _on_run_rule(self) -> None:
        bot_id = self.bot_picker.currentData()
        if bot_id is None:
            self.rule_output.setPlainText("Сначала выберите бота.")
            return
        text = self.rule_input.text()
        event = IncomingEvent(contact_telegram_id=0, username="тест", text=text, chat_type="dm")
        triggers = self.rules.triggers_for(bot_id, event)
        if not triggers:
            self.rule_output.setPlainText("Ни один триггер не сработал бы на это сообщение.")
            return
        lines = []
        for trig in triggers:
            lines.append(f"Триггер «{trig['type']}» сработал бы. Действия:")
            for action in self.ctx.db.list_actions(trig["id"]):
                cfg = json.loads(action["config"])
                lines.append(f"  → {action['type']}: {cfg}")
        self.rule_output.setPlainText("\n".join(lines))

    def _on_run_scenario(self) -> None:
        scenario_id = self.scenario_combo.currentData()
        if scenario_id is None:
            self.scenario_output.setPlainText("Сначала выберите сценарий.")
            return
        answers = [a for a in self.answers_input.toPlainText().split("\n") if a.strip()]
        trail = self.rules.scenarios.dry_run(scenario_id, answers)
        lines = []
        for step in trail:
            if "final_answers" in step:
                lines.append(f"\nИтоговые ответы: {step['final_answers']}")
            else:
                line = f"«{step['question']}» → поле {step['field']}"
                if "answer" in step:
                    line += f" = {step['answer']!r}"
                    if step.get("error"):
                        line += f"  [ошибка: {step['error']}]"
                lines.append(line)
        self.scenario_output.setPlainText("\n".join(lines))
