"""Справочник направлений — «Направления». Плоский список того, чем
торгует пользователь: косметическое сырьё, аккумуляторные батареи,
упаковка и так далее, с ключевыми словами и файлом прайса на каждое.

Подставляется дальше сам: ключевые слова — в поиск по чатам и в
мониторинг, файл прайса — в сценарии запроса прайса, само направление —
в карточку лида и в сопоставление с CRM. Экран общий для обоих блоков
(«Сбор» размечает чаты по направлению, «Боты» строит сценарии на нём),
поэтому висит в общих пунктах меню рядом с «Подключение» и «Настройки»,
а не внутри одного из блоков.
"""
from __future__ import annotations

import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView,
    QLineEdit, QMessageBox, QPlainTextEdit, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from ..context import AppContext
from ..widgets import button, card, h1, label, muted, plural


def _split_words(text: str) -> list[str]:
    """Одна строка через запятую → список слов, без пустых и дублей, но с
    сохранением порядка — так проще следить, что уже добавлено."""
    seen: list[str] = []
    for part in text.split(","):
        word = part.strip()
        if word and word not in seen:
            seen.append(word)
    return seen


class DirectionDialog(QDialog):
    """Добавление и редактирование одного направления — общая форма,
    отличаются только заголовком и тем, что поля предзаполнены."""

    def __init__(self, parent=None, direction: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("Направление" if direction else "Новое направление")
        self.setMinimumWidth(440)
        lay = QVBoxLayout(self)

        lay.addWidget(muted("Название"))
        self.name_input = QLineEdit(direction["name"] if direction else "")
        lay.addWidget(self.name_input)

        lay.addWidget(muted("Ключевые слова — через запятую, для поиска по чатам и мониторинга"))
        self.keywords_input = QLineEdit(
            ", ".join(json.loads(direction["keywords"])) if direction else "")
        lay.addWidget(self.keywords_input)

        lay.addWidget(muted("Стоп-слова — через запятую, чтобы отсечь явно не то"))
        self.stop_words_input = QLineEdit(
            ", ".join(json.loads(direction["stop_words"])) if direction else "")
        lay.addWidget(self.stop_words_input)

        lay.addWidget(muted("Файл прайса или ссылка"))
        price_row = QHBoxLayout()
        self.price_input = QLineEdit(direction["price_file"] or "" if direction else "")
        price_row.addWidget(self.price_input, 1)
        browse_btn = button("Обзор…", "secondary")
        browse_btn.clicked.connect(self._on_browse_price)
        price_row.addWidget(browse_btn)
        lay.addLayout(price_row)

        lay.addWidget(muted("Заметка"))
        self.note_input = QPlainTextEdit(direction["note"] or "" if direction else "")
        self.note_input.setMaximumHeight(70)
        lay.addWidget(self.note_input)

        self.enabled_cb = QCheckBox("Направление активно")
        self.enabled_cb.setChecked(bool(direction["enabled"]) if direction else True)
        lay.addWidget(self.enabled_cb)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel_btn = button("Отмена", "secondary")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        save_btn = button("Сохранить", "primary")
        save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(save_btn)
        lay.addLayout(btn_row)

    def _on_browse_price(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Файл прайса", self.price_input.text())
        if path:
            self.price_input.setText(path)

    def _on_save(self) -> None:
        if not self.name_input.text().strip():
            QMessageBox.information(self, "Нужно название", "Укажите, как называется направление.")
            return
        self.accept()

    def values(self) -> dict:
        return {
            "name": self.name_input.text().strip(),
            "keywords": _split_words(self.keywords_input.text()),
            "stop_words": _split_words(self.stop_words_input.text()),
            "price_file": self.price_input.text().strip() or None,
            "note": self.note_input.toPlainText().strip() or None,
            "enabled": 1 if self.enabled_cb.isChecked() else 0,
        }


class DirectionsScreen(QWidget):
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
        title_col.addWidget(h1("Направления"))
        self.summary_label = muted("")
        title_col.addWidget(self.summary_label)
        head.addLayout(title_col)
        head.addStretch(1)
        import_btn = button("Импортировать", "secondary")
        import_btn.clicked.connect(self._on_import)
        head.addWidget(import_btn, alignment=Qt.AlignBottom)
        export_btn = button("Экспортировать", "secondary")
        export_btn.clicked.connect(self._on_export)
        head.addWidget(export_btn, alignment=Qt.AlignBottom)
        outer.addLayout(head)
        outer.addSpacing(6)

        hint = muted(
            "Чем торгует ваша компания, с ключевыми словами и файлом прайса на каждое "
            "направление. Дальше подставляется в поиск по чатам, мониторинг, сценарии "
            "и карточки заявок."
        )
        hint.setWordWrap(True)
        outer.addWidget(hint)
        outer.addSpacing(14)

        list_card = card()
        list_lay = QVBoxLayout(list_card)
        list_lay.setContentsMargins(16, 12, 16, 14)
        list_lay.setSpacing(8)

        add_row = QHBoxLayout()
        add_row.setSpacing(8)
        add_btn = button("＋ Новое направление", "primary")
        add_btn.clicked.connect(self._on_add)
        add_row.addWidget(add_btn)
        add_row.addStretch(1)
        list_lay.addLayout(add_row)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["Название", "Ключевые слова", "Прайс", "Активно", "", "", ""])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.cellDoubleClicked.connect(lambda row, _c: self._on_edit(row))
        list_lay.addWidget(self.table)
        outer.addWidget(list_card, 1)

        self.empty_label = muted(
            "Направлений пока нет. Добавьте первое — оно сразу появится в фильтрах "
            "поиска и мониторинга."
        )
        self.empty_label.setWordWrap(True)
        outer.addWidget(self.empty_label)

    def on_show(self, **kwargs) -> None:
        self.refresh()

    def refresh(self) -> None:
        rows = self.ctx.db.list_directions()
        n = len(rows)
        self.summary_label.setText(
            f"{n} " + plural(n, "направление", "направления", "направлений") if n else "")
        self.empty_label.setVisible(not rows)
        self.table.setVisible(bool(rows))

        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            item = QTableWidgetItem(row["name"])
            item.setData(Qt.UserRole, row["id"])
            if not row["enabled"]:
                item.setForeground(Qt.gray)
            self.table.setItem(i, 0, item)

            keywords = json.loads(row["keywords"])
            self.table.setItem(i, 1, QTableWidgetItem(", ".join(keywords) if keywords else "—"))
            self.table.setItem(i, 2, QTableWidgetItem(row["price_file"] or "—"))

            enabled_holder = QWidget()
            enabled_lay = QHBoxLayout(enabled_holder)
            enabled_lay.setContentsMargins(8, 0, 0, 0)
            enabled_cb = QCheckBox()
            enabled_cb.setChecked(bool(row["enabled"]))
            enabled_cb.toggled.connect(
                lambda on, did=row["id"]: self._on_toggle_enabled(did, on))
            enabled_lay.addWidget(enabled_cb)
            enabled_lay.addStretch(1)
            self.table.setCellWidget(i, 3, enabled_holder)

            move_holder = QWidget()
            move_lay = QHBoxLayout(move_holder)
            move_lay.setContentsMargins(0, 0, 0, 0)
            move_lay.setSpacing(2)
            up_btn = button("↑", "ghost")
            up_btn.setFixedWidth(28)
            up_btn.setEnabled(i > 0)
            up_btn.clicked.connect(lambda _c, idx=i: self._on_move(idx, -1))
            move_lay.addWidget(up_btn)
            down_btn = button("↓", "ghost")
            down_btn.setFixedWidth(28)
            down_btn.setEnabled(i < len(rows) - 1)
            down_btn.clicked.connect(lambda _c, idx=i: self._on_move(idx, 1))
            move_lay.addWidget(down_btn)
            self.table.setCellWidget(i, 4, move_holder)

            edit_btn = button("Изменить", "ghost")
            edit_btn.clicked.connect(lambda _c, idx=i: self._on_edit(idx))
            self.table.setCellWidget(i, 5, edit_btn)

            del_btn = button("Удалить", "ghost")
            del_btn.clicked.connect(lambda _c, did=row["id"], name=row["name"]: self._on_delete(did, name))
            self.table.setCellWidget(i, 6, del_btn)

            self.table.setRowHeight(i, 44)
        self.table.resizeColumnsToContents()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        # resizeColumnsToContents measures QTableWidgetItem text, not a
        # cell widget's sizeHint — columns 3-6 carry only cellWidgets
        # (checkbox, move buttons, "Изменить", "Удалить"), so without an
        # explicit floor here they come out a few pixels wide. QPushButton
        # centres its text and clips in place rather than eliding, so a
        # too-narrow cell doesn't shorten the label with "…" — it eats
        # letters off both ends symmetrically ("Изменить" → "зменить").
        # The floor here has to clear each button's own sizeHint, not
        # just look roomy.
        for col, width in ((3, 70), (4, 68), (5, 116), (6, 108)):
            header.setSectionResizeMode(col, QHeaderView.Fixed)
            self.table.setColumnWidth(col, width)

    # ---- actions -------------------------------------------------------
    def _on_add(self) -> None:
        dlg = DirectionDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        values = dlg.values()
        self.ctx.db.add_direction(
            values["name"], keywords=values["keywords"], stop_words=values["stop_words"],
            price_file=values["price_file"], note=values["note"])
        self.refresh()

    def _on_edit(self, row: int) -> None:
        rows = self.ctx.db.list_directions()
        if row >= len(rows):
            return
        current = rows[row]
        dlg = DirectionDialog(self, direction=current)
        if dlg.exec() != QDialog.Accepted:
            return
        values = dlg.values()
        self.ctx.db.update_direction(current["id"], **values)
        self.refresh()

    def _on_delete(self, direction_id: int, name: str) -> None:
        if QMessageBox.question(
            self, "Удалить направление",
            f"Удалить «{name}»? Ключевые слова и файл прайса пропадут — "
            "заявки и сообщения, которые уже на него ссылаются, не удаляются."
        ) != QMessageBox.Yes:
            return
        self.ctx.db.delete_direction(direction_id)
        self.refresh()

    def _on_toggle_enabled(self, direction_id: int, enabled: bool) -> None:
        self.ctx.db.update_direction(direction_id, enabled=1 if enabled else 0)
        self.refresh()

    def _on_move(self, index: int, delta: int) -> None:
        rows = self.ctx.db.list_directions()
        new_index = index + delta
        if not (0 <= new_index < len(rows)):
            return
        ids = [r["id"] for r in rows]
        ids[index], ids[new_index] = ids[new_index], ids[index]
        self.ctx.db.reorder_directions(ids)
        self.refresh()

    # ---- export / import ------------------------------------------------
    def _on_export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Экспорт направлений", "directions.json", "JSON (*.json)")
        if not path:
            return
        data = self.ctx.db.export_directions()
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
        except OSError as e:
            QMessageBox.warning(self, "Не удалось сохранить", str(e))
            return
        QMessageBox.information(self, "Готово", f"Справочник сохранён: {path}")

    def _on_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Импорт направлений", "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            QMessageBox.warning(
                self, "Не удалось прочитать файл",
                f"Файл повреждён или не в том формате: {e}")
            return

        has_existing = bool(self.ctx.db.list_directions())
        replace = False
        if has_existing:
            choice = QMessageBox.question(
                self, "Уже есть направления",
                "Добавить импортированные к уже существующим, или заменить список целиком?",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                QMessageBox.Yes,
            )
            if choice == QMessageBox.Cancel:
                return
            replace = choice == QMessageBox.No

        added = self.ctx.db.import_directions(data, replace=replace)
        self.refresh()
        if added:
            QMessageBox.information(
                self, "Готово",
                f"Добавлено {added} " + plural(added, "направление", "направления", "направлений") + ".")
        else:
            QMessageBox.information(
                self, "Ничего не добавлено",
                "В файле не нашлось ни одной записи с названием.")
