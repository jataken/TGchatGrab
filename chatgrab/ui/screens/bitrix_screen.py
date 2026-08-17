"""Bitrix24: webhook, mapping, and the sync journal — split out of
ui/screens/settings.py in С7 (that screen was already flagged there as a
temporary home for the webhook card added in С6). Grouped as one screen
because all four pieces answer the same question — "what happens when a
lead is sent to Bitrix24" — rather than being general app settings.
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QHeaderView, QMessageBox, QScrollArea, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..context import AppContext
from ..format import short_dt
from ..util import fire
from ..widgets import FieldRow, button, card, h1, muted
from ...core import lead as lead_domain
from ...integrations import bitrix
from ...integrations.bitrix import BitrixClient, BitrixError


def _populate_entity_combo(combo: QComboBox, items: list[dict], current_value: str | None) -> None:
    """Fills a mapping dropdown from crm.status.list's result, keeping the
    currently-saved value selectable even if it wasn't in the fetched
    list (portal not reachable yet, or the value was set by hand)."""
    combo.blockSignals(True)
    combo.clear()
    combo.addItem("— не задано —", "")
    seen = set()
    for it in items:
        code = it.get("STATUS_ID") or ""
        if not code:
            continue
        name = it.get("NAME") or code
        combo.addItem(f"{name} ({code})", code)
        seen.add(code)
    if current_value and current_value not in seen:
        combo.addItem(f"{current_value} (текущее значение)", current_value)
    idx = combo.findData(current_value or "")
    combo.setCurrentIndex(idx if idx >= 0 else 0)
    combo.blockSignals(False)


class BitrixScreen(QWidget):
    def __init__(self, ctx: AppContext, navigate):
        super().__init__()
        self.ctx = ctx
        self.navigate = navigate
        self._status_combos: dict[str, QComboBox] = {}
        self._direction_combos: dict[int, QComboBox] = {}

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        scroll.setWidget(container)
        wrap = QVBoxLayout(self)
        wrap.setContentsMargins(0, 0, 0, 0)
        wrap.addWidget(scroll)

        outer = QVBoxLayout(container)
        outer.setContentsMargins(40, 28, 40, 32)
        outer.addWidget(h1("Bitrix24"))
        outer.addWidget(muted(
            "Подключение к порталу, соответствие статусов и источников, и что и когда "
            "уходит в CRM автоматически."
        ))
        outer.addSpacing(18)

        # ---- webhook -----------------------------------------------------
        wh_card = card()
        wh_lay = QVBoxLayout(wh_card)
        wh_lay.setContentsMargins(16, 14, 16, 14)
        wh_lay.addWidget(muted("ПОДКЛЮЧЕНИЕ"))
        wh_hint = muted(
            "Входящий вебхук портала (Приложения → Разработчикам → Другое → Входящий "
            "вебхук, с правами на CRM). Вставьте ссылку целиком, включая /rest/…/…/ на конце."
        )
        wh_hint.setWordWrap(True)
        wh_lay.addWidget(wh_hint)

        self.webhook_field = FieldRow(
            "Ссылка вебхука",
            placeholder="https://portal.bitrix24.ru/rest/1/xxxxxxxxxxxxxxxx/",
            password=True,
        )
        self.webhook_field.set_text(bitrix.get_webhook_url(self.ctx.db, self.ctx.security) or "")
        wh_lay.addWidget(self.webhook_field)

        self.webhook_status = muted("")
        self.webhook_status.setWordWrap(True)
        wh_lay.addWidget(self.webhook_status)

        wh_btn_row = QHBoxLayout()
        save_webhook_btn = button("Сохранить", "primary")
        save_webhook_btn.clicked.connect(self._on_save_webhook)
        wh_btn_row.addWidget(save_webhook_btn)
        self.test_webhook_btn = button("Проверить подключение", "secondary")
        self.test_webhook_btn.clicked.connect(self._on_test_webhook)
        wh_btn_row.addWidget(self.test_webhook_btn)
        wh_btn_row.addStretch(1)
        wh_lay.addLayout(wh_btn_row)
        outer.addWidget(wh_card)
        outer.addSpacing(20)

        # ---- auto-send policy ----------------------------------------------
        policy_card = card()
        policy_lay = QVBoxLayout(policy_card)
        policy_lay.setContentsMargins(16, 14, 16, 14)
        policy_lay.addWidget(muted("КОГДА ОТПРАВЛЯТЬ"))
        policy_hint = muted(
            "Кнопка «Отправить в Битрикс24» на карточке лида работает всегда, независимо "
            "от этой настройки — она только про автоматическую постановку в очередь."
        )
        policy_hint.setWordWrap(True)
        policy_lay.addWidget(policy_hint)
        self.policy_combo = QComboBox()
        for code in bitrix.AUTO_SEND_POLICIES:
            self.policy_combo.addItem(bitrix.AUTO_SEND_POLICY_LABELS[code], code)
        self.policy_combo.currentIndexChanged.connect(self._on_policy_changed)
        policy_lay.addWidget(self.policy_combo)
        outer.addWidget(policy_card)
        outer.addSpacing(20)

        # ---- status mapping ------------------------------------------------
        status_card = card()
        status_lay = QVBoxLayout(status_card)
        status_lay.setContentsMargins(16, 14, 16, 14)
        status_header = QHBoxLayout()
        status_header.addWidget(muted("СТАТУСЫ: ВОРОНКА CHATGRAB → СТАДИЯ ЛИДА В BITRIX24"))
        status_header.addStretch(1)
        load_statuses_btn = button("Загрузить статусы из Bitrix24", "secondary")
        load_statuses_btn.clicked.connect(self._on_load_statuses)
        status_header.addWidget(load_statuses_btn)
        status_lay.addLayout(status_header)

        for status_code in lead_domain.ALL_STATUSES:
            row = QHBoxLayout()
            label = muted(lead_domain.label_for_status(status_code))
            label.setFixedWidth(160)
            row.addWidget(label)
            combo = QComboBox()
            _populate_entity_combo(combo, [], None)
            row.addWidget(combo, 1)
            status_lay.addLayout(row)
            self._status_combos[status_code] = combo

        save_status_btn = button("Сохранить соответствие статусов", "primary")
        save_status_btn.clicked.connect(self._on_save_status_map)
        status_lay.addWidget(save_status_btn)
        outer.addWidget(status_card)
        outer.addSpacing(20)

        # ---- direction -> source mapping ------------------------------------
        source_card = card()
        source_lay = QVBoxLayout(source_card)
        source_lay.setContentsMargins(16, 14, 16, 14)
        source_header = QHBoxLayout()
        source_header.addWidget(muted("ИСТОЧНИКИ: НАПРАВЛЕНИЕ → SOURCE_ID В BITRIX24"))
        source_header.addStretch(1)
        load_sources_btn = button("Загрузить источники из Bitrix24", "secondary")
        load_sources_btn.clicked.connect(self._on_load_sources)
        source_header.addWidget(load_sources_btn)
        source_lay.addLayout(source_header)

        self.source_rows_box = QVBoxLayout()
        source_lay.addLayout(self.source_rows_box)
        self.source_empty = muted("Направлений пока нет — их можно добавить на экране «Направления».")
        source_lay.addWidget(self.source_empty)

        save_source_btn = button("Сохранить соответствие источников", "primary")
        save_source_btn.clicked.connect(self._on_save_source_map)
        source_lay.addWidget(save_source_btn)
        outer.addWidget(source_card)
        outer.addSpacing(20)

        # ---- sync journal ----------------------------------------------------
        journal_card = card()
        journal_lay = QVBoxLayout(journal_card)
        journal_lay.setContentsMargins(16, 14, 16, 14)
        journal_header = QHBoxLayout()
        journal_header.addWidget(muted("ЖУРНАЛ СИНХРОНИЗАЦИИ"))
        journal_header.addStretch(1)
        refresh_journal_btn = button("Обновить", "ghost")
        refresh_journal_btn.clicked.connect(self._refresh_journal)
        journal_header.addWidget(refresh_journal_btn)
        journal_lay.addLayout(journal_header)

        self.journal_empty = muted("Пока ничего не отправлялось в Bitrix24.")
        journal_lay.addWidget(self.journal_empty)
        self.journal_table = QTableWidget(0, 4)
        self.journal_table.setHorizontalHeaderLabels(["Время", "Заявка", "Результат", "Подробности"])
        self.journal_table.verticalHeader().setVisible(False)
        self.journal_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.journal_table.setShowGrid(False)
        self.journal_table.setMaximumHeight(320)
        self.journal_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        journal_lay.addWidget(self.journal_table)
        outer.addWidget(journal_card)

        self._refresh_webhook_status()

    # ---- lifecycle ---------------------------------------------------------
    def on_show(self) -> None:
        self._refresh_webhook_status()
        self._load_saved_status_map()
        self._rebuild_source_rows()
        policy = bitrix.get_auto_send_policy(self.ctx.db)
        idx = self.policy_combo.findData(policy)
        self.policy_combo.blockSignals(True)
        self.policy_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.policy_combo.blockSignals(False)
        self._refresh_journal()

    # ---- webhook -------------------------------------------------------------
    def _refresh_webhook_status(self) -> None:
        if bitrix.get_webhook_url(self.ctx.db, self.ctx.security):
            self.webhook_status.setText(
                "Вебхук сохранён. Заявки уходят по кнопке «Отправить в Битрикс24» на "
                "карточке лида и, в зависимости от настройки «Когда отправлять» выше, "
                "автоматически; отправка досылается сама, пока соединения нет.")
        else:
            self.webhook_status.setText("Вебхук не задан — синхронизация с Bitrix24 выключена.")

    def _on_save_webhook(self) -> None:
        bitrix.set_webhook_url(self.ctx.db, self.ctx.security, self.webhook_field.text())
        self._refresh_webhook_status()
        QMessageBox.information(self, "Bitrix24", "Сохранено.")

    def _on_test_webhook(self) -> None:
        url = self.webhook_field.text().strip()
        if not url:
            QMessageBox.warning(self, "Bitrix24", "Сначала укажите ссылку вебхука.")
            return
        self.test_webhook_btn.setEnabled(False)

        async def _check() -> str:
            try:
                return await BitrixClient(url).ping()
            except BitrixError as e:
                raise RuntimeError(str(e)) from e

        def on_error(e):
            self.test_webhook_btn.setEnabled(True)
            QMessageBox.warning(self, "Не получилось подключиться", str(e))

        task = fire(_check(), parent=self, on_error=on_error)

        def _apply(t):
            self.test_webhook_btn.setEnabled(True)
            if t.cancelled() or t.exception() is not None:
                return
            QMessageBox.information(self, "Bitrix24", t.result())

        task.add_done_callback(_apply)

    # ---- auto-send policy ------------------------------------------------
    def _on_policy_changed(self, _index: int) -> None:
        policy = self.policy_combo.currentData()
        if policy:
            bitrix.set_auto_send_policy(self.ctx.db, policy)

    # ---- status mapping ----------------------------------------------------
    def _load_saved_status_map(self) -> None:
        mapping = bitrix.get_status_map(self.ctx.db)
        for status_code, combo in self._status_combos.items():
            _populate_entity_combo(combo, [], mapping.get(status_code))

    def _on_load_statuses(self) -> None:
        self._fetch_and_populate("STATUS", self._status_combos, "статусов")

    def _on_save_status_map(self) -> None:
        mapping = {code: combo.currentData() for code, combo in self._status_combos.items()}
        bitrix.set_status_map(self.ctx.db, mapping)
        QMessageBox.information(self, "Bitrix24", "Соответствие статусов сохранено.")

    # ---- direction -> source mapping --------------------------------------
    def _rebuild_source_rows(self) -> None:
        while self.source_rows_box.count():
            item = self.source_rows_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
            elif item.layout() is not None:
                self._clear_layout(item.layout())
        self._direction_combos.clear()

        directions = self.ctx.db.list_directions()
        self.source_empty.setVisible(not directions)
        for direction in directions:
            row = QHBoxLayout()
            label = muted(direction["name"])
            label.setFixedWidth(160)
            row.addWidget(label)
            combo = QComboBox()
            _populate_entity_combo(combo, [], direction["crm_source_id"])
            row.addWidget(combo, 1)
            self.source_rows_box.addLayout(row)
            self._direction_combos[direction["id"]] = combo

    @staticmethod
    def _clear_layout(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _on_load_sources(self) -> None:
        self._fetch_and_populate("SOURCE", self._direction_combos, "источников")

    def _on_save_source_map(self) -> None:
        for direction_id, combo in self._direction_combos.items():
            self.ctx.db.update_direction(direction_id, crm_source_id=combo.currentData() or None)
        QMessageBox.information(self, "Bitrix24", "Соответствие источников сохранено.")

    # ---- shared: fetch crm.status.list and refill a set of combos --------
    def _fetch_and_populate(self, entity_id: str, combos: dict, noun: str) -> None:
        url = bitrix.get_webhook_url(self.ctx.db, self.ctx.security)
        if not url:
            QMessageBox.warning(self, "Bitrix24", "Сначала сохраните вебхук.")
            return

        async def _load() -> list[dict]:
            try:
                return await BitrixClient(url).list_statuses(entity_id)
            except BitrixError as e:
                raise RuntimeError(str(e)) from e

        def on_error(e):
            QMessageBox.warning(self, f"Не удалось загрузить список {noun}", str(e))

        task = fire(_load(), parent=self, on_error=on_error)

        def _apply(t):
            if t.cancelled() or t.exception() is not None:
                return
            items = t.result()
            for combo in combos.values():
                current = combo.currentData()
                _populate_entity_combo(combo, items, current)

        task.add_done_callback(_apply)

    # ---- journal -------------------------------------------------------------
    def _refresh_journal(self) -> None:
        rows = self.ctx.db.crm_sync_journal(50)
        self.journal_empty.setVisible(not rows)
        self.journal_table.setVisible(bool(rows))
        self.journal_table.setRowCount(len(rows))
        outcome_labels = {"ok": "ушла", "pending": "ждёт отправки", "retrying": "повторная попытка"}
        for i, r in enumerate(rows):
            lead = self.ctx.db.get_lead(r["lead_id"])
            handle = "заявка удалена"
            if lead is not None:
                handle = lead["display_name"] or lead["username"] or f"заявка №{r['lead_id']}"
            self.journal_table.setItem(i, 0, QTableWidgetItem(short_dt(r["at"])))
            self.journal_table.setItem(i, 1, QTableWidgetItem(handle))
            self.journal_table.setItem(i, 2, QTableWidgetItem(outcome_labels.get(r["outcome"], r["outcome"])))
            self.journal_table.setItem(i, 3, QTableWidgetItem(r["detail"] or ""))
        self.journal_table.resizeColumnsToContents()
        self.journal_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
