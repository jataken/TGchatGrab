from __future__ import annotations

import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QInputDialog, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QSplitter, QVBoxLayout, QWidget,
)

from ...context import AppContext
from ...widgets import button, card, muted
from .common import populate_bot_picker

_VALIDATIONS = [("text", "Любой текст"), ("number", "Число"), ("phone", "Телефон")]


class ScenariosTab(QWidget):
    """Pошаговый диалог: список шагов (вопрос, поле для ответа, тип
    проверки). Порядок в списке = порядок вопросов; next — это просто
    "следующий элемент списка", ScenarioEngine идёт по нему линейно."""

    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.selected_bot_id: int | None = None
        self.selected_scenario_id: int | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 16, 0, 16)

        top_row = QHBoxLayout()
        top_row.addWidget(muted("Бот"))
        self.bot_picker = QComboBox()
        self.bot_picker.currentIndexChanged.connect(self._on_bot_changed)
        top_row.addWidget(self.bot_picker, 1)
        outer.addLayout(top_row)
        outer.addSpacing(10)

        split = QSplitter()
        outer.addWidget(split, 1)

        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 8, 0)
        left_lay.addWidget(muted("Сценарии"))
        self.scenario_list = QListWidget()
        self.scenario_list.currentItemChanged.connect(self._on_scenario_selected)
        left_lay.addWidget(self.scenario_list, 1)
        add_btn = button("＋ Новый сценарий", "secondary")
        add_btn.clicked.connect(self._on_add_scenario)
        left_lay.addWidget(add_btn)
        delete_btn = button("Удалить сценарий", "ghost")
        delete_btn.clicked.connect(self._on_delete_scenario)
        left_lay.addWidget(delete_btn)
        split.addWidget(left)

        right = card()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(14, 14, 14, 14)
        right_lay.addWidget(muted("Шаги (по порядку)"))
        self.step_list = QListWidget()
        right_lay.addWidget(self.step_list, 1)

        form_row = QHBoxLayout()
        self.question_input = QLineEdit()
        self.question_input.setPlaceholderText("Текст вопроса")
        form_row.addWidget(self.question_input, 2)
        self.field_input = QLineEdit()
        self.field_input.setPlaceholderText("поле (например, company)")
        form_row.addWidget(self.field_input, 1)
        self.validation_combo = QComboBox()
        for key, label in _VALIDATIONS:
            self.validation_combo.addItem(label, key)
        form_row.addWidget(self.validation_combo)
        right_lay.addLayout(form_row)

        btn_row = QHBoxLayout()
        add_step_btn = button("＋ Добавить шаг", "secondary")
        add_step_btn.clicked.connect(self._on_add_step)
        btn_row.addWidget(add_step_btn)
        remove_step_btn = button("Удалить выбранный шаг", "ghost")
        remove_step_btn.clicked.connect(self._on_remove_step)
        btn_row.addWidget(remove_step_btn)
        move_up_btn = button("↑", "ghost")
        move_up_btn.clicked.connect(lambda: self._move_step(-1))
        btn_row.addWidget(move_up_btn)
        move_down_btn = button("↓", "ghost")
        move_down_btn.clicked.connect(lambda: self._move_step(1))
        btn_row.addWidget(move_down_btn)
        btn_row.addStretch(1)
        right_lay.addLayout(btn_row)

        split.addWidget(right)
        split.setSizes([260, 560])

    def on_show(self) -> None:
        populate_bot_picker(self.ctx, self.bot_picker)
        self.selected_bot_id = self.bot_picker.currentData()
        self._reload_scenarios()

    def _on_bot_changed(self, _index: int) -> None:
        self.selected_bot_id = self.bot_picker.currentData()
        self._reload_scenarios()

    def _reload_scenarios(self) -> None:
        self.scenario_list.clear()
        self.selected_scenario_id = None
        self.step_list.clear()
        if self.selected_bot_id is None:
            return
        for sc in self.ctx.db.list_scenarios(self.selected_bot_id):
            item = QListWidgetItem(sc["name"])
            item.setData(Qt.UserRole, sc["id"])
            self.scenario_list.addItem(item)

    def _on_scenario_selected(self, current: QListWidgetItem, _prev) -> None:
        self.selected_scenario_id = current.data(Qt.UserRole) if current else None
        self._reload_steps()

    def _reload_steps(self) -> None:
        self.step_list.clear()
        if self.selected_scenario_id is None:
            return
        scenario = self.ctx.db.get_scenario(self.selected_scenario_id)
        steps = json.loads(scenario["steps"]) if scenario else []
        for step in steps:
            self.step_list.addItem(f"{step['question']}  →  {step['field']} ({step.get('validation', 'text')})")

    def _get_steps(self) -> list[dict]:
        scenario = self.ctx.db.get_scenario(self.selected_scenario_id)
        return json.loads(scenario["steps"]) if scenario else []

    def _save_steps(self, steps: list[dict]) -> None:
        self.ctx.db.update_scenario(self.selected_scenario_id, steps=steps)
        self._reload_steps()

    def _on_add_scenario(self) -> None:
        if self.selected_bot_id is None:
            QMessageBox.information(self, "Выберите бота", "Сначала выберите бота вверху экрана.")
            return
        name, ok = QInputDialog.getText(self, "Новый сценарий", "Название:")
        if not ok or not name.strip():
            return
        sc_id = self.ctx.db.add_scenario(self.selected_bot_id, name.strip(), [])
        self._reload_scenarios()
        for i in range(self.scenario_list.count()):
            if self.scenario_list.item(i).data(Qt.UserRole) == sc_id:
                self.scenario_list.setCurrentRow(i)
                break

    def _on_delete_scenario(self) -> None:
        if self.selected_scenario_id is None:
            return
        self.ctx.db.delete_scenario(self.selected_scenario_id)
        self._reload_scenarios()

    def _on_add_step(self) -> None:
        if self.selected_scenario_id is None:
            QMessageBox.information(self, "Выберите сценарий", "Сначала выберите или создайте сценарий слева.")
            return
        question = self.question_input.text().strip()
        field = self.field_input.text().strip()
        if not question or not field:
            QMessageBox.warning(self, "Не хватает данных", "Укажите текст вопроса и имя поля для ответа.")
            return
        steps = self._get_steps()
        steps.append({"question": question, "field": field, "validation": self.validation_combo.currentData()})
        self._save_steps(steps)
        self.question_input.clear()
        self.field_input.clear()

    def _on_remove_step(self) -> None:
        row = self.step_list.currentRow()
        if row < 0:
            return
        steps = self._get_steps()
        del steps[row]
        self._save_steps(steps)

    def _move_step(self, delta: int) -> None:
        row = self.step_list.currentRow()
        if row < 0:
            return
        steps = self._get_steps()
        new_row = row + delta
        if not (0 <= new_row < len(steps)):
            return
        steps[row], steps[new_row] = steps[new_row], steps[row]
        self._save_steps(steps)
        self.step_list.setCurrentRow(new_row)
