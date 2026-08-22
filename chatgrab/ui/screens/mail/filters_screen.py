"""П10: «Почта → Фильтры» — rules that act on new mail as it syncs
(core/mail_filter.py decides the match, services/mail_service.py's
_apply_mail_filters() does the acting). Deliberately no delete action
anywhere in this screen: the create/edit dialog's action checkboxes are
label/move/mark-read/mute, full stop — "фильтр никогда не удаляет" isn't
a UI-level promise here, mail_filter's own schema has nowhere to put a
delete action even if someone wanted one.

Below the filter list: the journal every hit writes, each entry with its
own «Отменить» — MailService.undo_filter_hit() reverses exactly what
that one hit did.
"""
from __future__ import annotations

import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFrame, QHBoxLayout, QHeaderView,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ...context import AppContext
from ...format import short_dt
from ...widgets import Card, TabletCheckBox, ToggleSwitch, button, h1, label, muted

_FIELD_LABELS = [
    ("sender", "Отправитель"),
    ("domain", "Домен отправителя"),
    ("subject", "Тема"),
    ("body", "Текст письма"),
    ("has_attachment", "Есть вложение"),
    ("size_over_kb", "Размер вложений от, КБ"),
]
_OP_LABELS = [("contains", "содержит"), ("equals", "равно"), ("starts_with", "начинается с")]


class ConditionRow(QWidget):
    def __init__(self, cond: dict | None, on_remove) -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.field_combo = QComboBox()
        for key, title in _FIELD_LABELS:
            self.field_combo.addItem(title, key)
        self.op_combo = QComboBox()
        for key, title in _OP_LABELS:
            self.op_combo.addItem(title, key)
        self.value_input = QLineEdit()
        if cond:
            idx = self.field_combo.findData(cond.get("field"))
            self.field_combo.setCurrentIndex(max(0, idx))
            idx = self.op_combo.findData(cond.get("op", "contains"))
            self.op_combo.setCurrentIndex(max(0, idx))
            self.value_input.setText(str(cond.get("value", "")))
        self.field_combo.currentIndexChanged.connect(self._on_field_changed)
        lay.addWidget(self.field_combo)
        lay.addWidget(self.op_combo)
        lay.addWidget(self.value_input, 1)
        remove_btn = button("✕", "ghost")
        remove_btn.setFixedWidth(28)
        remove_btn.clicked.connect(lambda: on_remove(self))
        lay.addWidget(remove_btn)
        self._on_field_changed(self.field_combo.currentIndex())

    def _on_field_changed(self, _idx: int) -> None:
        field = self.field_combo.currentData()
        is_flag = field == "has_attachment"
        self.op_combo.setVisible(not is_flag and field != "size_over_kb")
        self.value_input.setVisible(not is_flag)
        if is_flag:
            self.value_input.setText("true")

    def condition(self) -> dict | None:
        field = self.field_combo.currentData()
        value = self.value_input.text().strip()
        if field == "has_attachment":
            return {"field": field, "value": True}
        if not value:
            return None
        return {"field": field, "op": self.op_combo.currentData(), "value": value}


