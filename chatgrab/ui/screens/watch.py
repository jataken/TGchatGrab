"""«Наблюдение» — слова, о появлении которых надо узнать сразу.

Лёгкая половина той же задачи, что решают боты: бот нужен, когда что-то
должно *произойти*; здесь достаточно узнать. Ничего никому не
отправляется — только отметка в списке и уведомление в трее."""
from __future__ import annotations

from PySide6.QtCore import QTimer, QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMenu, QMessageBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..context import AppContext
from ..format import short_dt
from ..util import fire, run_blocking
from ..widgets import button, card, h1, label, muted, plural
from ...core import lead as lead_domain
from .bots.lead_card import LeadCardDialog


class WatchScreen(QWidget):
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
        title_col.addWidget(h1("Наблюдение"))
        self.summary_label = muted("")
        title_col.addWidget(self.summary_label)
        head.addLayout(title_col)
        head.addStretch(1)
        self.mark_btn = button("Отметить всё прочитанным", "secondary")
        self.mark_btn.clicked.connect(self._on_mark_all)
        head.addWidget(self.mark_btn, alignment=Qt.AlignBottom)
        outer.addLayout(head)
        outer.addSpacing(6)

        hint = muted(
            "Слово из этого списка в новом сообщении — и вы сразу об этом узнаете, "
            "без бота и без риска для аккаунта. Приложение ничего никому не пишет: "
            "только отмечает находку и показывает уведомление."
        )
        hint.setWordWrap(True)
        outer.addWidget(hint)
        outer.addSpacing(14)

        # ---- rules ----
        rules_card = card()
        rules_lay = QVBoxLayout(rules_card)
        rules_lay.setContentsMargins(16, 12, 16, 14)
        rules_lay.setSpacing(8)
        rules_lay.addWidget(label("ЗА ЧЕМ СЛЕДИМ", "kicker"))

        add_row = QHBoxLayout()
        add_row.setSpacing(8)
        self.phrase_input = QLineEdit()
        self.phrase_input.setPlaceholderText("слово или фраза, например: куплю глицерин")
        self.phrase_input.returnPressed.connect(self._on_add)
        add_row.addWidget(self.phrase_input, 1)
        self.chat_combo = QComboBox()
        self.chat_combo.setMinimumWidth(180)
        add_row.addWidget(self.chat_combo)
        self.notify_cb = QCheckBox("Уведомлять")
        self.notify_cb.setChecked(True)
        add_row.addWidget(self.notify_cb)
        add_btn = button("Добавить", "primary")
        add_btn.clicked.connect(self._on_add)
        add_row.addWidget(add_btn)
        rules_lay.addLayout(add_row)

        self.rules_table = QTableWidget(0, 4)
        self.rules_table.setHorizontalHeaderLabels(["Фраза", "Где", "Уведомлять", ""])
        self.rules_table.verticalHeader().setVisible(False)
        self.rules_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.rules_table.setShowGrid(False)
        self.rules_table.setMaximumHeight(180)
        self.rules_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        rules_lay.addWidget(self.rules_table)
        outer.addWidget(rules_card)
        outer.addSpacing(14)

        # ---- hits ----
        hits_head = QHBoxLayout()
        hits_head.addWidget(label("НАЙДЕННОЕ", "kicker"))
        hits_head.addStretch(1)
        self.only_unseen_cb = QCheckBox("Только непрочитанные")
        self.only_unseen_cb.toggled.connect(self.refresh)
        hits_head.addWidget(self.only_unseen_cb)
        outer.addLayout(hits_head)
        outer.addSpacing(6)

        self.hits_table = QTableWidget(0, 5)
        self.hits_table.setHorizontalHeaderLabels(["Когда", "Слово", "Чат", "Автор", "Сообщение"])
        self.hits_table.verticalHeader().setVisible(False)
        self.hits_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.hits_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.hits_table.setShowGrid(False)
        self.hits_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.hits_table.cellDoubleClicked.connect(self._on_open_hit)
        self.hits_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.hits_table.customContextMenuRequested.connect(self._on_hits_context_menu)
        outer.addWidget(self.hits_table, 1)

        self.empty_label = muted(
            "Пока ничего не найдено. Добавьте слово выше — и новые сообщения будут "
            "проверяться по нему автоматически."
        )
        self.empty_label.setWordWrap(True)
        outer.addWidget(self.empty_label)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(5000)

    def on_show(self, **kwargs) -> None:
        self._populate_chats()
        self.refresh()

    def _populate_chats(self) -> None:
        current = self.chat_combo.currentData()
        self.chat_combo.blockSignals(True)
        self.chat_combo.clear()
        self.chat_combo.addItem("во всех чатах", None)
        for chat in self.ctx.db.list_chats():
            self.chat_combo.addItem(chat["title"], chat["chat_id"])
        idx = self.chat_combo.findData(current)
        self.chat_combo.setCurrentIndex(max(0, idx))
        self.chat_combo.blockSignals(False)

    # ---- rules -------------------------------------------------------
    def _on_add(self) -> None:
        phrase = self.phrase_input.text().strip()
        if len(phrase) < 3:
            QMessageBox.information(
                self, "Слишком короткая фраза",
                "Укажите хотя бы три символа — иначе совпадать будет почти всё.")
            return
        self.ctx.db.add_watch_rule(phrase, self.chat_combo.currentData(),
                                    self.notify_cb.isChecked())
        self.ctx.watch_service.invalidate()
        self.phrase_input.clear()
        self.refresh()

        # A new phrase should look through what is already collected —
        # otherwise it only ever sees the future, which is rarely what
        # anyone means by "следи за этим словом".
        def done(t) -> None:
            if t.cancelled() or t.exception() is not None:
                return
            found = t.result()
            self.refresh()
            if found:
                QMessageBox.information(
                    self, "Проверено по собранному",
                    f"В уже собранных сообщениях нашлось {found} "
                    + plural(found, "совпадение", "совпадения", "совпадений") + ".")

        task = fire(run_blocking(self.ctx.watch_service.rescan), parent=self,
                    on_error=lambda e: None)
        task.add_done_callback(done)

    def _refresh_rules(self) -> None:
        rules = self.ctx.db.list_watch_rules()
        self.rules_table.setRowCount(len(rules))
        for row, rule in enumerate(rules):
            self.rules_table.setItem(row, 0, QTableWidgetItem(rule["phrase"]))
            chat = self.ctx.db.get_chat(rule["chat_id"]) if rule["chat_id"] else None
            self.rules_table.setItem(row, 1, QTableWidgetItem(
                chat["title"] if chat else "во всех чатах"))

            notify_cb = QCheckBox()
            notify_cb.setChecked(bool(rule["notify"]))
            notify_cb.toggled.connect(
                lambda on, rid=rule["id"]: self._set_rule(rid, notify=1 if on else 0))
            holder = QWidget(); hl = QHBoxLayout(holder)
            hl.setContentsMargins(8, 0, 0, 0); hl.addWidget(notify_cb); hl.addStretch(1)
            self.rules_table.setCellWidget(row, 2, holder)

            del_btn = button("Удалить", "ghost")
            del_btn.clicked.connect(lambda _c, rid=rule["id"]: self._on_delete_rule(rid))
            self.rules_table.setCellWidget(row, 3, del_btn)
            self.rules_table.setRowHeight(row, 36)
        self.rules_table.resizeColumnsToContents()
        header = self.rules_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        # resizeColumnsToContents measures QTableWidgetItem text, not this
        # column's cellWidget — it comes out too narrow for "Удалить"'s own
        # sizeHint, and QPushButton clips centred text in place rather
        # than eliding it, so the button reads as "далить" instead of
        # truncating cleanly.
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        self.rules_table.setColumnWidth(3, 108)

    def _set_rule(self, rule_id: int, **fields) -> None:
        self.ctx.db.set_watch_rule(rule_id, **fields)
        self.ctx.watch_service.invalidate()

    def _on_delete_rule(self, rule_id: int) -> None:
        hits = len([h for h in self.ctx.db.list_watch_hits(limit=10000)
                    if h["rule_id"] == rule_id])
        if QMessageBox.question(
            self, "Убрать из наблюдения",
            f"Перестать следить за этой фразой? Заодно исчезнут {hits} "
            + plural(hits, "найденная запись", "найденные записи", "найденных записей")
            + ". Сами сообщения останутся в базе."
        ) != QMessageBox.Yes:
            return
        self.ctx.db.delete_watch_rule(rule_id)
        self.ctx.watch_service.invalidate()
        self.refresh()

    # ---- hits --------------------------------------------------------
    def refresh(self) -> None:
        self._refresh_rules()
        hits = self.ctx.db.list_watch_hits(unseen_only=self.only_unseen_cb.isChecked())
        unseen = self.ctx.db.unseen_watch_count()
        rules_n = len(self.ctx.db.list_watch_rules())
        if rules_n:
            summary = (f"{rules_n} " + plural(rules_n, "фраза", "фразы", "фраз")
                       + f" · найдено {len(hits)}"
                       + (f", непрочитано {unseen}" if unseen else ""))
        else:
            summary = "ни одной фразы ещё не задано"
        self.summary_label.setText(summary)
        self.mark_btn.setEnabled(unseen > 0)
        self.empty_label.setVisible(not hits)
        self.hits_table.setVisible(bool(hits))

        self.hits_table.setRowCount(len(hits))
        for row, hit in enumerate(hits):
            when = short_dt(hit["date"] or hit["matched_at"])
            item = QTableWidgetItem(when)
            item.setData(Qt.UserRole, hit["id"])
            self.hits_table.setItem(row, 0, item)
            self.hits_table.setItem(row, 1, QTableWidgetItem(hit["phrase"]))
            self.hits_table.setItem(row, 2, QTableWidgetItem(hit["chat_title"] or "—"))
            author = hit["sender_display_name"] or ""
            if hit["sender_username"]:
                author += f" @{hit['sender_username']}"
            self.hits_table.setItem(row, 3, QTableWidgetItem(author.strip() or "—"))
            text = (hit["text"] or "").replace("\n", " ")
            text_item = QTableWidgetItem(text[:200] if text else "(сообщение удалено из базы)")
            if not hit["seen"]:
                # Unread rows read brighter; everything else stays muted.
                text_item.setForeground(Qt.white)
            self.hits_table.setItem(row, 4, text_item)
            self.hits_table.setRowHeight(row, 34)
        self.hits_table.resizeColumnsToContents()
        self.hits_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)

    def _on_open_hit(self, row: int, _col: int) -> None:
        item = self.hits_table.item(row, 0)
        if item is None:
            return
        hit_id = item.data(Qt.UserRole)
        self.ctx.db.mark_watch_hits_seen([hit_id])
        hits = self.ctx.db.list_watch_hits(limit=10000)
        hit = next((h for h in hits if h["id"] == hit_id), None)
        if hit and hit["link"]:
            QDesktopServices.openUrl(QUrl(hit["link"]))
        self.refresh()

    def _on_mark_all(self) -> None:
        self.ctx.db.mark_watch_hits_seen()
        self.refresh()

    def _on_hits_context_menu(self, pos) -> None:
        row = self.hits_table.rowAt(pos.y())
        item = self.hits_table.item(row, 0) if row >= 0 else None
        hit_id = item.data(Qt.UserRole) if item else None
        if hit_id is None:
            return
        menu = QMenu(self)
        create_lead = menu.addAction("Создать лид")
        chosen = menu.exec(self.hits_table.viewport().mapToGlobal(pos))
        if chosen == create_lead:
            self._on_create_lead(hit_id)

    def _on_create_lead(self, hit_id: int) -> None:
        hits = self.ctx.db.list_watch_hits(limit=10000)
        hit = next((h for h in hits if h["id"] == hit_id), None)
        if hit is None:
            return
        lead_id = self.ctx.db.add_lead(
            None, None, {"text": hit["text"] or ""}, status=lead_domain.NEW,
            tg_user_id=hit["sender_id"], username=hit["sender_username"],
            display_name=hit["sender_display_name"],
            source_chat_id=hit["chat_id"], source_type=lead_domain.SOURCE_TYPE_CHAT,
            event_source=lead_domain.EVENT_SOURCE_MANUAL,
        )
        LeadCardDialog(self.ctx, lead_id, parent=self).exec()
        self.refresh()
