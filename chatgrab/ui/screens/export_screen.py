from __future__ import annotations

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from ..context import AppContext
from ..format import short_dt
from ..util import fire, run_blocking
from ..widgets import button, card, h1, muted, plural
from ...services.export_service import DEFAULT_TOKEN_LIMIT, ExportParams


def _pill_button(text: str, hint: str = "") -> QPushButton:
    btn = QPushButton(f"{text}\n{hint}" if hint else text)
    btn.setCheckable(True)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setStyleSheet(
        "QPushButton { text-align: left; padding: 10px 13px; border-radius: 9px; "
        "background: rgba(233,233,237,8); border: 1px solid rgba(233,233,237,20); font-size: 13px; }"
        "QPushButton:checked { background: rgba(145,132,217,30); border: 1px solid #9184d9; color: #b5abfc; }"
    )
    return btn


class ExportScreen(QWidget):
    def __init__(self, ctx: AppContext, navigate):
        super().__init__()
        self.ctx = ctx
        self.navigate = navigate
        self.chat_checks: dict[int, QCheckBox] = {}
        self._search_filters: dict = {}

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        scroll.setWidget(container)
        wrap = QVBoxLayout(self)
        wrap.setContentsMargins(0, 0, 0, 0)
        wrap.addWidget(scroll)

        outer = QVBoxLayout(container)
        outer.setContentsMargins(40, 28, 40, 32)
        outer.addWidget(h1("Выгрузка в файл"))
        outer.addWidget(muted(
            "Готовый файл открывается в Claude для разбора запросов, объёмов и цен. "
            "Фотографии остаются рядом с базой — в выгрузке будет путь к файлу."
        ))
        outer.addSpacing(18)

        cols = QHBoxLayout()
        left = QVBoxLayout()
        left.setSpacing(18)
        right = QVBoxLayout()
        cols.addLayout(left, 11)
        cols.addLayout(right, 9)
        outer.addLayout(cols)

        # ---- chats ------------------------------------------------------
        left.addWidget(muted("КАКИЕ ЧАТЫ ВЫГРУЗИТЬ"))
        self.chat_list_widget = QWidget()
        self.chat_list_lay = QVBoxLayout(self.chat_list_widget)
        self.chat_list_lay.setContentsMargins(0, 0, 0, 0)
        self.chat_list_lay.setSpacing(4)
        left.addWidget(self.chat_list_widget)
        sel_row = QHBoxLayout()
        all_btn = button("Выбрать все", "ghost")
        all_btn.clicked.connect(self._select_all)
        sel_row.addWidget(all_btn)
        none_btn = button("Снять выбор", "ghost")
        none_btn.clicked.connect(self._select_none)
        sel_row.addWidget(none_btn)
        sel_row.addStretch(1)
        left.addLayout(sel_row)

        # ---- format ------------------------------------------------------
        left.addWidget(muted("ФОРМАТ"))
        fmt_row = QHBoxLayout()
        self.fmt_group = QButtonGroup(self)
        self.fmt_group.setExclusive(True)
        self.fmt_buttons: dict[str, QPushButton] = {}
        for key, label_, hint in [("xlsx", "Excel (.xlsx)", "таблица по колонкам"),
                                   ("jsonl", "JSONL", "по записи в строке"),
                                   ("markdown", "Markdown", "дайджест по дням")]:
            btn = _pill_button(label_, hint)
            btn.clicked.connect(self._on_settings_changed)
            self.fmt_group.addButton(btn)
            self.fmt_buttons[key] = btn
            fmt_row.addWidget(btn)
        self.fmt_buttons["xlsx"].setChecked(True)
        left.addLayout(fmt_row)

        # ---- dates ------------------------------------------------------
        date_row = QHBoxLayout()
        from_col = QVBoxLayout()
        from_col.addWidget(muted("Период — с"))
        self.date_from = QLineEdit()
        self.date_from.setPlaceholderText("ГГГГ-ММ-ДД")
        from_col.addWidget(self.date_from)
        to_col = QVBoxLayout()
        to_col.addWidget(muted("Период — по"))
        self.date_to = QLineEdit()
        self.date_to.setPlaceholderText("ГГГГ-ММ-ДД")
        to_col.addWidget(self.date_to)
        date_row.addLayout(from_col)
        date_row.addLayout(to_col)
        left.addLayout(date_row)

        # ---- toggles ------------------------------------------------------
        self.merge_cb = QCheckBox("Объединить выбранные чаты в один файл")
        left.addWidget(self.merge_cb)
        self.incremental_cb = QCheckBox("Только новое с прошлой выгрузки")
        left.addWidget(self.incremental_cb)
        self.zip_cb = QCheckBox("Приложить папку с медиафайлами (zip рядом с выгрузкой)")
        self.zip_cb.setChecked(True)
        left.addWidget(self.zip_cb)
        self.include_hidden_cb = QCheckBox("Включить скрытые правилами игнора записи")
        left.addWidget(self.include_hidden_cb)
        self.unique_only_cb = QCheckBox("Только уникальные — без повторов одного и того же текста")
        left.addWidget(self.unique_only_cb)
        self.repeats_hint = muted("")
        self.repeats_hint.setWordWrap(True)
        left.addWidget(self.repeats_hint)
        for cb in (self.merge_cb, self.incremental_cb, self.zip_cb, self.include_hidden_cb,
                    self.unique_only_cb):
            cb.toggled.connect(self._on_settings_changed)

        # ---- split mode ------------------------------------------------------
        left.addWidget(muted("КАК ДЕЛИТЬ ФАЙЛЫ"))
        split_row = QHBoxLayout()
        self.split_group = QButtonGroup(self)
        self.split_buttons: dict[str, QPushButton] = {}
        for key, label_, hint in [("tokens", "По размеру для Claude", "части ≤ лимита токенов"),
                                   ("month", "По месяцам", "отдельный файл на месяц"),
                                   ("none", "Одним куском", "весь период в файле")]:
            btn = _pill_button(label_, hint)
            btn.clicked.connect(self._on_settings_changed)
            self.split_group.addButton(btn)
            self.split_buttons[key] = btn
            split_row.addWidget(btn)
        self.split_buttons["tokens"].setChecked(True)
        left.addLayout(split_row)

        token_row = QHBoxLayout()
        token_row.addWidget(muted("Лимит токенов на файл"))
        self.token_limit_spin = QSpinBox()
        self.token_limit_spin.setRange(5_000, 1_000_000)
        self.token_limit_spin.setSingleStep(5_000)
        self.token_limit_spin.setValue(DEFAULT_TOKEN_LIMIT)
        self.token_limit_spin.valueChanged.connect(self._on_settings_changed)
        token_row.addWidget(self.token_limit_spin)
        token_row.addStretch(1)
        left.addLayout(token_row)

        # ---- folder ------------------------------------------------------
        left.addWidget(muted("Папка сохранения"))
        folder_row = QHBoxLayout()
        self.folder_input = QLineEdit(str(ctx.paths.exports_dir))
        folder_row.addWidget(self.folder_input)
        browse_btn = button("Обзор…", "secondary")
        browse_btn.clicked.connect(self._browse_folder)
        folder_row.addWidget(browse_btn)
        left.addLayout(folder_row)

        # ---- presets ------------------------------------------------------
        left.addWidget(muted("ПРЕСЕТЫ"))
        preset_row = QHBoxLayout()
        self.preset_combo = QComboBox()
        preset_row.addWidget(self.preset_combo, 1)
        load_preset_btn = button("Загрузить", "secondary")
        load_preset_btn.clicked.connect(self._load_preset)
        preset_row.addWidget(load_preset_btn)
        save_preset_btn = button("Сохранить как…", "secondary")
        save_preset_btn.clicked.connect(self._save_preset)
        preset_row.addWidget(save_preset_btn)
        left.addLayout(preset_row)

        run_row = QHBoxLayout()
        self.run_btn = button("Экспортировать", "primary")
        self.run_btn.clicked.connect(self._run_export)
        run_row.addWidget(self.run_btn)
        self.estimate_label = muted("")
        run_row.addWidget(self.estimate_label)
        run_row.addStretch(1)
        left.addLayout(run_row)
        left.addStretch(1)

        # ---- right column: preview ------------------------------------------
        preview_card = card()
        prev_lay = QVBoxLayout(preview_card)
        prev_lay.setContentsMargins(16, 16, 16, 16)
        kicker = muted("ЧТО ПОЛУЧИТСЯ")
        kicker.setStyleSheet("color: #9184d9; font-size: 11px;")
        prev_lay.addWidget(kicker)
        self.token_line_label = QLabel("—")
        self.token_line_label.setStyleSheet("font-size: 15px;")
        prev_lay.addWidget(self.token_line_label)
        self.file_preview_list = QVBoxLayout()
        prev_lay.addLayout(self.file_preview_list)
        self.export_note_label = QLabel("")
        self.export_note_label.setWordWrap(True)
        self.export_note_label.setProperty("class", "muted")
        prev_lay.addWidget(self.export_note_label)
        self.done_label = QLabel("")
        self.done_label.setWordWrap(True)
        self.done_label.setStyleSheet(
            "background: rgba(145,132,217,20); color: #e7e5fe; border-radius: 8px; "
            "padding: 10px 12px; font-size: 12.5px;"
        )
        self.done_label.hide()
        prev_lay.addWidget(self.done_label)
        right.addWidget(preview_card)

        right.addWidget(muted("ЖУРНАЛ ВЫГРУЗОК"))
        self.log_list = QListWidget()
        self.log_list.setMaximumHeight(220)
        self.log_list.itemDoubleClicked.connect(self._repeat_from_log)
        right.addWidget(self.log_list)
        right.addWidget(muted("Двойной клик по записи — повторить ту же выгрузку."))
        right.addStretch(1)

        for w in (self.date_from, self.date_to, self.folder_input):
            w.textChanged.connect(self._on_settings_changed)

    # ---- lifecycle -----------------------------------------------------
    def on_show(self, search_filters: dict | None = None, **kwargs) -> None:
        self._populate_chats()
        self._populate_presets()
        self._populate_log()
        if search_filters:
            self._search_filters = search_filters
        self._update_estimate()

    def _populate_chats(self) -> None:
        existing = {cid: cb.isChecked() for cid, cb in self.chat_checks.items()}
        for cb in self.chat_checks.values():
            cb.setParent(None)
        self.chat_checks.clear()
        for chat in self.ctx.db.list_chats():
            count = self.ctx.db.message_count(chat["chat_id"])
            cb = QCheckBox(f"{chat['title']}   ·   {count:,}".replace(",", " "))
            cb.setChecked(existing.get(chat["chat_id"], True))
            cb.toggled.connect(self._on_settings_changed)
            self.chat_list_lay.addWidget(cb)
            self.chat_checks[chat["chat_id"]] = cb

    def _populate_presets(self) -> None:
        self.preset_combo.clear()
        for row in self.ctx.db.list_presets():
            self.preset_combo.addItem(row["name"])

    def _populate_log(self) -> None:
        self.log_list.clear()
        for row in self.ctx.db.list_export_log(limit=20):
            when = short_dt(row["created_at"])
            item = QListWidgetItem(f"{when} · {row['format']} · {row['split_mode']}")
            item.setData(Qt.UserRole, row["id"])
            self.log_list.addItem(item)

    # ---- state -----------------------------------------------------
    def _selected_chat_ids(self) -> list[int]:
        return [cid for cid, cb in self.chat_checks.items() if cb.isChecked()]

    def _select_all(self) -> None:
        for cb in self.chat_checks.values():
            cb.setChecked(True)

    def _select_none(self) -> None:
        for cb in self.chat_checks.values():
            cb.setChecked(False)

    def _current_format(self) -> str:
        for key, btn in self.fmt_buttons.items():
            if btn.isChecked():
                return key
        return "jsonl"

    def _current_split(self) -> str:
        for key, btn in self.split_buttons.items():
            if btn.isChecked():
                return key
        return "tokens"

    def build_params(self) -> ExportParams:
        filters = self._search_filters
        return ExportParams(
            chat_ids=self._selected_chat_ids(),
            format=self._current_format(),
            merge=self.merge_cb.isChecked(),
            split_mode=self._current_split(),
            token_limit=self.token_limit_spin.value(),
            date_from=self.date_from.text().strip() or None,
            date_to=self.date_to.text().strip() or None,
            incremental=self.incremental_cb.isChecked(),
            zip_photos=self.zip_cb.isChecked(),
            include_hidden=self.include_hidden_cb.isChecked(),
            unique_only=self.unique_only_cb.isChecked(),
            folder=self.folder_input.text().strip(),
            query=filters.get("query", ""),
            author=filters.get("author", ""),
            photos_only=filters.get("photos_only", False),
            forwards_only=filters.get("forwards_only", False),
            replies_only=filters.get("replies_only", False),
            markdown_header=self.ctx.db.get_setting("markdown_header", ""),
        )

    def _on_settings_changed(self) -> None:
        self.done_label.hide()
        self._update_estimate()

    def _update_repeats_hint(self, chat_ids: list[int]) -> None:
        """Say what «только уникальные» would actually drop, so the choice
        isn't made blind."""
        if not chat_ids:
            self.repeats_hint.setText("")
            return
        summary = self.ctx.db.repeat_summary(chat_ids)
        repeats = summary["repeats"]
        if not repeats:
            self.repeats_hint.setText("Повторов среди выбранного не найдено.")
            return
        self.repeats_hint.setText(
            f"В выбранном {repeats} " + plural(repeats, "повтор", "повтора", "повторов")
            + f" в {summary['groups']} " + plural(summary["groups"], "тексте", "текстах", "текстах")
            + " — столько записей уйдёт из выгрузки с этим флажком. "
            "Первое появление каждого текста остаётся."
        )

    def _update_estimate(self) -> None:
        params = self.build_params()
        self._update_repeats_hint(params.chat_ids)
        if not params.chat_ids:
            self.token_line_label.setText("—")
            self._clear_preview()
            self.estimate_label.setText("Ничего не выбрано")
            return
        est = self.ctx.export_service.estimate(params)
        parts_word = "файл" if est.file_count == 1 else ("файла" if est.file_count < 5 else "файлов")
        self.token_line_label.setText(f"≈{est.token_count // 1000}k токенов · {est.file_count} {parts_word} под контекст Claude")
        self._clear_preview()
        for name in est.file_names[:6]:
            row = QHBoxLayout()
            lbl = QLabel(f"▸ {name}")
            lbl.setStyleSheet("font-family: Consolas, monospace; font-size: 12px; color: #d6d6db;")
            row.addWidget(lbl)
            row.addStretch(1)
            w = QWidget()
            w.setLayout(row)
            self.file_preview_list.addWidget(w)
        if len(est.file_names) > 6:
            self.file_preview_list.addWidget(muted(f"… ещё {len(est.file_names) - 6} файлов"))
        self.estimate_label.setText(
            f"{est.row_count:,} сообщений · {est.file_count} файлов".replace(",", " ")
        )
        cfg = self.ctx.config
        media_enabled = cfg.photos_enabled or cfg.videos_enabled or cfg.voice_enabled or cfg.documents_enabled
        self.export_note_label.setText(
            "Медиафайлы не встраиваются в файл: в каждой записи будет путь вида "
            "photos|videos|voice|documents/<чат>/<номер>, а сами файлы остаются рядом с базой."
            if media_enabled else
            "Скачивание медиафайлов выключено в Настройках — в выгрузке будет только текст и сведения о сообщении."
        )

    def _clear_preview(self) -> None:
        while self.file_preview_list.count():
            item = self.file_preview_list.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _browse_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Папка сохранения", self.folder_input.text())
        if d:
            self.folder_input.setText(d)

    def _run_export(self) -> None:
        params = self.build_params()
        if not params.chat_ids:
            QMessageBox.information(self, "Экспорт", "Выберите хотя бы один чат.")
            return
        self.run_btn.setEnabled(False)

        # Off the shared qasync loop, via a worker thread — a large export
        # (openpyxl writing thousands of rows) would otherwise freeze the
        # whole UI *and* every bot's message handling until it finished.
        def on_error(e):
            self.run_btn.setEnabled(True)
            QMessageBox.warning(self, "Не получилось", str(e))

        task = fire(run_blocking(self.ctx.export_service.run, params), parent=self, on_error=on_error)

        def _apply(t):
            self.run_btn.setEnabled(True)
            if t.cancelled() or t.exception() is not None:
                return
            result = t.result()
            self.done_label.setText(
                f"Готово. {result.row_count} сообщений сохранено в {len(result.output_paths)} файл(ов) — "
                f'<a href="#">открыть папку</a>.'
            )
            self.done_label.linkActivated.connect(lambda _: self._open_folder())
            self.done_label.show()
            self._populate_log()

        task.add_done_callback(_apply)

    def _open_folder(self) -> None:
        folder = self.folder_input.text().strip() or str(self.ctx.paths.exports_dir)
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def _save_preset(self) -> None:
        name, ok = QInputDialog.getText(self, "Сохранить пресет", "Имя пресета:")
        if ok and name.strip():
            self.ctx.export_service.save_preset(name.strip(), self.build_params())
            self._populate_presets()

    def _load_preset(self) -> None:
        name = self.preset_combo.currentText()
        if not name:
            return
        params = self.ctx.export_service.load_preset(name)
        if not params:
            return
        self._apply_params(params)

    def _apply_params(self, params: ExportParams) -> None:
        for cid, cb in self.chat_checks.items():
            cb.setChecked(cid in params.chat_ids)
        # "csv" was retired in favor of "xlsx" — fall back gracefully so
        # an old export_log entry / preset from before that change can
        # still be repeated instead of crashing on an unknown key.
        fmt_key = params.format if params.format in self.fmt_buttons else "xlsx"
        self.fmt_buttons[fmt_key].setChecked(True)
        self.merge_cb.setChecked(params.merge)
        split_key = params.split_mode if params.split_mode in self.split_buttons else "tokens"
        self.split_buttons[split_key].setChecked(True)
        self.token_limit_spin.setValue(params.token_limit)
        self.date_from.setText(params.date_from or "")
        self.date_to.setText(params.date_to or "")
        self.incremental_cb.setChecked(params.incremental)
        self.zip_cb.setChecked(params.zip_photos)
        self.include_hidden_cb.setChecked(params.include_hidden)
        self.unique_only_cb.setChecked(getattr(params, 'unique_only', False))
        if params.folder:
            self.folder_input.setText(params.folder)
        self._update_estimate()

    def _repeat_from_log(self, item: QListWidgetItem) -> None:
        log_id = item.data(Qt.UserRole)
        rows = [r for r in self.ctx.db.list_export_log(limit=200) if r["id"] == log_id]
        if not rows:
            return
        row = rows[0]
        import json
        params = ExportParams(
            chat_ids=json.loads(row["chat_ids"]), format=row["format"],
            merge=bool(row["merge"]), split_mode=row["split_mode"],
            token_limit=row["token_limit"] or DEFAULT_TOKEN_LIMIT,
            date_from=row["date_from"], date_to=row["date_to"],
            incremental=bool(row["incremental"]), zip_photos=bool(row["zip_photos"]),
            include_hidden=bool(row["include_hidden"]), folder=self.folder_input.text().strip(),
        )
        self._apply_params(params)
        self._run_export()
