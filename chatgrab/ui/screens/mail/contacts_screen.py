"""П10: «Почта → Адресная книга» — the first standalone contact-list
screen in the app (see PLAN.md's own note that nothing like this existed
before mail needed one). Same flat-list-plus-dialog shape as
ui/screens/directions.py, CSV instead of that screen's JSON for import/
export since that's what the checklist itself asks for here.

Rows come from two sources merged into one table, distinguished only by
the "Откуда" column: source='auto' (upsert_mail_contact_from_message(),
one row per address ever seen in incoming mail) and source='manual'
(added here by hand, or via CSV import) — both live in the same
mail_contact table, so editing an auto row (giving it a display name,
say) just turns it into a normal row, nothing special to migrate.
"""
from __future__ import annotations

import csv

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView,
    QLineEdit, QMessageBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ...context import AppContext
from ...widgets import button, card, h1, muted, plural

_CSV_FIELDS = ["address", "display_name", "group_name"]


class ContactDialog(QDialog):
    def __init__(self, parent=None, contact=None):
        super().__init__(parent)
        self.setWindowTitle("Контакт" if contact else "Новый контакт")
        self.setMinimumWidth(400)
        lay = QVBoxLayout(self)

        lay.addWidget(muted("Email"))
        self.address_input = QLineEdit(contact["address"] if contact else "")
        self.address_input.setEnabled(contact is None)  # адрес — ключ, после создания не меняется
        lay.addWidget(self.address_input)

        lay.addWidget(muted("Имя"))
        self.name_input = QLineEdit((contact["display_name"] or "") if contact else "")
        lay.addWidget(self.name_input)

        lay.addWidget(muted("Группа"))
        self.group_input = QLineEdit((contact["group_name"] or "") if contact else "")
        lay.addWidget(self.group_input)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel_btn = button("Отмена", "secondary")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        save_btn = button("Сохранить", "primary")
        save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(save_btn)
        lay.addLayout(btn_row)

    def _on_save(self) -> None:
        if not self.address_input.text().strip() or "@" not in self.address_input.text():
            QMessageBox.information(self, "Нужен email", "Укажите настоящий адрес — с «@».")
            return
        self.accept()

    def values(self) -> dict:
        return {
            "address": self.address_input.text().strip(),
            "display_name": self.name_input.text().strip() or None,
            "group_name": self.group_input.text().strip() or None,
        }