class FilterDialog(QDialog):
    def __init__(self, ctx: AppContext, parent=None, mailbox_id: int | None = None, filt=None):
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle("Фильтр" if filt else "Новый фильтр")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)

        lay.addWidget(muted("Название"))
        self.name_input = QLineEdit(filt["name"] if filt else "")
        lay.addWidget(self.name_input)

        lay.addWidget(muted("Ящик"))
        self.mailbox_combo = QComboBox()
        self.mailbox_combo.addItem("Все ящики", None)
        for mb in ctx.db.list_mailboxes():
            self.mailbox_combo.addItem(mb["address"], mb["id"])
        current_mailbox = filt["mailbox_id"] if filt else mailbox_id
        idx = self.mailbox_combo.findData(current_mailbox)
        self.mailbox_combo.setCurrentIndex(max(0, idx))
        lay.addWidget(self.mailbox_combo)

        lay.addWidget(label("УСЛОВИЯ (все должны совпасть)", "kicker"))
        self.conditions_box = QVBoxLayout()
        lay.addLayout(self.conditions_box)
        self._condition_rows: list[ConditionRow] = []
        existing = json.loads(filt["conditions"]) if filt else []
        for cond in existing or [{}]:
            self._add_condition_row(cond if cond else None)
        add_cond_btn = button("+ условие", "ghost")
        add_cond_btn.clicked.connect(lambda: self._add_condition_row(None))
        lay.addWidget(add_cond_btn)

        lay.addWidget(label("ДЕЙСТВИЯ", "kicker"))
        self.label_combo = QComboBox()
        self.label_combo.addItem("— без ярлыка —", None)
        mailboxes_for_labels = [current_mailbox] if current_mailbox else [mb["id"] for mb in ctx.db.list_mailboxes()]
        seen_labels = set()
        for mbid in mailboxes_for_labels:
            for lb in ctx.db.list_mail_labels(mbid):
                if lb["id"] in seen_labels:
                    continue
                seen_labels.add(lb["id"])
                self.label_combo.addItem(lb["name"], lb["id"])
        if filt and filt["label_id"]:
            idx = self.label_combo.findData(filt["label_id"])
            self.label_combo.setCurrentIndex(max(0, idx))
        lay.addWidget(self.label_combo)

        self.move_input = QLineEdit(filt["move_to_folder"] or "" if filt else "")
        self.move_input.setPlaceholderText("Переместить в папку (имя папки, необязательно)")
        lay.addWidget(self.move_input)

        self.mark_read_cb = TabletCheckBox("Пометить прочитанным")
        self.mark_read_cb.setChecked(bool(filt["mark_read"]) if filt else False)
        lay.addWidget(self.mark_read_cb)
        self.no_notify_cb = TabletCheckBox("Не уведомлять")
        self.no_notify_cb.setChecked(bool(filt["no_notify"]) if filt else False)
        lay.addWidget(self.no_notify_cb)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel_btn = button("Отмена", "secondary")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        save_btn = button("Сохранить", "primary")
        save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(save_btn)
        lay.addLayout(btn_row)

    def _add_condition_row(self, cond: dict | None) -> None:
        row = ConditionRow(cond, self._on_remove_condition)
        self._condition_rows.append(row)
        self.conditions_box.addWidget(row)

    def _on_remove_condition(self, row: ConditionRow) -> None:
        if len(self._condition_rows) <= 1:
            return  # хотя бы одно условие должно остаться в форме
        self._condition_rows.remove(row)
        row.setParent(None)
        row.deleteLater()

    def _on_save(self) -> None:
        if not self.name_input.text().strip():
            QMessageBox.information(self, "Нужно название", "Дайте фильтру короткое имя.")
            return
        if not self.conditions():
            QMessageBox.information(
                self, "Нужно хотя бы одно условие",
                "Пустой фильтр ничего не будет ловить — заполните хотя бы одну строку.")
            return
        self.accept()

    def conditions(self) -> list[dict]:
        return [c for c in (row.condition() for row in self._condition_rows) if c is not None]

    def values(self) -> dict:
        return {
            "name": self.name_input.text().strip(),
            "mailbox_id": self.mailbox_combo.currentData(),
            "conditions": self.conditions(),
            "label_id": self.label_combo.currentData(),
            "move_to_folder": self.move_input.text().strip() or None,
            "mark_read": self.mark_read_cb.isChecked(),
            "no_notify": self.no_notify_cb.isChecked(),
        }


