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
from ....bots.scenario_engine import BRANCHING, END, LINEAR, format_question, options_of
from ....bots.templating import render
from ....core import lead as lead_domain

_VALIDATIONS = [("text", "Любой"), ("phone", "Телефон"), ("number", "Число")]
_VALIDATION_LABELS = dict(_VALIDATIONS)
_VALIDATION_HINTS = {
    "text": "Подойдёт любой непустой ответ. Пустое сообщение бот переспросит.",
    "phone": "Если в ответе не найдётся номера, бот вежливо переспросит и не пойдёт дальше.",
    "number": "Бот примет только число — удобно для объёмов и количества.",
}


def _branch_summary(step: dict, steps: list[dict]) -> str:
    """Куда ведёт шаг — словами, а не идентификаторами.

    Автор сценария думает «после этого спросим про тираж», а не «s4».
    Список шагов на экране должен показывать ровно это, иначе развилку
    приходится держать в голове.
    """
    numbers = {s.get("id"): i + 1 for i, s in enumerate(steps)}

    def where(target):
        if target == END:
            return "конец"
        if target and target in numbers:
            return f"шаг {numbers[target]}"
        if target:
            return "шаг удалён"
        return None

    options = options_of(step)
    if options:
        return " · ".join(
            f"«{o['label']}» → {where(o.get('next')) or 'дальше'}" for o in options)
    target = where(step.get("next"))
    return f"дальше → {target}" if target else ""


