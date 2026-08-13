from __future__ import annotations

import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QFrame, QHBoxLayout, QInputDialog, QLineEdit,
    QMessageBox, QPlainTextEdit, QScrollArea, QVBoxLayout, QWidget,
)

from ...context import AppContext
from ...widgets import button, card, chip, label, muted, plural as _plural
from ....bots.rules_engine import RulesEngine

_VALIDATIONS = [("text", "Любой"), ("phone", "Телефон"), ("number", "Число")]
_VALIDATION_LABELS = dict(_VALIDATIONS)
_VALIDATION_HINTS = {
    "text": "Подойдёт любой непустой ответ. Пустое сообщение бот переспросит.",
    "phone": "Если в ответе не найдётся номера, бот вежливо переспросит и не пойдёт дальше.",
    "number": "Бот примет только число — удобно для объёмов и количества.",
}


class StepRow(QFrame):
    def __init__(self, index: int, step: dict, selected: bool, on_select, on_move, can_move_down: bool):
        super().__init__()
        self.index = index
        color = "rgba(145,132,217,46)" if selected else "rgba(233,233,237,8)"
        ring = "#5d5294" if selected else "#33354a"
        # Scoped by object name: QLabel is a QFrame subclass, so an
        # unscoped QFrame rule would repaint every child label's box too.
        self.setObjectName("stepRow")
        self.setStyleSheet(
            f"QFrame#stepRow {{ background: {color}; border-radius: 10px; "
            f"border: 1px solid {ring}; }}"
        )
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 10, 10)
        lay.setSpacing(12)

        num = label(str(index + 1))
        num.setFixedSize(22, 22)
        num.setAlignment(Qt.AlignCenter)
        num_bg = "#9184d9" if selected else "rgba(233,233,237,10)"
        num_fg = "#161826" if selected else "#9a9aa3"
        num.setStyleSheet(f"background: {num_bg}; color: {num_fg}; border-radius: 6px; font-size: 11.5px;")
        lay.addWidget(num, alignment=Qt.AlignTop)

        text_col = QVBoxLayout()
        text_col.setSpacing(3)
        q = step.get("question", "")
        q_label = label(q or "(вопрос пустой)")
        q_label.setWordWrap(True)
        q_label.setStyleSheet("font-size: 13.5px;")
        text_col.addWidget(q_label)
        detail = label(
            f"ответ сохранится как {step.get('field', '')} · "
            f"{_VALIDATION_LABELS.get(step.get('validation', 'text'), step.get('validation', 'text'))}"
        )
        detail.setStyleSheet("color: #6c6c78; font-size: 11.5px;")
        text_col.addWidget(detail)
        lay.addLayout(text_col, 1)

        if not q.strip():
            warn = label("вопрос пустой")
            warn.setStyleSheet(
                "background: rgba(220,150,90,46); color: #f0c6a0; border-radius: 6px; "
                "padding: 3px 9px; font-size: 11.5px;"
            )
            lay.addWidget(warn, alignment=Qt.AlignTop)

        move_col = QVBoxLayout()
        move_col.setSpacing(3)
        for arrow, delta, enabled in (("↑", -1, index > 0), ("↓", 1, can_move_down)):
            btn = button(arrow, "secondary")
            btn.setFixedSize(28, 24)
            btn.setEnabled(enabled)
            btn.clicked.connect(lambda _c, d=delta: on_move(index, d))
            move_col.addWidget(btn)
        lay.addLayout(move_col)

        self._on_select = on_select

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._on_select(self.index)


