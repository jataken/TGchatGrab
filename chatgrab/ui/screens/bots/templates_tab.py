from __future__ import annotations

import json
import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPlainTextEdit, QSplitter, QVBoxLayout, QWidget,
)

from ...context import AppContext
from ...widgets import Card, button, dashed_button, label, muted, plural as _plural

_VAR_RE = re.compile(r"\{(\w+)\}")


class TemplatesTab(QWidget):
    """Message library with `{variable}` placeholders — used by send_dm/
    notify action config and as scenario completion messages."""

    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.selected_bot_id: int | None = None
        self.selected_template_id: int | None = None
        ctx.bot_selection.changed.connect(self._on_bot_changed)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 16, 0, 16)

        split = QSplitter()
        outer.addWidget(split, 1)

        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 8, 0)
        left_lay.addWidget(label("ШАБЛОНЫ", "kicker"))
        self.tpl_list = QListWidget()
        self.tpl_list.currentItemChanged.connect(self._on_selected)
        left_lay.addWidget(self.tpl_list, 1)
        add_btn = dashed_button("＋ Новый шаблон")
        add_btn.clicked.connect(self._on_add)
        left_lay.addWidget(add_btn)
        split.addWidget(left)

        right = Card()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(14, 14, 14, 14)
        right_lay.addWidget(muted("Название"))
        self.name_input = QLineEdit()
        right_lay.addWidget(self.name_input)
        right_lay.addWidget(muted("Текст"))
        self.text_input = QPlainTextEdit()
        self.text_input.setMinimumHeight(140)
        right_lay.addWidget(self.text_input, 1)
        vars_hint = muted(
            "Подставляются: {имя}, {ник}, {бот}, {менеджер}, {текст} — а также "
            "любое поле из сценария под тем именем, которое вы ему дали "
            "(например {объём}). Незнакомое имя останется в тексте как есть."
        )
        vars_hint.setWordWrap(True)
        right_lay.addWidget(vars_hint)
        self.vars_label = muted("")
        right_lay.addWidget(self.vars_label)

        btn_row = QHBoxLayout()
        save_btn = button("Сохранить", "primary")
        save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(save_btn)
        delete_btn = button("Удалить", "ghost")
        delete_btn.clicked.connect(self._on_delete)
        btn_row.addWidget(delete_btn)
        btn_row.addStretch(1)
        right_lay.addLayout(btn_row)

        split.addWidget(right)
        split.setSizes([260, 560])
        self.text_input.textChanged.connect(self._update_vars_label)

    def on_show(self) -> None:
        self.selected_bot_id = self.ctx.bot_selection.current
        self._reload()

    def _on_bot_changed(self, bot_id) -> None:
        self.selected_bot_id = bot_id
        self._reload()

    def _reload(self) -> None:
        self.tpl_list.clear()
        self.selected_template_id = None
        self.name_input.clear()
        self.text_input.clear()
        if self.selected_bot_id is None:
            return
        for tpl in self.ctx.db.list_templates(self.selected_bot_id):
            item = QListWidgetItem(tpl["name"])
            item.setData(Qt.UserRole, tpl["id"])
            self.tpl_list.addItem(item)

    def _on_selected(self, current: QListWidgetItem, _prev) -> None:
        if current is None:
            self.selected_template_id = None
            return
        self.selected_template_id = current.data(Qt.UserRole)
        tpl = self.ctx.db.get_template(self.selected_template_id)
        if tpl:
            self.name_input.setText(tpl["name"])
            self.text_input.setPlainText(tpl["text"])

    def _update_vars_label(self) -> None:
        variables = sorted(set(_VAR_RE.findall(self.text_input.toPlainText())))
        self.vars_label.setText(f"Переменные: {', '.join(variables)}" if variables else "Переменных нет")

    def _on_add(self) -> None:
        if self.selected_bot_id is None:
            QMessageBox.information(self, "Выберите бота", "Сначала выберите бота вверху экрана.")
            return
        tpl_id = self.ctx.db.add_template(self.selected_bot_id, "Новый шаблон", "", [])
        self._reload()
        for i in range(self.tpl_list.count()):
            if self.tpl_list.item(i).data(Qt.UserRole) == tpl_id:
                self.tpl_list.setCurrentRow(i)
                break

    def _on_save(self) -> None:
        if self.selected_template_id is None:
            return
        text = self.text_input.toPlainText()
        variables = sorted(set(_VAR_RE.findall(text)))
        self.ctx.db.update_template(
            self.selected_template_id, name=self.name_input.text().strip() or "Без названия",
            text=text, variables=variables,
        )
        self._reload()

    def _on_delete(self) -> None:
        if self.selected_template_id is None:
            return
        tpl = self.ctx.db.get_template(self.selected_template_id)
        usage = self.ctx.db.template_usage(self.selected_template_id)
        parts = []
        if usage["actions"]:
            parts.append(f"{usage['actions']} " + _plural(
                usage["actions"], "действие в правилах", "действия в правилах", "действий в правилах"))
        if usage["scenarios"]:
            parts.append(f"{usage['scenarios']} " + _plural(
                usage["scenarios"], "сценарий", "сценария", "сценариев") + " (как сообщение о завершении)")

        name = tpl["name"] if tpl else "шаблон"
        if parts:
            text = (f"На «{name}» ссылается " + " и ".join(parts) +
                     ".\n\nПосле удаления эти места перестанут отправлять сообщение — "
                     "они будут помечены как проблемные в «Правилах». Удалить?")
        else:
            text = f"Удалить «{name}»? На него никто не ссылается."
        if QMessageBox.question(self, "Удалить шаблон", text) != QMessageBox.Yes:
            return
        self.ctx.db.delete_template(self.selected_template_id)
        self._reload()
