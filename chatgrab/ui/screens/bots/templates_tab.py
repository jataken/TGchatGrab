from __future__ import annotations

import json
import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPlainTextEdit, QSplitter, QVBoxLayout, QWidget,
)

from ...context import AppContext
from ...widgets import button, card, muted
from .common import populate_bot_picker

_VAR_RE = re.compile(r"\{(\w+)\}")


class TemplatesTab(QWidget):
    """Message library with `{variable}` placeholders — used by send_dm/
    notify action config and as scenario completion messages."""

    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.selected_bot_id: int | None = None
        self.selected_template_id: int | None = None

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
        left_lay.addWidget(muted("Шаблоны"))
        self.tpl_list = QListWidget()
        self.tpl_list.currentItemChanged.connect(self._on_selected)
        left_lay.addWidget(self.tpl_list, 1)
        add_btn = button("＋ Новый шаблон", "secondary")
        add_btn.clicked.connect(self._on_add)
        left_lay.addWidget(add_btn)
        split.addWidget(left)

        right = card()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(14, 14, 14, 14)
        right_lay.addWidget(muted("Название"))
        self.name_input = QLineEdit()
        right_lay.addWidget(self.name_input)
        right_lay.addWidget(muted("Текст — переменные вида {name}, {company}"))
        self.text_input = QPlainTextEdit()
        self.text_input.setMinimumHeight(140)
        right_lay.addWidget(self.text_input, 1)
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
        populate_bot_picker(self.ctx, self.bot_picker)
        self.selected_bot_id = self.bot_picker.currentData()
        self._reload()

    def _on_bot_changed(self, _index: int) -> None:
        self.selected_bot_id = self.bot_picker.currentData()
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
        self.ctx.db.delete_template(self.selected_template_id)
        self._reload()