class ScenarioScreen(QWidget):
    """Сценарий — a real linear step editor (question → field → validation)
    with a side properties panel and an inline test-run panel, replacing
    the old «Сценарии» + «Тест» tabs. The engine only executes steps in
    order — no branching canvas is offered here, only the note the
    redesign asked for: the branching canvas is a stated future, not a
    working feature, so it isn't mocked up as one."""

    def __init__(self, ctx: AppContext, navigate):
        super().__init__()
        self.ctx = ctx
        self.navigate = navigate
        self.selected_bot_id: int | None = None
        self.selected_scenario_id: int | None = None
        self.step_sel = 0
        self.test_open = False
        self.rules = RulesEngine(ctx.db)
        ctx.bot_selection.changed.connect(self._on_bot_changed)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 24, 40, 22)
        outer.setSpacing(0)

        # The mode badge is the design's answer to "незамеченный режим —
        # источник самых дорогих ошибок": edits here save themselves, and
        # that has to be visible where the eye already is.
        mode_row = QHBoxLayout()
        mode_row.setSpacing(10)
        self.mode_badge = label("●  Черновик · правки сохраняются сами")
        self.mode_badge.setStyleSheet(
            "background: rgba(145,132,217,36); color: #d2cefd; border: 1px solid rgba(145,132,217,102); "
            "border-radius: 8px; padding: 5px 12px; font-size: 12.5px;"
        )
        mode_row.addWidget(self.mode_badge)
        mode_row.addStretch(1)
        self.saved_label = muted("")
        mode_row.addWidget(self.saved_label)
        outer.addLayout(mode_row)
        outer.addSpacing(12)

        top_row = QHBoxLayout()
        top_row.addWidget(muted("Сценарий"))
        self.scenario_picker = QComboBox()
        self.scenario_picker.currentIndexChanged.connect(self._on_scenario_changed)
        top_row.addWidget(self.scenario_picker, 1)
        add_btn = button("＋ Новый", "secondary")
        add_btn.clicked.connect(self._on_add_scenario)
        top_row.addWidget(add_btn)
        del_btn = button("Удалить сценарий", "ghost")
        del_btn.clicked.connect(self._on_delete_scenario)
        top_row.addWidget(del_btn)
        outer.addLayout(top_row)
        outer.addSpacing(6)

        note_row = QHBoxLayout()
        note_row.setSpacing(14)
        note_label = muted(
            "Шаги идут строго по порядку, без развилок — так это работает в движке. "
            "Когда контакт ответит на последний вопрос, ответы станут заявкой и уйдут менеджеру."
        )
        note_label.setWordWrap(True)
        note_row.addWidget(note_label, 1)
        self.test_btn = button("Прогнать сценарий", "secondary")
        self.test_btn.clicked.connect(self._toggle_test)
        note_row.addWidget(self.test_btn, alignment=Qt.AlignTop)
        outer.addLayout(note_row)
        outer.addSpacing(12)

        body = QHBoxLayout()
        body.setSpacing(16)
        outer.addLayout(body, 1)

        left_col = QVBoxLayout()
        left_col.setSpacing(10)
        self.steps_scroll = QScrollArea()
        self.steps_scroll.setWidgetResizable(True)
        self.steps_host = QWidget()
        self.steps_lay = QVBoxLayout(self.steps_host)
        self.steps_lay.setSpacing(6)
        self.steps_lay.addStretch(1)
        self.steps_scroll.setWidget(self.steps_host)
        left_col.addWidget(self.steps_scroll, 1)

        self.add_step_btn = button("＋ Добавить вопрос", "ghost")
        self.add_step_btn.clicked.connect(self._on_add_step)
        left_col.addWidget(self.add_step_btn)

        self.test_panel = card()
        test_lay = QVBoxLayout(self.test_panel)
        test_lay.setContentsMargins(14, 12, 14, 12)
        test_head = QHBoxLayout()
        test_head.addWidget(label("ТЕСТОВЫЙ ПРОГОН", "kicker"))
        test_head.addWidget(muted("ничего не отправляется, заявка не создаётся"))
        test_lay.addLayout(test_head)
        test_lay.addWidget(muted("Пробные ответы по одному на строку, по порядку шагов:"))
        self.test_answers = QPlainTextEdit()
        self.test_answers.setPlaceholderText("Acme Corp\n500000 руб\nИван, снабжение")
        self.test_answers.setMaximumHeight(80)
        test_lay.addWidget(self.test_answers)
        run_test_btn = button("Прогнать", "primary")
        run_test_btn.clicked.connect(self._on_run_test)
        test_lay.addWidget(run_test_btn)
        self.test_output = QPlainTextEdit()
        self.test_output.setReadOnly(True)
        self.test_output.setMaximumHeight(140)
        test_lay.addWidget(self.test_output)
        self.test_panel.setVisible(False)
        left_col.addWidget(self.test_panel)

        # ---- completion message + funnel ----
        tail = card()
        tail_lay = QVBoxLayout(tail)
        tail_lay.setContentsMargins(16, 12, 16, 14)
        tail_lay.setSpacing(8)
        done_row = QHBoxLayout()
        done_row.addWidget(muted("Что отправить, когда контакт ответил на всё"))
        self.done_template_combo = QComboBox()
        self.done_template_combo.currentIndexChanged.connect(self._on_done_template_changed)
        done_row.addWidget(self.done_template_combo, 1)
        tail_lay.addLayout(done_row)

        tail_lay.addWidget(label("ГДЕ КОНТАКТЫ ОТВАЛИВАЮТСЯ", "kicker"))
        self.funnel_lay = QVBoxLayout()
        self.funnel_lay.setSpacing(4)
        tail_lay.addLayout(self.funnel_lay)
        self.funnel_empty = muted("Пока никто не проходил этот сценарий — воронка появится после первых диалогов.")
        self.funnel_empty.setWordWrap(True)
        tail_lay.addWidget(self.funnel_empty)
        self._funnel_rows: list[QWidget] = []
        left_col.addWidget(tail)

        body.addLayout(left_col, 60)

        self.props_panel = card()
        props_lay = QVBoxLayout(self.props_panel)
        props_lay.setContentsMargins(16, 16, 16, 16)
        props_lay.setSpacing(12)
        self.props_kicker = label("", "kicker")
        props_lay.addWidget(self.props_kicker)
        props_lay.addWidget(muted("Что бот спросит"))
        self.question_input = QLineEdit()
        self.question_input.editingFinished.connect(self._on_props_changed)
        props_lay.addWidget(self.question_input)
        props_lay.addWidget(muted("Имя поля в заявке"))
        self.field_input = QLineEdit()
        self.field_input.editingFinished.connect(self._on_props_changed)
        props_lay.addWidget(self.field_input)
        props_lay.addWidget(muted("Проверка ответа"))
        val_row = QHBoxLayout()
        self.validation_group = QButtonGroup(self)
        self.validation_group.setExclusive(True)
        self.validation_buttons: dict[str, object] = {}
        for key, lbl in _VALIDATIONS:
            btn = chip(lbl)
            btn.clicked.connect(lambda _c, k=key: self._on_validation_pick(k))
            self.validation_group.addButton(btn)
            val_row.addWidget(btn)
            self.validation_buttons[key] = btn
        props_lay.addLayout(val_row)
        self.validation_hint = muted("")
        self.validation_hint.setWordWrap(True)
        props_lay.addWidget(self.validation_hint)

        props_lay.addSpacing(6)
        props_lay.addWidget(muted("Так это увидит контакт:"))
        self.preview_bubble = label("")
        self.preview_bubble.setWordWrap(True)
        self.preview_bubble.setStyleSheet(
            "background: #12141d; border-radius: 10px; padding: 9px 12px; font-size: 13px;"
        )
        props_lay.addWidget(self.preview_bubble)
        props_lay.addStretch(1)
        self.remove_step_btn = button("Удалить шаг", "danger")
        self.remove_step_btn.clicked.connect(self._on_remove_step)
        props_lay.addWidget(self.remove_step_btn)
        self.props_panel.setFixedWidth(320)
        body.addWidget(self.props_panel, 0)

        self.empty_hint = muted("В этом сценарии пока нет вопросов — добавьте первый.")
        left_col.addWidget(self.empty_hint)

    def on_show(self, **kwargs) -> None:
        self.selected_bot_id = self.ctx.bot_selection.current
        self._reload_scenarios()

    def _on_bot_changed(self, bot_id) -> None:
        self.selected_bot_id = bot_id
        self._reload_scenarios()

    def _reload_scenarios(self) -> None:
        current = self.scenario_picker.currentData()
        self.scenario_picker.blockSignals(True)
        self.scenario_picker.clear()
        if self.selected_bot_id is not None:
            for sc in self.ctx.db.list_scenarios(self.selected_bot_id):
                self.scenario_picker.addItem(sc["name"], sc["id"])
        idx = self.scenario_picker.findData(current)
        self.scenario_picker.setCurrentIndex(idx if idx >= 0 else 0)
        self.scenario_picker.blockSignals(False)
        self.selected_scenario_id = self.scenario_picker.currentData()
        self.step_sel = 0
        self._reload_done_template()
        self._reload_steps()

    def _on_scenario_changed(self, _index: int) -> None:
        self.selected_scenario_id = self.scenario_picker.currentData()
        self.step_sel = 0
        self._reload_done_template()
        self._reload_steps()

    def _steps(self) -> list[dict]:
        if self.selected_scenario_id is None:
            return []
        scenario = self.ctx.db.get_scenario(self.selected_scenario_id)
        return json.loads(scenario["steps"]) if scenario else []

    def _save_steps(self, steps: list[dict]) -> None:
        self.ctx.db.update_scenario(self.selected_scenario_id, steps=steps)
        self.saved_label.setText("сохранено только что")
        self._reload_steps()

    def _reload_steps(self) -> None:
        while self.steps_lay.count() > 1:
            item = self.steps_lay.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        steps = self._steps()
        has_scenario = self.selected_scenario_id is not None
        self.steps_scroll.setVisible(has_scenario and bool(steps))
        self.add_step_btn.setVisible(has_scenario)
        self.empty_hint.setVisible(has_scenario and not steps)
        self.props_panel.setVisible(has_scenario and bool(steps))
        if not steps:
            return

        self.step_sel = max(0, min(self.step_sel, len(steps) - 1))
        for i, step in enumerate(steps):
            row = StepRow(i, step, i == self.step_sel, self._select_step, self._move_step, i < len(steps) - 1)
            self.steps_lay.insertWidget(i, row)

        self._sync_props()
        self._reload_funnel()

    def _reload_funnel(self) -> None:
        """Where contacts stop answering — the question the design brief
        asks of scenario analytics, computed from accumulated session
        history rather than a message counter."""
        for w in self._funnel_rows:
            w.setParent(None)
            w.deleteLater()
        self._funnel_rows.clear()

        funnel = self.ctx.db.scenario_funnel(self.selected_scenario_id) \
            if self.selected_scenario_id is not None else []
        has_data = any(row["reached"] for row in funnel)
        self.funnel_empty.setVisible(not has_data)
        if not has_data:
            return

        top = funnel[0]["reached"] or 1
        for row in funnel:
            host = QWidget()
            lay = QHBoxLayout(host)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(10)

            num = label(str(row["index"] + 1))
            num.setFixedWidth(16)
            num.setStyleSheet("color: #6c6c78; font-size: 11.5px;")
            lay.addWidget(num)

            bar = QWidget()
            share = row["reached"] / top
            bar.setFixedHeight(8)
            bar.setMinimumWidth(max(4, int(220 * share)))
            bar.setMaximumWidth(max(4, int(220 * share)))
            bar.setStyleSheet("background: #9184d9; border-radius: 4px;")
            lay.addWidget(bar)

            caption = muted(
                f"{row['reached']} дошли"
                + (f" · {row['dropped']} остановились здесь" if row["dropped"] else "")
                + f" — {row['field']}"
            )
            lay.addWidget(caption, 1)
            self.funnel_lay.addWidget(host)
            self._funnel_rows.append(host)

    def _reload_done_template(self) -> None:
        self.done_template_combo.blockSignals(True)
        self.done_template_combo.clear()
        self.done_template_combo.addItem("ничего не отправлять", None)
        if self.selected_bot_id is not None:
            for tpl in self.ctx.db.list_templates(self.selected_bot_id):
                self.done_template_combo.addItem(tpl["name"], tpl["id"])
        scenario = self.ctx.db.get_scenario(self.selected_scenario_id) \
            if self.selected_scenario_id is not None else None
        current = scenario["done_template_id"] if scenario else None
        idx = self.done_template_combo.findData(current)
        self.done_template_combo.setCurrentIndex(max(0, idx))
        self.done_template_combo.blockSignals(False)

    def _on_done_template_changed(self, _index: int) -> None:
        if self.selected_scenario_id is None:
            return
        self.ctx.db.update_scenario(
            self.selected_scenario_id,
            done_template_id=self.done_template_combo.currentData(),
        )
        self.saved_label.setText("сохранено только что")

    def _select_step(self, index: int) -> None:
        self.step_sel = index
        self._reload_steps()

    def _move_step(self, index: int, delta: int) -> None:
        steps = self._steps()
        new_index = index + delta
        if not (0 <= new_index < len(steps)):
            return
        steps[index], steps[new_index] = steps[new_index], steps[index]
        self.step_sel = new_index
        self._save_steps(steps)

    def _sync_props(self) -> None:
        steps = self._steps()
        if not steps:
            return
        step = steps[self.step_sel]
        self.props_kicker.setText(f"ШАГ {self.step_sel + 1} ИЗ {len(steps)}")
        self.question_input.blockSignals(True)
        self.question_input.setText(step.get("question", ""))
        self.question_input.blockSignals(False)
        self.field_input.blockSignals(True)
        self.field_input.setText(step.get("field", ""))
        self.field_input.blockSignals(False)
        validation = step.get("validation", "text")
        btn = self.validation_buttons.get(validation)
        if btn is not None:
            btn.setChecked(True)
        self.validation_hint.setText(_VALIDATION_HINTS.get(validation, ""))
        self.preview_bubble.setText(step.get("question", "") or "(вопрос пока пустой)")

    def _on_props_changed(self) -> None:
        steps = self._steps()
        if not steps:
            return
        steps[self.step_sel]["question"] = self.question_input.text().strip()
        steps[self.step_sel]["field"] = self.field_input.text().strip() or f"field_{self.step_sel + 1}"
        self._save_steps(steps)

    def _on_validation_pick(self, key: str) -> None:
        steps = self._steps()
        if not steps:
            return
        steps[self.step_sel]["validation"] = key
        self._save_steps(steps)

    def _on_add_step(self) -> None:
        if self.selected_scenario_id is None:
            QMessageBox.information(self, "Выберите сценарий", "Сначала создайте сценарий вверху экрана.")
            return
        steps = self._steps()
        steps.append({"question": "", "field": f"field_{len(steps) + 1}", "validation": "text"})
        self.step_sel = len(steps) - 1
        self._save_steps(steps)
        self.question_input.setFocus()

    def _on_remove_step(self) -> None:
        steps = self._steps()
        if not steps:
            return
        del steps[self.step_sel]
        self.step_sel = max(0, self.step_sel - 1)
        self._save_steps(steps)

    def _on_add_scenario(self) -> None:
        if self.selected_bot_id is None:
            QMessageBox.information(self, "Выберите бота", "Сначала выберите бота вверху экрана.")
            return
        name, ok = QInputDialog.getText(self, "Новый сценарий", "Название:")
        if not ok or not name.strip():
            return
        sc_id = self.ctx.db.add_scenario(self.selected_bot_id, name.strip(), [])
        self._reload_scenarios()
        idx = self.scenario_picker.findData(sc_id)
        if idx >= 0:
            self.scenario_picker.setCurrentIndex(idx)

    def _on_delete_scenario(self) -> None:
        if self.selected_scenario_id is None:
            return
        scenario = self.ctx.db.get_scenario(self.selected_scenario_id)
        usage = self.ctx.db.scenario_usage(self.selected_scenario_id)
        n_steps = len(self._steps())

        text = f"Удалить «{scenario['name'] if scenario else 'сценарий'}» " \
               f"вместе с {n_steps} " + _plural(n_steps, "вопросом", "вопросами", "вопросами") + "?"
        consequences = []
        if usage["actions"]:
            consequences.append(
                f"{usage['actions']} " + _plural(usage["actions"], "правило", "правила", "правил") +
                " перестанет запускать этот сценарий")
        if usage["active_dialogs"]:
            consequences.append(
                f"{usage['active_dialogs']} " +
                _plural(usage["active_dialogs"], "контакт сейчас проходит", "контакта сейчас проходят",
                        "контактов сейчас проходят") + " его — их незаконченные ответы пропадут")
        if consequences:
            text += "\n\n" + ";\n".join(consequences) + "."
        text += "\n\nОтменить это нельзя."

        if QMessageBox.question(self, "Удалить сценарий", text) != QMessageBox.Yes:
            return
        self.ctx.db.delete_scenario(self.selected_scenario_id)
        self._reload_scenarios()

    def _toggle_test(self) -> None:
        self.test_open = not self.test_open
        self.test_panel.setVisible(self.test_open)
        self.test_btn.setText("Выйти из теста" if self.test_open else "Прогнать сценарий")
        if self.test_open:
            self.mode_badge.setText("●  Тестовый прогон — ничего не отправляется")
            self.mode_badge.setStyleSheet(
                "background: rgba(220,150,90,40); color: #f0c6a0; "
                "border: 1px solid rgba(240,198,160,115); border-radius: 8px; "
                "padding: 5px 12px; font-size: 12.5px;"
            )
        else:
            self.mode_badge.setText("●  Черновик · правки сохраняются сами")
            self.mode_badge.setStyleSheet(
                "background: rgba(145,132,217,36); color: #d2cefd; "
                "border: 1px solid rgba(145,132,217,102); border-radius: 8px; "
                "padding: 5px 12px; font-size: 12.5px;"
            )

    def _on_run_test(self) -> None:
        if self.selected_scenario_id is None:
            self.test_output.setPlainText("Сначала выберите сценарий.")
            return
        answers = [a for a in self.test_answers.toPlainText().split("\n") if a.strip()]
        trail = self.rules.scenarios.dry_run(self.selected_scenario_id, answers)
        lines = []
        for step in trail:
            if "final_answers" in step:
                lines.append(f"\nЗаявка вышла бы такой: {step['final_answers']}")
            else:
                line = f"«{step['question']}» → поле {step['field']}"
                if "answer" in step:
                    line += f" = {step['answer']!r}"
                    if step.get("error"):
                        line += f"  [ошибка: {step['error']}]"
                lines.append(line)
        self.test_output.setPlainText("\n".join(lines))