class StepRow(QFrame):
    def __init__(self, index: int, step: dict, selected: bool, on_select, on_move, can_move_down: bool,
                 branch_text: str = ""):
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
        if branch_text:
            branch = label(branch_text)
            branch.setWordWrap(True)
            branch.setStyleSheet("color: #b5abfc; font-size: 11.5px;")
            text_col.addWidget(branch)
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
    """Редактор сценария: вопрос → поле → проверка, боковая панель
    свойств, тестовый прогон.

    Вид сценария («Пошаговый» / «С развилками») — свойство самого
    сценария, а не режим экрана. Так у одного бота могут одновременно
    жить простая анкета и разговор с развилками, и переключение вида не
    переписывает уже настроенное: переходы сохраняются в тех же шагах и
    просто не читаются, пока сценарий пошаговый.

    Развилка редактируется списком, а не полотном со стрелками: путь
    контакта здесь всегда один сверху вниз, и список показывает его
    ровно так же, как контакт его увидит."""

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

        # Вид сценария — свойство сценария, а не приложения: под разные
        # задачи планируются разные боты, и пошаговая анкета должна
        # оставаться пошаговой, когда рядом появилась ветвящаяся.
        kind_row = QHBoxLayout()
        kind_row.setSpacing(8)
        kind_row.addWidget(muted("Вид"))
        self.kind_group = QButtonGroup(self)
        self.kind_group.setExclusive(True)
        self.kind_buttons: dict[str, object] = {}
        for key, lbl in ((LINEAR, "Пошаговый"), (BRANCHING, "С развилками")):
            btn = chip(lbl)
            btn.clicked.connect(lambda _c, k=key: self._on_kind_pick(k))
            self.kind_group.addButton(btn)
            kind_row.addWidget(btn)
            self.kind_buttons[key] = btn
        kind_row.addStretch(1)
        outer.addLayout(kind_row)
        outer.addSpacing(6)

        note_row = QHBoxLayout()
        note_row.setSpacing(14)
        self.note_label = muted("")
        note_label = self.note_label
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

        # Панель прокручивается: число вариантов задаёт пользователь, и на
        # пятом переход последнего варианта просто обрезался снизу.
        self.props_panel = card()
        panel_lay = QVBoxLayout(self.props_panel)
        panel_lay.setContentsMargins(0, 0, 0, 0)
        props_scroll = QScrollArea()
        props_scroll.setWidgetResizable(True)
        props_scroll.setFrameShape(QFrame.NoFrame)
        props_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # Селекторы обязательны: таблица стилей без них применяется и ко
        # всем детям, а «прозрачный фон» у полей ввода — это поля без фона.
        props_scroll.setObjectName("propsScroll")
        props_scroll.setStyleSheet(
            "QScrollArea#propsScroll { background: transparent; border: none; }")
        props_inner = QWidget()
        props_inner.setObjectName("propsInner")
        props_inner.setStyleSheet("QWidget#propsInner { background: transparent; }")
        props_scroll.setWidget(props_inner)
        panel_lay.addWidget(props_scroll)
        props_lay = QVBoxLayout(props_inner)
        # Правый отступ побольше: полоса прокрутки забирает ширину изнутри,
        # и без него она наезжала на подписи.
        props_lay.setContentsMargins(16, 16, 22, 16)
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
        props_lay.addWidget(muted("→ поле лида (необязательно)"))
        self.lead_field_combo = QComboBox()
        self.lead_field_combo.addItem("— не связано с лидом —", None)
        for key in lead_domain.SCENARIO_LEAD_FIELDS:
            self.lead_field_combo.addItem(lead_domain.SCENARIO_LEAD_FIELD_LABELS[key], key)
        self.lead_field_combo.currentIndexChanged.connect(self._on_props_changed)
        props_lay.addWidget(self.lead_field_combo)
        # Проверка ответа теряет смысл, когда у шага есть варианты:
        # выбирать можно только из предложенного.
        self.validation_box = QWidget()
        validation_lay = QVBoxLayout(self.validation_box)
        validation_lay.setContentsMargins(0, 0, 0, 0)
        validation_lay.setSpacing(6)
        validation_lay.addWidget(muted("Проверка ответа"))
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
        validation_lay.addLayout(val_row)
        self.validation_hint = muted("")
        self.validation_hint.setWordWrap(True)
        validation_lay.addWidget(self.validation_hint)
        props_lay.addWidget(self.validation_box)

        # ---- развилка ------------------------------------------------
        self.branch_box = QWidget()
        branch_lay = QVBoxLayout(self.branch_box)
        branch_lay.setContentsMargins(0, 0, 0, 0)
        branch_lay.setSpacing(6)
        branch_lay.addWidget(muted("Варианты ответа"))
        self.options_lay = QVBoxLayout()
        self.options_lay.setSpacing(10)
        branch_lay.addLayout(self.options_lay)
        add_opt_btn = button("＋ Вариант", "ghost")
        add_opt_btn.clicked.connect(self._on_add_option)
        branch_lay.addWidget(add_opt_btn)
        self.branch_hint = muted("")
        self.branch_hint.setWordWrap(True)
        branch_lay.addWidget(self.branch_hint)
        next_row = QHBoxLayout()
        next_row.addWidget(muted("Дальше"))
        self.next_combo = QComboBox()
        self.next_combo.currentIndexChanged.connect(self._on_next_changed)
        next_row.addWidget(self.next_combo, 1)
        branch_lay.addLayout(next_row)
        props_lay.addWidget(self.branch_box)
        self._option_rows: list[QWidget] = []

        props_lay.addSpacing(6)
        props_lay.addWidget(muted("Так это увидит контакт:"))
        self.preview_bubble = label("")
        self.preview_bubble.setWordWrap(True)
        self.preview_bubble.setStyleSheet(
            "background: #12141d; border-radius: 10px; padding: 9px 12px; font-size: 13px;"
        )
        props_lay.addWidget(self.preview_bubble)
        props_lay.addStretch(1)
        # Кнопка удаления — под прокруткой, а не внутри неё: это действие,
        # а не свойство, и искать его прокруткой незачем.
        self.remove_step_btn = button("Удалить шаг", "danger")
        self.remove_step_btn.clicked.connect(self._on_remove_step)
        del_wrap = QWidget()
        del_lay = QVBoxLayout(del_wrap)
        del_lay.setContentsMargins(16, 0, 16, 14)
        del_lay.addWidget(self.remove_step_btn)
        panel_lay.addWidget(del_wrap)
        self.props_panel.setFixedWidth(344)
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
                # setParent(None) до deleteLater: отложенное удаление
                # случится, когда цикл событий до него дойдёт, а до тех
                # пор старый шаг рисуется поверх нового.
                w.setParent(None)
                w.deleteLater()

        steps = self._steps()
        has_scenario = self.selected_scenario_id is not None
        self.steps_scroll.setVisible(has_scenario and bool(steps))
        self.add_step_btn.setVisible(has_scenario)
        self.empty_hint.setVisible(has_scenario and not steps)
        self.props_panel.setVisible(has_scenario and bool(steps))
        self._sync_kind()
        if not steps:
            self._clear_options()
            return

        branching = self._kind() == BRANCHING
        self.step_sel = max(0, min(self.step_sel, len(steps) - 1))
        for i, step in enumerate(steps):
            row = StepRow(i, step, i == self.step_sel, self._select_step, self._move_step,
                          i < len(steps) - 1,
                          branch_text=_branch_summary(step, steps) if branching else "")
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

    # ---- вид сценария: пошаговый / с развилками -----------------------
    def _kind(self) -> str:
        if self.selected_scenario_id is None:
            return LINEAR
        scenario = self.ctx.db.get_scenario(self.selected_scenario_id)
        if scenario is None:
            return LINEAR
        return (scenario["kind"] if "kind" in scenario.keys() else None) or LINEAR

    def _sync_kind(self) -> None:
        kind = self._kind()
        btn = self.kind_buttons.get(kind)
        if btn is not None:
            btn.setChecked(True)
        if kind == BRANCHING:
            self.note_label.setText(
                "У шага могут быть варианты ответа, и каждый ведёт в свой шаг. "
                "Контакт отвечает номером варианта или его словами — кнопок у "
                "обычного аккаунта нет. Шаг без вариантов идёт туда, куда указано "
                "в «Дальше», иначе просто к следующему по списку."
            )
        else:
            self.note_label.setText(
                "Шаги идут строго по порядку, без развилок. Когда контакт ответит "
                "на последний вопрос, ответы станут заявкой и уйдут менеджеру."
            )
        for key, btn in self.kind_buttons.items():
            btn.setEnabled(self.selected_scenario_id is not None)

    def _on_kind_pick(self, kind: str) -> None:
        if self.selected_scenario_id is None or kind == self._kind():
            self._sync_kind()
            return
        if kind == LINEAR:
            branching_steps = [s for s in self._steps() if options_of(s) or s.get("next")]
            if branching_steps and QMessageBox.question(
                self, "Сделать пошаговым",
                f"В {len(branching_steps)} " + _plural(branching_steps and len(branching_steps),
                                                        "шаге", "шагах", "шагах")
                + " заданы развилки. Пошаговый сценарий пойдёт строго по порядку и "
                "переходы учитывать не будет — сами они сохранятся, так что "
                "вернуть развилки можно этой же кнопкой. Продолжить?"
            ) != QMessageBox.Yes:
                self._sync_kind()
                return
        self.ctx.db.update_scenario(self.selected_scenario_id, kind=kind)
        self.saved_label.setText("сохранено только что")
        self._reload_steps()

    # ---- развилка выбранного шага -------------------------------------
    def _targets(self, steps: list[dict], exclude_id=None) -> list[tuple[str, object]]:
        out: list[tuple[str, object]] = [("следующий по списку", None)]
        for i, step in enumerate(steps):
            if step.get("id") == exclude_id:
                continue
            title = (step.get("question") or "").strip() or f"шаг {i + 1}"
            out.append((f"{i + 1}. {title[:34]}", step.get("id")))
        out.append(("завершить разговор", END))
        return out

    def _clear_options(self) -> None:
        for row in self._option_rows:
            row.setParent(None)
            row.deleteLater()
        self._option_rows.clear()

    def _sync_branch(self, steps: list[dict], step: dict) -> None:
        branching = self._kind() == BRANCHING
        self.branch_box.setVisible(branching)
        options = options_of(step)
        self.validation_box.setVisible(not (branching and options))
        if not branching:
            return

        self._clear_options()
        targets = self._targets(steps, exclude_id=step.get("id"))
        for i, option in enumerate(step.get("options") or []):
            # Подпись и переход — двумя строками, а не в одну: панель узкая,
            # и в одной строке от подписи оставалось три буквы.
            row = QWidget()
            rl = QVBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(4)
            top = QHBoxLayout()
            top.setContentsMargins(0, 0, 0, 0)
            top.setSpacing(4)
            label_input = QLineEdit(str(option.get("label", "")))
            label_input.setPlaceholderText("что ответит контакт")
            label_input.editingFinished.connect(
                lambda idx=i: self._on_option_label_changed(idx))
            top.addWidget(label_input, 1)
            del_btn = button("×", "ghost")
            del_btn.setFixedWidth(26)
            del_btn.clicked.connect(lambda _c, n=i: self._on_remove_option(n))
            top.addWidget(del_btn)
            rl.addLayout(top)
            bottom = QHBoxLayout()
            bottom.setContentsMargins(12, 0, 0, 0)
            bottom.setSpacing(4)
            bottom.addWidget(muted("→"))
            combo = QComboBox()
            for text, data in targets:
                combo.addItem(text, data)
            idx = combo.findData(option.get("next"))
            combo.setCurrentIndex(max(0, idx))
            combo.currentIndexChanged.connect(
                lambda _i, n=i: self._on_option_next_changed(n))
            bottom.addWidget(combo, 1)
            rl.addLayout(bottom)
            self.options_lay.addWidget(row)
            self._option_rows.append(row)
            row._label_input = label_input
            row._combo = combo

        self.branch_hint.setText(
            "Без вариантов контакт отвечает своими словами."
            if not options else
            "Ответ засчитывается по номеру варианта или по его словам."
        )
        self.next_combo.blockSignals(True)
        self.next_combo.clear()
        for text, data in targets:
            self.next_combo.addItem(text, data)
        idx = self.next_combo.findData(step.get("next"))
        self.next_combo.setCurrentIndex(max(0, idx))
        self.next_combo.blockSignals(False)
        # «Дальше» — это про шаг без вариантов: когда варианты есть, куда
        # идти, решает выбранный вариант.
        self.next_combo.setEnabled(not options)

    def _on_add_option(self) -> None:
        steps = self._steps()
        if not steps:
            return
        step = steps[self.step_sel]
        step.setdefault("options", []).append({"label": "", "next": None})
        self._save_steps(steps)

    def _on_remove_option(self, index: int) -> None:
        steps = self._steps()
        if not steps:
            return
        options = steps[self.step_sel].get("options") or []
        if 0 <= index < len(options):
            del options[index]
        self._save_steps(steps)

    def _on_option_label_changed(self, index: int) -> None:
        steps = self._steps()
        if not steps or index >= len(self._option_rows):
            return
        options = steps[self.step_sel].get("options") or []
        if index >= len(options):
            return
        options[index]["label"] = self._option_rows[index]._label_input.text().strip()
        self._save_steps(steps)

    def _on_option_next_changed(self, index: int) -> None:
        steps = self._steps()
        if not steps or index >= len(self._option_rows):
            return
        options = steps[self.step_sel].get("options") or []
        if index >= len(options):
            return
        options[index]["next"] = self._option_rows[index]._combo.currentData()
        self._save_steps(steps)

    def _on_next_changed(self, _index: int) -> None:
        steps = self._steps()
        if not steps:
            return
        steps[self.step_sel]["next"] = self.next_combo.currentData()
        self._save_steps(steps)

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
        self.lead_field_combo.blockSignals(True)
        idx = self.lead_field_combo.findData(step.get("lead_field"))
        self.lead_field_combo.setCurrentIndex(max(0, idx))
        self.lead_field_combo.blockSignals(False)
        validation = step.get("validation", "text")
        btn = self.validation_buttons.get(validation)
        if btn is not None:
            btn.setChecked(True)
        self.validation_hint.setText(_VALIDATION_HINTS.get(validation, ""))
        self._sync_branch(steps, step)
        # Предпросмотр показывает то же, что уйдёт контакту, — вместе с
        # пронумерованными вариантами: без номеров развилка выглядит как
        # вопрос без ответа.
        preview = format_question(step) if self._kind() == BRANCHING else step.get("question", "")
        self.preview_bubble.setText(preview or "(вопрос пока пустой)")

    def _on_props_changed(self) -> None:
        steps = self._steps()
        if not steps:
            return
        steps[self.step_sel]["question"] = self.question_input.text().strip()
        steps[self.step_sel]["field"] = self.field_input.text().strip() or f"field_{self.step_sel + 1}"
        steps[self.step_sel]["lead_field"] = self.lead_field_combo.currentData()
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
        steps = self._steps()
        lead_fields = {s["field"]: s["lead_field"] for s in steps if s.get("lead_field")}
        visited = len([s for s in trail if "final_answers" not in s])

        lines = [f"Путь: {visited} из {len(steps)} шагов сценария."]
        for step in trail:
            if "final_answers" in step:
                final = step["final_answers"]
                lines.append(f"\nЗаявка вышла бы такой: {final}")
                if lead_fields:
                    mapped = {lead_fields[f]: v for f, v in final.items() if f in lead_fields}
                    lines.append(
                        "В карточку лида попали бы поля: "
                        + (", ".join(f"{k}={v!r}" for k, v in mapped.items()) if mapped else "(ничего не сопоставлено)")
                    )
                # С5: what happens after the last question — the same two
                # things continue_scenario() does for real, see rules_engine.py.
                scenario = self.ctx.db.get_scenario(self.selected_scenario_id)
                template_id = scenario["done_template_id"] if scenario else None
                if template_id is not None:
                    template = self.ctx.db.get_template(template_id)
                    if template is not None:
                        lines.append(f"Контакту ушло бы: «{render(template['text'], final)}»")
                    else:
                        lines.append("Подтверждение не отправилось бы — выбранный шаблон удалён.")
                bot = self.ctx.db.get_bot(self.selected_bot_id) if self.selected_bot_id else None
                if bot and bot["manager_chat_id"]:
                    lines.append(f"Менеджеру ({bot['manager_chat_id']}) ушла бы сводка ответов.")
                else:
                    lines.append("Менеджер не уведомился бы — у бота не задан «Кому пересылать заявки».")
            else:
                line = f"«{step['question']}» → поле {step['field']}"
                if step["field"] in lead_fields:
                    line += f" [→ {lead_fields[step['field']]}]"
                if "answer" in step:
                    line += f" = {step['answer']!r}"
                    if step.get("error"):
                        line += f"  [ошибка: {step['error']}]"
                lines.append(line)
        self.test_output.setPlainText("\n".join(lines))