class MailFiltersScreen(QWidget):
    def __init__(self, ctx: AppContext, navigate):
        super().__init__()
        self.ctx = ctx
        self.navigate = navigate

        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 28, 40, 32)
        outer.addWidget(h1("Фильтры"))
        hint = muted(
            "Фильтр никогда не удаляет письмо — только ярлык, папка, «прочитано» или "
            "«не уведомлять». Каждое срабатывание попадает в журнал ниже и отменяется одной кнопкой.")
        hint.setWordWrap(True)
        outer.addWidget(hint)
        outer.addSpacing(14)

        outer.addWidget(self._build_list_card())
        outer.addSpacing(16)
        outer.addWidget(self._build_log_card(), 1)

    def _build_list_card(self) -> QWidget:
        c = Card()
        lay = QVBoxLayout(c)
        lay.setContentsMargins(16, 14, 16, 14)
        add_row = QHBoxLayout()
        add_btn = button("＋ Новый фильтр", "primary")
        add_btn.clicked.connect(self._on_add)
        add_row.addWidget(add_btn)
        add_row.addStretch(1)
        lay.addLayout(add_row)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Название", "Ящик", "Действия", "Активен", ""])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.cellDoubleClicked.connect(lambda row, _c: self._on_edit(row))
        lay.addWidget(self.table)
        return c

    def _build_log_card(self) -> QWidget:
        # design-brief.md §3.9 — тёмная лог-панель (LOG_BG), не обычная
        # карточка: используется тот же QSS-класс "logpanel", что и
        # collect.py (Д4), собственный список строк (не widgets.LogPanel
        # — его схема колонок "время|чат|текст" не даёт места под
        # кнопку «Отменить» на строку, которую требует этот журнал).
        c = QFrame()
        c.setProperty("class", "logpanel")
        lay = QVBoxLayout(c)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.addWidget(label("ЖУРНАЛ СРАБАТЫВАНИЙ", "kicker"))
        self.log_list = QListWidget()
        lay.addWidget(self.log_list, 1)
        return c

    def on_show(self, **kwargs) -> None:
        self.refresh()

    def refresh(self) -> None:
        rows = self.ctx.db.list_mail_filters()
        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            item = QTableWidgetItem(row["name"])
            item.setData(Qt.UserRole, row["id"])
            self.table.setItem(i, 0, item)
            mailbox = self.ctx.db.get_mailbox(row["mailbox_id"]) if row["mailbox_id"] else None
            self.table.setItem(i, 1, QTableWidgetItem(mailbox["address"] if mailbox else "все"))
            actions = []
            if row["label_id"]:
                lb = self.ctx.db.get_mail_label(row["label_id"])
                actions.append(f"ярлык «{lb['name']}»" if lb else "ярлык")
            if row["move_to_folder"]:
                actions.append(f"в «{row['move_to_folder']}»")
            if row["mark_read"]:
                actions.append("прочитано")
            if row["no_notify"]:
                actions.append("без уведомления")
            self.table.setItem(i, 2, QTableWidgetItem("; ".join(actions) or "—"))

            enabled_holder = QWidget()
            enabled_lay = QHBoxLayout(enabled_holder)
            enabled_lay.setContentsMargins(8, 0, 0, 0)
            enabled_sw = ToggleSwitch(bool(row["enabled"]))
            enabled_sw.toggled.connect(lambda on, fid=row["id"]: self._on_toggle(fid, on))
            enabled_lay.addWidget(enabled_sw)
            self.table.setCellWidget(i, 3, enabled_holder)

            del_btn = button("Удалить", "ghost")
            del_btn.clicked.connect(lambda _c, fid=row["id"], name=row["name"]: self._on_delete(fid, name))
            self.table.setCellWidget(i, 4, del_btn)
            self.table.setRowHeight(i, 40)

        self.table.resizeColumnsToContents()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for col, width in ((3, 80), (4, 100)):
            header.setSectionResizeMode(col, QHeaderView.Fixed)
            self.table.setColumnWidth(col, width)

        self._refresh_log()

    def _refresh_log(self) -> None:
        self.log_list.clear()
        entries = self.ctx.db.list_filter_log(limit=100)
        if not entries:
            self.log_list.addItem("Пока ничего не срабатывало.")
            return
        for entry in entries:
            item = QListWidgetItem()
            self.log_list.addItem(item)
            row_widget = QWidget()
            row_lay = QHBoxLayout(row_widget)
            row_lay.setContentsMargins(4, 2, 4, 2)
            text = f"{short_dt(entry['applied_at'])} · {entry['summary']}"
            row_lay.addWidget(muted(text), 1)
            if not entry["undone"]:
                undo_btn = button("Отменить", "ghost")
                undo_btn.clicked.connect(lambda _c, lid=entry["id"]: self._on_undo(lid))
                row_lay.addWidget(undo_btn)
            else:
                row_lay.addWidget(muted("отменено"))
            item.setSizeHint(row_widget.sizeHint())
            self.log_list.setItemWidget(item, row_widget)

    # ---- actions -----------------------------------------------------
    def _on_add(self) -> None:
        dlg = FilterDialog(self.ctx, self)
        if dlg.exec() != QDialog.Accepted:
            return
        values = dlg.values()
        self.ctx.db.create_mail_filter(
            values["name"], values["conditions"], mailbox_id=values["mailbox_id"],
            label_id=values["label_id"], move_to_folder=values["move_to_folder"],
            mark_read=values["mark_read"], no_notify=values["no_notify"])
        self.refresh()

    def _row_id_at(self, row: int):
        item = self.table.item(row, 0)
        return item.data(Qt.UserRole) if item else None

    def _on_edit(self, row: int) -> None:
        filter_id = self._row_id_at(row)
        if filter_id is None:
            return
        filt = self.ctx.db.get_mail_filter(filter_id)
        if filt is None:
            return
        dlg = FilterDialog(self.ctx, self, filt=filt)
        if dlg.exec() != QDialog.Accepted:
            return
        values = dlg.values()
        self.ctx.db.update_mail_filter(filter_id, **values)
        self.refresh()

    def _on_delete(self, filter_id: int, name: str) -> None:
        if QMessageBox.question(
            self, "Удалить фильтр", f"Удалить фильтр «{name}»? Уже сработавшее не отменяется этим — "
            "используйте «Отменить» в журнале для конкретных писем."
        ) != QMessageBox.Yes:
            return
        self.ctx.db.delete_mail_filter(filter_id)
        self.refresh()

    def _on_toggle(self, filter_id: int, enabled: bool) -> None:
        self.ctx.db.update_mail_filter(filter_id, enabled=enabled)
        self.refresh()

    def _on_undo(self, log_id: int) -> None:
        self.ctx.mail_service.undo_filter_hit(log_id)
        self.refresh()