class MailContactsScreen(QWidget):
    def __init__(self, ctx: AppContext, navigate):
        super().__init__()
        self.ctx = ctx
        self.navigate = navigate

        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 28, 40, 24)
        outer.setSpacing(0)

        head = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.setSpacing(3)
        title_col.addWidget(h1("Адресная книга"))
        self.summary_label = muted("")
        title_col.addWidget(self.summary_label)
        head.addLayout(title_col)
        head.addStretch(1)
        import_btn = button("Импорт CSV…", "secondary")
        import_btn.clicked.connect(self._on_import_csv)
        head.addWidget(import_btn, alignment=Qt.AlignBottom)
        export_btn = button("Экспорт CSV…", "secondary")
        export_btn.clicked.connect(self._on_export_csv)
        head.addWidget(export_btn, alignment=Qt.AlignBottom)
        outer.addLayout(head)
        outer.addSpacing(6)

        hint = muted(
            "Собирается сама из переписки — каждый новый адрес отправителя попадает сюда "
            "автоматически — плюс записи, добавленные вручную или импортированные из CSV.")
        hint.setWordWrap(True)
        outer.addWidget(hint)
        outer.addSpacing(14)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Поиск по адресу или имени…")
        self.search_input.textChanged.connect(lambda _t: self.refresh())
        filter_row.addWidget(self.search_input, 1)
        self.group_combo = QComboBox()
        self.group_combo.currentIndexChanged.connect(lambda _i: self.refresh())
        filter_row.addWidget(self.group_combo)
        add_btn = button("＋ Новый контакт", "primary")
        add_btn.clicked.connect(self._on_add)
        filter_row.addWidget(add_btn)
        outer.addLayout(filter_row)
        outer.addSpacing(10)

        list_card = card()
        list_lay = QVBoxLayout(list_card)
        list_lay.setContentsMargins(16, 12, 16, 14)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Email", "Имя", "Группа", "Откуда", "", ""])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.cellDoubleClicked.connect(lambda row, _c: self._on_edit(row))
        list_lay.addWidget(self.table)
        outer.addWidget(list_card, 1)

    def on_show(self, **kwargs) -> None:
        self._populate_groups()
        self.refresh()

    def _populate_groups(self) -> None:
        current = self.group_combo.currentData()
        self.group_combo.blockSignals(True)
        self.group_combo.clear()
        self.group_combo.addItem("Любая группа", None)
        for group in self.ctx.db.list_mail_contact_groups():
            self.group_combo.addItem(group, group)
        idx = self.group_combo.findData(current)
        self.group_combo.setCurrentIndex(max(0, idx))
        self.group_combo.blockSignals(False)

    def refresh(self) -> None:
        rows = self.ctx.db.list_mail_contacts(
            query=self.search_input.text().strip() or None,
            group_name=self.group_combo.currentData())
        n = len(rows)
        self.summary_label.setText(f"{n} " + plural(n, "контакт", "контакта", "контактов") if n else "")

        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            item = QTableWidgetItem(row["address"])
            item.setData(Qt.UserRole, row["id"])
            self.table.setItem(i, 0, item)
            self.table.setItem(i, 1, QTableWidgetItem(row["display_name"] or "—"))
            self.table.setItem(i, 2, QTableWidgetItem(row["group_name"] or "—"))
            self.table.setItem(i, 3, QTableWidgetItem(
                "вручную" if row["source"] == "manual" else "из переписки"))

            edit_btn = button("Изменить", "ghost")
            edit_btn.clicked.connect(lambda _c, idx=i: self._on_edit(idx))
            self.table.setCellWidget(i, 4, edit_btn)
            del_btn = button("Удалить", "ghost")
            del_btn.clicked.connect(lambda _c, cid=row["id"], addr=row["address"]: self._on_delete(cid, addr))
            self.table.setCellWidget(i, 5, del_btn)
            self.table.setRowHeight(i, 40)

        self.table.resizeColumnsToContents()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        for col, width in ((3, 110), (4, 100), (5, 100)):
            header.setSectionResizeMode(col, QHeaderView.Fixed)
            self.table.setColumnWidth(col, width)

    # ---- actions -----------------------------------------------------
    def _on_add(self) -> None:
        dlg = ContactDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        values = dlg.values()
        self.ctx.db.create_mail_contact(values["address"], values["display_name"], values["group_name"])
        self._populate_groups()
        self.refresh()

    def _row_id_at(self, row: int):
        item = self.table.item(row, 0)
        return item.data(Qt.UserRole) if item else None

    def _on_edit(self, row: int) -> None:
        contact_id = self._row_id_at(row)
        if contact_id is None:
            return
        contact = self.ctx.db.get_mail_contact(contact_id)
        if contact is None:
            return
        dlg = ContactDialog(self, contact=contact)
        if dlg.exec() != QDialog.Accepted:
            return
        values = dlg.values()
        self.ctx.db.update_mail_contact(contact_id, display_name=values["display_name"],
                                         group_name=values["group_name"])
        self._populate_groups()
        self.refresh()

    def _on_delete(self, contact_id: int, address: str) -> None:
        if QMessageBox.question(
            self, "Удалить контакт", f"Удалить «{address}» из адресной книги?"
        ) != QMessageBox.Yes:
            return
        self.ctx.db.delete_mail_contact(contact_id)
        self.refresh()

    # ---- CSV ------------------------------------------------------------
    def _on_export_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Экспорт адресной книги", "contacts.csv", "CSV (*.csv)")
        if not path:
            return
        rows = self.ctx.db.list_mail_contacts(limit=100000)
        try:
            with open(path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=_CSV_FIELDS)
                writer.writeheader()
                for row in rows:
                    writer.writerow({k: row[k] or "" for k in _CSV_FIELDS})
        except OSError as e:
            QMessageBox.warning(self, "Не удалось сохранить", str(e))
            return
        QMessageBox.information(self, "Готово", f"Адресная книга сохранена: {path}")

    def _on_import_csv(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Импорт адресной книги", "", "CSV (*.csv)")
        if not path:
            return
        try:
            with open(path, "r", newline="", encoding="utf-8") as fh:
                sample = fh.read(4096)
                fh.seek(0)
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
                except csv.Error:
                    dialect = csv.excel
                reader = csv.DictReader(fh, dialect=dialect)
                rows = list(reader)
        except OSError as e:
            QMessageBox.warning(self, "Не удалось прочитать файл", str(e))
            return

        added = 0
        for row in rows:
            address = (row.get("address") or row.get("email") or row.get("Email") or "").strip()
            if not address or "@" not in address:
                continue
            display_name = (row.get("display_name") or row.get("name") or row.get("Имя") or "").strip() or None
            group_name = (row.get("group_name") or row.get("group") or row.get("Группа") or "").strip() or None
            self.ctx.db.create_mail_contact(address, display_name, group_name)
            added += 1
        self._populate_groups()
        self.refresh()
        if added:
            QMessageBox.information(
                self, "Готово", f"Добавлено/обновлено {added} " +
                plural(added, "контакт", "контакта", "контактов") + ".")
        else:
            QMessageBox.information(
                self, "Ничего не добавлено",
                "В файле не нашлось ни одной строки с настоящим email — нужна колонка "
                "«address» или «email».")
