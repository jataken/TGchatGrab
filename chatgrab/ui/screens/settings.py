from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QGridLayout, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QScrollArea, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..context import AppContext
from .. import tray
from ..format import short_dt
from ..util import fire
from ... import __version__, diagnostics
from ..widgets import FieldRow, button, card, h1, muted, plural
from ...integrations import llm
from ...integrations.llm import LLMClient, LLMError
from ...security import WrongPasswordError
from ...services.backup_service import DEFAULT_BACKUP_SETTINGS, open_in_explorer
from ...services.export_service import DEFAULT_MD_HEADER
from ...telegram.collector import DEFAULT_SCHEDULE

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


class SettingsScreen(QWidget):
    """16 independent sections in one long scrolling page — see PLAN.md's
    Р4 journal entry. Each lives in its own `_build_*` method (a card
    widget it returns, or a shared QGridLayout it populates for the two
    sections — Telegram access and speed — that sit side by side in one
    grid rather than as separate cards); `__init__` just assembles them
    in the same order and with the same spacing the page always had.
    Splitting further into separate files wasn't done here — every
    section is still a handful of widgets and one save handler, and nothing
    about that got harder to find once it had its own method name.
    """

    def __init__(self, ctx: AppContext, navigate):
        super().__init__()
        self.ctx = ctx
        self.navigate = navigate

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        scroll.setWidget(container)
        wrap = QVBoxLayout(self)
        wrap.setContentsMargins(0, 0, 0, 0)
        wrap.addWidget(scroll)

        outer = QVBoxLayout(container)
        outer.setContentsMargins(40, 28, 40, 32)
        outer.addWidget(h1("Настройки"))
        outer.addWidget(muted(
            "Ключи доступа берутся на my.telegram.org и хранятся в отдельном файле "
            "рядом с программой, не в её коде."
        ))
        # Which build this is — the first thing worth knowing when
        # something goes wrong and the report comes back as a screenshot.
        version_row = QHBoxLayout()
        version_row.addWidget(muted(f"Версия {__version__}"))
        version_row.addWidget(muted("·"))
        copy_paths_btn = button("Скопировать сведения о сборке", "ghost")
        copy_paths_btn.clicked.connect(self._copy_build_info)
        version_row.addWidget(copy_paths_btn)
        version_row.addStretch(1)
        outer.addLayout(version_row)
        outer.addSpacing(18)

        grid = QGridLayout()
        grid.setHorizontalSpacing(28)
        grid.setVerticalSpacing(10)
        outer.addLayout(grid)
        self._build_telegram_access_grid(grid)
        self._build_speed_grid(grid)
        outer.addSpacing(24)

        outer.addWidget(self._build_media_card())
        outer.addSpacing(24)

        outer.addWidget(self._build_security_card())
        outer.addSpacing(24)

        outer.addWidget(self._build_schedule_card())
        outer.addSpacing(24)

        outer.addWidget(self._build_ignore_rules_card())
        outer.addSpacing(24)

        outer.addWidget(self._build_authors_card())
        outer.addSpacing(24)

        outer.addWidget(self._build_database_card())
        outer.addSpacing(24)

        outer.addWidget(self._build_tray_card())

        outer.addWidget(self._build_retention_card())

        outer.addWidget(self._build_productivity_card())

        outer.addWidget(self._build_export_schedule_card())
        outer.addSpacing(24)

        outer.addWidget(self._build_integrations_pointer_card())
        outer.addSpacing(24)

        outer.addWidget(self._build_llm_card())
        outer.addSpacing(24)

        outer.addWidget(self._build_diagnostics_card())
        self._refresh_diagnostics_status()

        outer.addWidget(self._build_misc_card())
        outer.addStretch(1)

        self._refresh_rules()
        self._refresh_authors()
        self._refresh_db_size()
        self._refresh_security_section()
        self._refresh_retention_preview()
        self._populate_sched_presets()
        self._refresh_schedules()

    def on_show(self, **kwargs) -> None:
        self._refresh_rules()
        self._refresh_authors()
        self._refresh_db_size()
        self._refresh_security_section()
        self._refresh_retention_preview()
        self._refresh_productivity()
        self._populate_sched_presets()
        self._refresh_schedules()

    # ---- section builders ----------------------------------------------
    def _build_telegram_access_grid(self, grid: QGridLayout) -> None:
        grid.addWidget(muted("ДОСТУП К TELEGRAM"), 0, 0)
        grid.addWidget(QLabel("Ключ приложения (api_id)"), 1, 0)
        self.api_id_input = QLineEdit(self.ctx.config.api_id)
        grid.addWidget(self.api_id_input, 2, 0)
        self.api_hash_field = FieldRow("Секрет приложения (api_hash)", password=True)
        self.api_hash_field.set_text(self.ctx.config.api_hash)
        grid.addWidget(self.api_hash_field, 3, 0)
        grid.addWidget(QLabel("Файл входа в аккаунт"), 4, 0)
        session_row = QHBoxLayout()
        self.session_path_input = QLineEdit(self.ctx.config.session_path)
        session_row.addWidget(self.session_path_input)
        session_browse = button("Обзор…", "secondary")
        session_browse.clicked.connect(self._browse_session)
        session_row.addWidget(session_browse)
        session_row_w = QWidget()
        session_row_w.setLayout(session_row)
        grid.addWidget(session_row_w, 5, 0)
        note = muted("Этот файл даёт полный доступ к аккаунту. Не пересылайте его и не кладите в выгрузки.")
        note.setWordWrap(True)
        grid.addWidget(note, 6, 0)
        save_creds_btn = button("Сохранить", "primary")
        save_creds_btn.clicked.connect(self._save_credentials)
        grid.addWidget(save_creds_btn, 7, 0)

    def _build_speed_grid(self, grid: QGridLayout) -> None:
        grid.addWidget(muted("СКОРОСТЬ ЗАГРУЗКИ ИСТОРИИ"), 0, 1)
        grid.addWidget(QLabel("Пауза между запросами истории, с"), 1, 1)
        delay_row = QHBoxLayout()
        self.delay_min = QDoubleSpinBox()
        self.delay_min.setRange(0.1, 10.0)
        self.delay_min.setSingleStep(0.1)
        self.delay_max = QDoubleSpinBox()
        self.delay_max.setRange(0.1, 20.0)
        self.delay_max.setSingleStep(0.1)
        bounds = self.ctx.db.get_setting("delay_bounds", {"min": 0.2, "max": 4.0})
        self.delay_min.setValue(bounds["min"])
        self.delay_max.setValue(bounds["max"])
        delay_row.addWidget(muted("от"))
        delay_row.addWidget(self.delay_min)
        delay_row.addWidget(muted("до"))
        delay_row.addWidget(self.delay_max)
        delay_row_w = QWidget()
        delay_row_w.setLayout(delay_row)
        grid.addWidget(delay_row_w, 2, 1)
        hint = muted("Больше пауза — реже остановки со стороны Telegram, но история собирается дольше. "
                      "Пауза подстраивается сама: растёт после отказа, плавно снижается при успешной серии.")
        hint.setWordWrap(True)
        grid.addWidget(hint, 3, 1)
        save_speed_btn = button("Сохранить", "primary")
        save_speed_btn.clicked.connect(self._save_speed_photos)
        grid.addWidget(save_speed_btn, 4, 1)

    def _build_media_card(self) -> QWidget:
        media_card = card()
        media_lay = QVBoxLayout(media_card)
        media_lay.setContentsMargins(16, 14, 16, 14)
        media_lay.addWidget(muted("КАКИЕ МЕДИАФАЙЛЫ СКАЧИВАТЬ"))
        media_lay.addWidget(muted(
            "По умолчанию скачиваются только фото — остальное включайте, если нужно "
            "разобрать чат, где важны видео, голосовые или документы."
        ))

        self.photos_cb = self._media_toggle_row(
            media_lay, "Фотографии", "photos/<chat_id>/<message_id>.jpg", self.ctx.config.photos_enabled,
        )
        self.videos_cb = self._media_toggle_row(
            media_lay, "Видео", "videos/<chat_id>/<message_id>.mp4", self.ctx.config.videos_enabled,
        )
        self.voice_cb = self._media_toggle_row(
            media_lay, "Голосовые сообщения", "voice/<chat_id>/<message_id>.ogg", self.ctx.config.voice_enabled,
        )
        self.documents_cb = self._media_toggle_row(
            media_lay, "Документы", "documents/<chat_id>/<message_id>_<имя файла>", self.ctx.config.documents_enabled,
        )

        size_row = QHBoxLayout()
        size_row.addWidget(muted("Максимальный размер файла (кроме фото)"))
        self.max_media_size_spin = QSpinBox()
        self.max_media_size_spin.setRange(1, 2000)
        self.max_media_size_spin.setSuffix(" МБ")
        self.max_media_size_spin.setValue(self.ctx.config.max_media_size_mb)
        size_row.addWidget(self.max_media_size_spin)
        size_row.addStretch(1)
        media_lay.addLayout(size_row)
        size_hint = muted(
            "Файлы крупнее лимита пропускаются (сохранятся только тип и подпись) — "
            "видео и документы бывают очень большими, а скачивание идёт в той же "
            "очереди, что и загрузка истории."
        )
        size_hint.setWordWrap(True)
        media_lay.addWidget(size_hint)

        media_lay.addWidget(QLabel("Папка для фотографий"))
        photos_dir_row = QHBoxLayout()
        self.photos_dir_input = QLineEdit(self.ctx.config.photos_dir)
        photos_dir_row.addWidget(self.photos_dir_input)
        photos_dir_browse = button("Обзор…", "secondary")
        photos_dir_browse.clicked.connect(self._browse_photos_dir)
        photos_dir_row.addWidget(photos_dir_browse)
        media_lay.addLayout(photos_dir_row)
        media_note = muted("Видео, голосовые и документы сохраняются рядом, в подпапках videos/voice/documents.")
        media_lay.addWidget(media_note)

        save_media_btn = button("Сохранить", "primary")
        save_media_btn.clicked.connect(self._save_media_settings)
        media_lay.addWidget(save_media_btn)
        return media_card

    def _build_security_card(self) -> QWidget:
        self.security_card = card()
        sec_lay = QVBoxLayout(self.security_card)
        sec_lay.setContentsMargins(16, 14, 16, 14)
        sec_lay.addWidget(muted("ЗАЩИТА МАСТЕР-ПАРОЛЕМ"))
        self.security_status_label = QLabel("")
        self.security_status_label.setWordWrap(True)
        sec_lay.addWidget(self.security_status_label)
        sec_btn_row = QHBoxLayout()
        self.security_toggle_btn = button("", "primary")
        self.security_toggle_btn.clicked.connect(self._on_toggle_security)
        sec_btn_row.addWidget(self.security_toggle_btn)
        sec_btn_row.addStretch(1)
        sec_lay.addLayout(sec_btn_row)
        return self.security_card

    def _build_schedule_card(self) -> QWidget:
        sched_card = card()
        sched_lay = QVBoxLayout(sched_card)
        sched_lay.setContentsMargins(16, 14, 16, 14)
        sched_lay.addWidget(muted("РАСПИСАНИЕ ЗАГРУЗКИ ИСТОРИИ"))
        sched_lay.addWidget(muted(
            "Прослушивание новых сообщений работает всегда, независимо от расписания."
        ))
        schedule = self.ctx.db.get_setting("schedule", DEFAULT_SCHEDULE)
        self.sched_enabled_cb = QCheckBox("Ограничить загрузку истории окном времени")
        self.sched_enabled_cb.setChecked(schedule.get("enabled", False))
        sched_lay.addWidget(self.sched_enabled_cb)
        time_row = QHBoxLayout()
        time_row.addWidget(muted("с"))
        self.sched_start = QLineEdit(schedule.get("start", "23:00"))
        self.sched_start.setMaximumWidth(70)
        time_row.addWidget(self.sched_start)
        time_row.addWidget(muted("до"))
        self.sched_end = QLineEdit(schedule.get("end", "08:00"))
        self.sched_end.setMaximumWidth(70)
        time_row.addWidget(self.sched_end)
        time_row.addSpacing(16)
        self.day_checks: list[QCheckBox] = []
        for i, name in enumerate(WEEKDAYS):
            cb = QCheckBox(name)
            cb.setChecked(i in schedule.get("days", list(range(7))))
            self.day_checks.append(cb)
            time_row.addWidget(cb)
        time_row.addStretch(1)
        sched_lay.addLayout(time_row)
        save_sched_btn = button("Сохранить расписание", "primary")
        save_sched_btn.clicked.connect(self._save_schedule)
        sched_lay.addWidget(save_sched_btn)
        return sched_card

    def _build_ignore_rules_card(self) -> QWidget:
        ignore_card = card()
        ig_lay = QVBoxLayout(ignore_card)
        ig_lay.setContentsMargins(16, 14, 16, 14)
        ig_lay.addWidget(muted("ПРАВИЛА ИГНОРА"))
        ig_lay.addWidget(muted(
            "Сообщения от автора или со стоп-словом помечаются скрытыми — не удаляются, "
            "не попадают в выгрузку по умолчанию."
        ))
        add_row = QHBoxLayout()
        self.rule_type_combo = QComboBox()
        self.rule_type_combo.addItems(["автор (имя или @ник)", "стоп-слово"])
        add_row.addWidget(self.rule_type_combo)
        self.rule_value_input = QLineEdit()
        self.rule_value_input.setPlaceholderText("значение")
        add_row.addWidget(self.rule_value_input, 1)
        self.rule_scope_combo = QComboBox()
        self.rule_scope_combo.addItem("во всех чатах", "global")
        for chat in self.ctx.db.list_chats():
            self.rule_scope_combo.addItem(f"только в «{chat['title']}»", chat["chat_id"])
        add_row.addWidget(self.rule_scope_combo)
        add_rule_btn = button("Добавить правило", "primary")
        add_rule_btn.clicked.connect(self._add_ignore_rule)
        add_row.addWidget(add_rule_btn)
        ig_lay.addLayout(add_row)

        self.rules_list = QListWidget()
        self.rules_list.setMaximumHeight(140)
        ig_lay.addWidget(self.rules_list)
        rule_btn_row = QHBoxLayout()
        del_rule_btn = button("Удалить выбранное", "secondary")
        del_rule_btn.clicked.connect(self._delete_ignore_rule)
        rule_btn_row.addWidget(del_rule_btn)
        apply_rules_btn = button("Применить к уже собранным", "ghost")
        apply_rules_btn.clicked.connect(self._apply_ignore_rules)
        rule_btn_row.addWidget(apply_rules_btn)
        rule_btn_row.addStretch(1)
        ig_lay.addLayout(rule_btn_row)
        return ignore_card

    def _build_authors_card(self) -> QWidget:
        authors_card = card()
        au_lay = QVBoxLayout(authors_card)
        au_lay.setContentsMargins(16, 14, 16, 14)
        au_lay.addWidget(muted("АВТОРЫ ПО ЧАТУ"))
        au_pick_row = QHBoxLayout()
        self.authors_chat_combo = QComboBox()
        for chat in self.ctx.db.list_chats():
            self.authors_chat_combo.addItem(chat["title"], chat["chat_id"])
        self.authors_chat_combo.currentIndexChanged.connect(self._refresh_authors)
        au_pick_row.addWidget(self.authors_chat_combo, 1)
        au_lay.addLayout(au_pick_row)
        self.authors_table = QTableWidget(0, 5)
        self.authors_table.setHorizontalHeaderLabels(["Автор", "@ник", "Сообщений", "Первое", "Последнее"])
        self.authors_table.setMaximumHeight(200)
        self.authors_table.verticalHeader().setVisible(False)
        au_lay.addWidget(self.authors_table)
        return authors_card

    def _build_database_card(self) -> QWidget:
        db_card = card()
        db_lay = QVBoxLayout(db_card)
        db_lay.setContentsMargins(16, 14, 16, 14)
        db_lay.addWidget(muted("БАЗА ДАННЫХ"))
        paths_row = QHBoxLayout()
        paths_row.addWidget(QLabel(f"База: {self.ctx.paths.db_path}"))
        open_db_btn = button("Открыть в проводнике", "secondary")
        open_db_btn.clicked.connect(lambda: open_in_explorer(self.ctx.paths.data_dir))
        paths_row.addWidget(open_db_btn)
        paths_row.addStretch(1)
        db_lay.addLayout(paths_row)
        photos_path_row = QHBoxLayout()
        photos_path_row.addWidget(QLabel(f"Фото: {self.ctx.paths.photos_dir}"))
        open_photos_btn = button("Открыть в проводнике", "secondary")
        open_photos_btn.clicked.connect(lambda: open_in_explorer(self.ctx.paths.photos_dir))
        photos_path_row.addWidget(open_photos_btn)
        photos_path_row.addStretch(1)
        db_lay.addLayout(photos_path_row)

        backup_settings = self.ctx.backup_service.settings()
        backup_row = QHBoxLayout()
        self.backup_enabled_cb = QCheckBox("Резервная копия по расписанию")
        self.backup_enabled_cb.setChecked(backup_settings.get("enabled", True))
        backup_row.addWidget(self.backup_enabled_cb)
        backup_row.addWidget(muted("каждые"))
        self.backup_interval_spin = QSpinBox()
        self.backup_interval_spin.setRange(1, 168)
        self.backup_interval_spin.setValue(backup_settings.get("interval_hours", 24))
        backup_row.addWidget(self.backup_interval_spin)
        backup_row.addWidget(muted("ч. · хранить"))
        self.backup_keep_spin = QSpinBox()
        self.backup_keep_spin.setRange(1, 50)
        self.backup_keep_spin.setValue(backup_settings.get("keep", 5))
        backup_row.addWidget(self.backup_keep_spin)
        backup_row.addWidget(muted("копий"))
        backup_row.addStretch(1)
        db_lay.addLayout(backup_row)

        action_row = QHBoxLayout()
        save_backup_btn = button("Сохранить настройки бэкапа", "secondary")
        save_backup_btn.clicked.connect(self._save_backup_settings)
        action_row.addWidget(save_backup_btn)
        backup_now_btn = button("Сделать бэкап сейчас", "secondary")
        backup_now_btn.clicked.connect(self._backup_now)
        action_row.addWidget(backup_now_btn)
        vacuum_btn = button("Сжать базу (VACUUM)", "secondary")
        vacuum_btn.clicked.connect(self._vacuum)
        action_row.addWidget(vacuum_btn)
        action_row.addStretch(1)
        db_lay.addLayout(action_row)
        self.db_size_label = muted("")
        db_lay.addWidget(self.db_size_label)
        return db_card

    def _build_tray_card(self) -> QWidget:
        tray_card = card()
        tray_lay = QVBoxLayout(tray_card)
        tray_lay.setContentsMargins(16, 14, 16, 14)
        tray_lay.addWidget(muted("РАБОТА В ФОНЕ"))
        tray_hint = muted(
            "Приложение слушает чаты, только пока запущено. Эти настройки позволяют "
            "держать его включённым, не занимая место на панели задач."
        )
        tray_hint.setWordWrap(True)
        tray_lay.addWidget(tray_hint)

        self.tray_close_cb = QCheckBox("Сворачивать в область уведомлений вместо закрытия")
        self.tray_close_cb.setChecked(self.ctx.db.get_setting("tray_minimize_on_close", True))
        tray_lay.addWidget(self.tray_close_cb)

        self.autostart_cb = QCheckBox("Запускать вместе с Windows")
        self.autostart_cb.setChecked(tray.autostart_enabled())
        if not tray.autostart_supported():
            self.autostart_cb.setEnabled(False)
            self.autostart_cb.setText("Запускать вместе с Windows (только в Windows-сборке)")
        tray_lay.addWidget(self.autostart_cb)

        self.tray_status = muted("")
        self.tray_status.setWordWrap(True)
        tray_lay.addWidget(self.tray_status)
        save_tray_btn = button("Сохранить", "primary")
        save_tray_btn.clicked.connect(self._save_tray)
        tray_lay.addWidget(save_tray_btn)
        return tray_card

    def _build_retention_card(self) -> QWidget:
        ret_card = card()
        ret_lay = QVBoxLayout(ret_card)
        ret_lay.setContentsMargins(16, 14, 16, 14)
        ret_lay.addWidget(muted("СКОЛЬКО ХРАНИТЬ"))
        ret_hint = muted(
            "База растёт, пока её не подрезать. Старое сначала выписывается в "
            "архивный файл рядом с выгрузками и только потом удаляется — "
            "заархивированное можно открыть и отдать Claude позже. "
            "Само по себе ничего не удаляется: только по этой кнопке."
        )
        ret_hint.setWordWrap(True)
        ret_lay.addWidget(ret_hint)

        ret_row = QHBoxLayout()
        ret_row.addWidget(QLabel("Хранить сообщения за последние"))
        self.retention_spin = QSpinBox()
        self.retention_spin.setRange(0, 120)
        self.retention_spin.setSuffix(" мес.")
        self.retention_spin.setSpecialValueText("всё время")
        self.retention_spin.setValue(self.ctx.retention_service.months)
        self.retention_spin.valueChanged.connect(self._refresh_retention_preview)
        ret_row.addWidget(self.retention_spin)
        ret_row.addStretch(1)
        ret_lay.addLayout(ret_row)

        self.retention_preview = muted("")
        self.retention_preview.setWordWrap(True)
        ret_lay.addWidget(self.retention_preview)

        ret_btns = QHBoxLayout()
        save_ret_btn = button("Сохранить", "primary")
        save_ret_btn.clicked.connect(self._save_retention)
        ret_btns.addWidget(save_ret_btn)
        self.prune_btn = button("Заархивировать и удалить старое", "secondary")
        self.prune_btn.clicked.connect(self._on_prune)
        ret_btns.addWidget(self.prune_btn)
        orphan_btn = button("Найти файлы без сообщений", "ghost")
        orphan_btn.clicked.connect(self._on_orphans)
        ret_btns.addWidget(orphan_btn)
        ret_btns.addStretch(1)
        ret_lay.addLayout(ret_btns)
        return ret_card

    def _build_productivity_card(self) -> QWidget:
        prod_card = card()
        prod_lay = QVBoxLayout(prod_card)
        prod_lay.setContentsMargins(16, 14, 16, 14)
        prod_lay.addWidget(muted("ОТДАЧА ИСТОЧНИКОВ ЗА 30 ДНЕЙ"))
        prod_hint = muted(
            "Объём сам по себе не говорит, стоит ли собирать чат. Рядом — сколько "
            "раз он дал то, о чём вы просили сообщить: совпадения по наблюдению и "
            "срабатывания правил ботов."
        )
        prod_hint.setWordWrap(True)
        prod_lay.addWidget(prod_hint)
        self.prod_table = QTableWidget(0, 6)
        self.prod_table.setHorizontalHeaderLabels(
            ["Чат", "Всего", "В сутки", "Находок", "Правил", "Медиа"])
        self.prod_table.verticalHeader().setVisible(False)
        self.prod_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.prod_table.setShowGrid(False)
        self.prod_table.setMaximumHeight(220)
        self.prod_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        prod_lay.addWidget(self.prod_table)
        prod_btns = QHBoxLayout()
        refresh_prod_btn = button("Пересчитать", "ghost")
        refresh_prod_btn.clicked.connect(self._refresh_productivity)
        prod_btns.addWidget(refresh_prod_btn)
        prod_btns.addStretch(1)
        prod_lay.addLayout(prod_btns)
        return prod_card

    def _build_export_schedule_card(self) -> QWidget:
        xsched_card = card()
        xsched_lay = QVBoxLayout(xsched_card)
        xsched_lay.setContentsMargins(16, 14, 16, 14)
        xsched_lay.addWidget(muted("ВЫГРУЗКА ПО РАСПИСАНИЮ"))
        sched_hint = muted(
            "Запускает сохранённый пресет экспорта сам, чтобы свежий файл просто "
            "появлялся в папке. Если компьютер был выключен в назначенный час, "
            "выгрузка произойдёт при следующем запуске — момент не пропадает."
        )
        sched_hint.setWordWrap(True)
        xsched_lay.addWidget(sched_hint)

        sched_add = QHBoxLayout()
        sched_add.addWidget(muted("Пресет"))
        self.sched_preset_combo = QComboBox()
        self.sched_preset_combo.setMinimumWidth(160)
        sched_add.addWidget(self.sched_preset_combo)
        sched_add.addWidget(muted("каждые"))
        self.sched_hours_spin = QSpinBox()
        self.sched_hours_spin.setRange(1, 24 * 30)
        self.sched_hours_spin.setValue(168)
        self.sched_hours_spin.setSuffix(" ч")
        sched_add.addWidget(self.sched_hours_spin)
        sched_add.addWidget(muted("не раньше"))
        self.sched_hour_spin = QSpinBox()
        self.sched_hour_spin.setRange(0, 23)
        self.sched_hour_spin.setValue(9)
        self.sched_hour_spin.setSuffix(":00")
        sched_add.addWidget(self.sched_hour_spin)
        add_sched_btn = button("Добавить", "primary")
        add_sched_btn.clicked.connect(self._on_add_schedule)
        sched_add.addWidget(add_sched_btn)
        sched_add.addStretch(1)
        xsched_lay.addLayout(sched_add)

        self.sched_table = QTableWidget(0, 5)
        self.sched_table.setHorizontalHeaderLabels(
            ["Пресет", "Период", "Прошлый запуск", "Результат", ""])
        self.sched_table.verticalHeader().setVisible(False)
        self.sched_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.sched_table.setShowGrid(False)
        self.sched_table.setMaximumHeight(160)
        self.sched_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        xsched_lay.addWidget(self.sched_table)
        self.sched_empty = muted(
            "Расписаний нет. Пресеты создаются на экране «Выгрузка» — "
            "сначала сохраните там набор параметров, потом его можно запускать сам."
        )
        self.sched_empty.setWordWrap(True)
        xsched_lay.addWidget(self.sched_empty)
        return xsched_card

    def _build_integrations_pointer_card(self) -> QWidget:
        """С7: Bitrix24 moved to its own screen — this card is what's left
        behind on Настройки, pointing at it."""
        integrations_card = card()
        integrations_lay = QHBoxLayout(integrations_card)
        integrations_lay.setContentsMargins(16, 14, 16, 14)
        integrations_lay.addWidget(muted(
            "Подключение к Bitrix24, соответствие статусов и источников — на отдельном экране."
        ), 1)
        open_bitrix_btn = button("Bitrix24 →", "secondary")
        open_bitrix_btn.clicked.connect(lambda: self.navigate("bitrix"))
        integrations_lay.addWidget(open_bitrix_btn)
        return integrations_card

    def _build_llm_card(self) -> QWidget:
        """С9 — опционально, выключен по умолчанию."""
        llm_card = card()
        llm_lay = QVBoxLayout(llm_card)
        llm_lay.setContentsMargins(16, 14, 16, 14)
        llm_lay.addWidget(muted("LLM-ПОМОЩНИК · ОПЦИОНАЛЬНО, ВЫКЛЮЧЕН ПО УМОЛЧАНИЮ"))
        llm_hint = muted(
            "Извлекает поля заявки из переписки, готовит краткое содержание и черновик "
            "ответа — на карточке лида, по кнопке, ничего не сохраняет само. Без ключа и "
            "с выключенной галочкой ниже приложение не делает к этому сервису ни одного "
            "сетевого запроса."
        )
        llm_hint.setWordWrap(True)
        llm_lay.addWidget(llm_hint)

        self.llm_enabled_cb = QCheckBox("Включить помощника")
        self.llm_enabled_cb.setChecked(bool(self.ctx.db.get_setting(llm.SETTING_KEY_ENABLED, False)))
        llm_lay.addWidget(self.llm_enabled_cb)

        self.llm_key_field = FieldRow("Ключ API Anthropic", placeholder="ключ API Anthropic", password=True)
        self.llm_key_field.set_text(llm.get_api_key(self.ctx.db, self.ctx.security) or "")
        llm_lay.addWidget(self.llm_key_field)

        llm_model_row = QHBoxLayout()
        llm_model_row.addWidget(muted("Модель"))
        self.llm_model_input = QLineEdit(llm.get_model(self.ctx.db))
        llm_model_row.addWidget(self.llm_model_input, 1)
        llm_lay.addLayout(llm_model_row)

        self.llm_status = muted("")
        self.llm_status.setWordWrap(True)
        llm_lay.addWidget(self.llm_status)
        self._refresh_llm_status()

        llm_btn_row = QHBoxLayout()
        save_llm_btn = button("Сохранить", "primary")
        save_llm_btn.clicked.connect(self._save_llm)
        llm_btn_row.addWidget(save_llm_btn)
        self.test_llm_btn = button("Проверить подключение", "secondary")
        self.test_llm_btn.clicked.connect(self._on_test_llm)
        llm_btn_row.addWidget(self.test_llm_btn)
        llm_btn_row.addStretch(1)
        llm_lay.addLayout(llm_btn_row)
        return llm_card

    def _build_diagnostics_card(self) -> QWidget:
        """Temporary — see TEMPORARY.md."""
        diag_card = card()
        diag_lay = QVBoxLayout(diag_card)
        diag_lay.setContentsMargins(16, 14, 16, 14)
        diag_lay.addWidget(muted("ДИАГНОСТИЧЕСКАЯ ЗАПИСЬ · ВРЕМЕННАЯ ФУНКЦИЯ"))
        diag_hint = muted(
            "Пишет в отдельный файл, какие экраны вы открывали, что нажимали и что "
            "приложение при этом делало — включая ошибки, которые не видно на экране. "
            "Нужна на время ручного тестирования: файл потом можно отдать разработчику. "
            "Тексты сообщений, ключи и токены в запись не попадают."
        )
        diag_hint.setWordWrap(True)
        diag_lay.addWidget(diag_hint)

        self.diag_cb = QCheckBox("Вести диагностическую запись")
        self.diag_cb.setChecked(bool(self.ctx.db.get_setting(diagnostics.SETTING_KEY, False)))
        diag_lay.addWidget(self.diag_cb)

        self.diag_status = muted("")
        self.diag_status.setWordWrap(True)
        diag_lay.addWidget(self.diag_status)

        diag_row = QHBoxLayout()
        save_diag_btn = button("Сохранить", "primary")
        save_diag_btn.clicked.connect(self._save_diagnostics)
        diag_row.addWidget(save_diag_btn)
        open_diag_btn = button("Открыть папку с записями", "secondary")
        open_diag_btn.clicked.connect(self._open_diagnostics_dir)
        diag_row.addWidget(open_diag_btn)
        diag_row.addStretch(1)
        diag_lay.addLayout(diag_row)
        return diag_card

    def _build_misc_card(self) -> QWidget:
        misc_card = card()
        misc_lay = QVBoxLayout(misc_card)
        misc_lay.setContentsMargins(16, 14, 16, 14)
        misc_lay.addWidget(muted("ПРОЧЕЕ"))
        gap_row = QHBoxLayout()
        gap_row.addWidget(QLabel("Предупреждать, если по чату нет новых сообщений дольше, суток"))
        self.gap_days_spin = QSpinBox()
        self.gap_days_spin.setRange(1, 90)
        self.gap_days_spin.setValue(self.ctx.db.get_setting("gap_notify_days", 7))
        gap_row.addWidget(self.gap_days_spin)
        gap_row.addStretch(1)
        misc_lay.addLayout(gap_row)

        misc_lay.addWidget(QLabel("Шапка Markdown-дайджеста"))
        self.md_header_edit = QPlainTextEdit(self.ctx.db.get_setting("markdown_header", DEFAULT_MD_HEADER))
        self.md_header_edit.setMaximumHeight(120)
        misc_lay.addWidget(self.md_header_edit)
        save_misc_btn = button("Сохранить", "primary")
        save_misc_btn.clicked.connect(self._save_misc)
        misc_lay.addWidget(save_misc_btn)
        return misc_card

    # ---- actions -----------------------------------------------------
    def _refresh_llm_status(self) -> None:
        if llm.is_enabled(self.ctx.db, self.ctx.security):
            self.llm_status.setText(f"Включён, модель: {llm.get_model(self.ctx.db)}.")
        elif llm.get_api_key(self.ctx.db, self.ctx.security):
            self.llm_status.setText("Ключ сохранён, но помощник выключен галочкой выше.")
        else:
            self.llm_status.setText("Выключен — ключ не задан, сетевых запросов к LLM API нет.")

    def _save_llm(self) -> None:
        llm.set_enabled(self.ctx.db, self.llm_enabled_cb.isChecked())
        llm.set_api_key(self.ctx.db, self.ctx.security, self.llm_key_field.text())
        llm.set_model(self.ctx.db, self.llm_model_input.text())
        self._refresh_llm_status()
        QMessageBox.information(self, "LLM-помощник", "Сохранено.")

    def _on_test_llm(self) -> None:
        api_key = self.llm_key_field.text().strip()
        if not api_key:
            QMessageBox.warning(self, "LLM-помощник", "Сначала укажите ключ API.")
            return
        model = self.llm_model_input.text().strip() or llm.DEFAULT_MODEL
        self.test_llm_btn.setEnabled(False)

        async def _check() -> str:
            try:
                return await LLMClient(api_key, model).ping()
            except LLMError as e:
                raise RuntimeError(str(e)) from e

        def on_error(e):
            self.test_llm_btn.setEnabled(True)
            QMessageBox.warning(self, "Не получилось подключиться", str(e))

        task = fire(_check(), parent=self, on_error=on_error)

        def _apply(t):
            self.test_llm_btn.setEnabled(True)
            if t.cancelled() or t.exception() is not None:
                return
            QMessageBox.information(self, "LLM-помощник", f"Подключено. Ответ модели: {t.result()!r}")

        task.add_done_callback(_apply)

    def _browse_session(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Файл входа", self.session_path_input.text())
        if path:
            self.session_path_input.setText(path)

    def _browse_photos_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Папка для фотографий", self.photos_dir_input.text())
        if d:
            self.photos_dir_input.setText(d)

    @staticmethod
    def _media_toggle_row(parent_layout: QVBoxLayout, title: str, path_pattern: str, checked: bool) -> QCheckBox:
        row = QHBoxLayout()
        col = QVBoxLayout()
        col.addWidget(QLabel(title))
        path_label = QLabel(path_pattern)
        path_label.setStyleSheet("font-family: Consolas, monospace; font-size: 11px; color: #6c6c78;")
        col.addWidget(path_label)
        row.addLayout(col)
        row.addStretch(1)
        cb = QCheckBox()
        cb.setChecked(checked)
        row.addWidget(cb)
        parent_layout.addLayout(row)
        return cb

    def _save_credentials(self) -> None:
        cfg = self.ctx.config
        new_api_id = self.api_id_input.text().strip()
        new_api_hash = self.api_hash_field.text().strip()
        new_session_path = self.session_path_input.text().strip()

        if self.ctx.security.enabled and new_api_hash != cfg.api_hash:
            pwd, ok = QInputDialog.getText(
                self, "Мастер-пароль",
                "Ключ защищён мастер-паролем — введите его, чтобы сохранить новое значение:",
                QLineEdit.Password,
            )
            if not ok:
                return
            try:
                self.ctx.security.unlock(pwd)
            except WrongPasswordError:
                QMessageBox.warning(self, "Неверный пароль", "Не удалось сохранить — пароль неверен.")
                return
            cfg.api_id = new_api_id
            cfg.session_path = new_session_path
            cfg.api_hash = new_api_hash
            self.ctx.security.enable(pwd)
        else:
            cfg.api_id = new_api_id
            cfg.api_hash = new_api_hash
            cfg.session_path = new_session_path
            cfg.save()

        QMessageBox.information(
            self, "Сохранено",
            "Ключи сохранены. Если менялся файл входа — перезапустите приложение, чтобы применить."
        )

    def _refresh_security_section(self) -> None:
        if self.ctx.security.enabled:
            self.security_status_label.setText(
                "Файл входа в Telegram и ключ приложения зашифрованы мастер-паролем. "
                "Он нигде не хранится — забыв его, нужно будет войти в Telegram заново."
            )
            self.security_toggle_btn.setText("Выключить защиту")
        else:
            self.security_status_label.setText(
                "Файл входа в Telegram и ключ приложения сейчас хранятся на диске "
                "открытым текстом. Мастер-пароль шифрует их — без пароля эти файлы "
                "бесполезны, даже если их скопировать с этого компьютера."
            )
            self.security_toggle_btn.setText("Включить защиту мастер-паролем")

    def _on_toggle_security(self) -> None:
        if self.ctx.security.enabled:
            self._disable_security()
        else:
            self._enable_security()

    def _enable_security(self) -> None:
        pwd, ok = QInputDialog.getText(self, "Мастер-пароль", "Придумайте мастер-пароль:", QLineEdit.Password)
        if not ok or not pwd:
            return
        pwd2, ok2 = QInputDialog.getText(self, "Мастер-пароль", "Повторите пароль:", QLineEdit.Password)
        if not ok2:
            return
        if pwd != pwd2:
            QMessageBox.warning(self, "Не совпадает", "Пароли не совпадают — попробуйте ещё раз.")
            return
        self.ctx.security.enable(pwd)
        self._refresh_security_section()
        QMessageBox.information(
            self, "Готово",
            "Защита включена. Пароль нигде не сохранён — не забудьте его: восстановить "
            "нельзя, только сбросить вход и начать заново."
        )

    def _disable_security(self) -> None:
        pwd, ok = QInputDialog.getText(self, "Мастер-пароль", "Введите текущий мастер-пароль:", QLineEdit.Password)
        if not ok or not pwd:
            return
        try:
            self.ctx.security.disable(pwd)
        except WrongPasswordError:
            QMessageBox.warning(self, "Неверный пароль", "Не удалось выключить защиту — пароль неверен.")
            return
        self._refresh_security_section()
        QMessageBox.information(self, "Готово", "Защита выключена.")

    def _save_speed_photos(self) -> None:
        self.ctx.collector.save_delay_bounds(self.delay_min.value(), self.delay_max.value())
        QMessageBox.information(self, "Сохранено", "Настройки скорости обновлены.")

    def _save_media_settings(self) -> None:
        cfg = self.ctx.config
        cfg.photos_enabled = self.photos_cb.isChecked()
        cfg.videos_enabled = self.videos_cb.isChecked()
        cfg.voice_enabled = self.voice_cb.isChecked()
        cfg.documents_enabled = self.documents_cb.isChecked()
        cfg.max_media_size_mb = self.max_media_size_spin.value()
        cfg.photos_dir = self.photos_dir_input.text().strip()
        cfg.save()
        self.ctx.paths.photos_dir = Path(cfg.photos_dir)
        self.ctx.paths.photos_dir.mkdir(parents=True, exist_ok=True)
        QMessageBox.information(self, "Сохранено", "Настройки медиафайлов обновлены.")

    def _save_schedule(self) -> None:
        days = [i for i, cb in enumerate(self.day_checks) if cb.isChecked()]
        self.ctx.collector.save_schedule(
            self.sched_enabled_cb.isChecked(), self.sched_start.text().strip(),
            self.sched_end.text().strip(), days,
        )
        QMessageBox.information(self, "Сохранено", "Расписание обновлено.")

    def _add_ignore_rule(self) -> None:
        value = self.rule_value_input.text().strip()
        if not value:
            return
        rule_type = "author" if self.rule_type_combo.currentIndex() == 0 else "stopword"
        scope_data = self.rule_scope_combo.currentData()
        scope = "global" if scope_data in (None, "global") else "chat"
        chat_id = scope_data if scope == "chat" else None
        self.ctx.ignore_service.add_rule(rule_type, value, scope, chat_id)
        self.rule_value_input.clear()
        self._refresh_rules()

    def _delete_ignore_rule(self) -> None:
        item = self.rules_list.currentItem()
        if item:
            self.ctx.ignore_service.remove_rule(item.data(1000))
            self._refresh_rules()

    def _refresh_rules(self) -> None:
        self.rules_list.clear()
        for r in self.ctx.ignore_service.list_rules():
            scope = "везде" if r["scope"] == "global" else f"чат {r['chat_id']}"
            kind = "автор" if r["rule_type"] == "author" else "стоп-слово"
            item = QListWidgetItem(f"{kind}: {r['value']}  ·  {scope}")
            item.setData(1000, r["id"])
            self.rules_list.addItem(item)

    def _apply_ignore_rules(self) -> None:
        n = self.ctx.ignore_service.apply_to_existing()
        QMessageBox.information(self, "Готово", f"Скрыто сообщений: {n}.")

    def _refresh_authors(self) -> None:
        chat_id = self.authors_chat_combo.currentData()
        self.authors_table.setRowCount(0)
        if chat_id is None:
            return
        authors = self.ctx.db.authors_for_chat(chat_id)
        self.authors_table.setRowCount(len(authors))
        for row, a in enumerate(authors):
            self.authors_table.setItem(row, 0, QTableWidgetItem(a["sender_display_name"] or "—"))
            self.authors_table.setItem(row, 1, QTableWidgetItem(a["sender_username"] or ""))
            self.authors_table.setItem(row, 2, QTableWidgetItem(str(a["n"])))
            self.authors_table.setItem(row, 3, QTableWidgetItem(str(a["first"])[:10]))
            self.authors_table.setItem(row, 4, QTableWidgetItem(str(a["last"])[:10]))

    def _refresh_db_size(self) -> None:
        size_mb = self.ctx.db.file_size() / (1024 * 1024)
        self.db_size_label.setText(f"Текущий размер базы: {size_mb:.1f} МБ")

    def _save_backup_settings(self) -> None:
        self.ctx.backup_service.save_settings(
            self.backup_enabled_cb.isChecked(), self.backup_interval_spin.value(),
            self.backup_keep_spin.value(),
        )
        QMessageBox.information(self, "Сохранено", "Настройки резервного копирования обновлены.")

    def _backup_now(self) -> None:
        path = self.ctx.backup_service.run_backup_now()
        QMessageBox.information(self, "Готово", f"Резервная копия сохранена: {path}")

    def _vacuum(self) -> None:
        before, after = self.ctx.backup_service.vacuum()
        self._refresh_db_size()
        QMessageBox.information(
            self, "Готово",
            f"Было: {before / (1024 * 1024):.1f} МБ → стало: {after / (1024 * 1024):.1f} МБ",
        )

    @staticmethod
    def _fit_table(table: QTableWidget, row_height: int, cap: int) -> None:
        """Height that follows the contents. Inside a long scrolling page a
        fixed-height table with three rows in it just reads as a hole."""
        wanted = table.horizontalHeader().height() + row_height * table.rowCount() + 4
        table.setFixedHeight(max(row_height + 24, min(cap, wanted)))

    # ---- retention ---------------------------------------------------
    def _refresh_retention_preview(self) -> None:
        """Says what the button would remove *before* it is pressed. The
        count is against the value currently in the spinner, not the saved
        one — otherwise the preview describes a different setting than the
        one on screen."""
        months = self.retention_spin.value()
        if months <= 0:
            self.retention_preview.setText(
                "Сейчас хранится всё. Ничего не удаляется и не архивируется.")
            self.prune_btn.setEnabled(False)
            return
        info = self.ctx.retention_service.preview(months)
        n = info["messages"]
        if not n:
            self.retention_preview.setText(
                f"Старше {info['cutoff'][:10]} ничего нет — удалять нечего.")
            self.prune_btn.setEnabled(False)
            return
        self.retention_preview.setText(
            f"Старше {info['cutoff'][:10]} — {n} "
            + plural(n, "сообщение", "сообщения", "сообщений")
            + ". Они уйдут в архивный файл и будут удалены из базы.")
        self.prune_btn.setEnabled(True)

    def _save_retention(self) -> None:
        self.ctx.retention_service.set_months(self.retention_spin.value())
        self._refresh_retention_preview()
        QMessageBox.information(
            self, "Сохранено",
            "Срок хранения записан. Само по себе ничего не удалится — "
            "старое уйдёт только по кнопке «Заархивировать и удалить старое».")

    def _on_prune(self) -> None:
        months = self.retention_spin.value()
        if months <= 0:
            return
        info = self.ctx.retention_service.preview(months)
        n = info["messages"]
        if not n:
            QMessageBox.information(self, "Нечего удалять",
                                    "Сообщений старше указанного срока нет.")
            return
        if QMessageBox.question(
            self, "Заархивировать и удалить",
            f"{n} " + plural(n, "сообщение", "сообщения", "сообщений")
            + f" старше {info['cutoff'][:10]} будут выписаны в архивный файл "
            "и удалены из базы. Файлы медиа останутся на диске — их можно "
            "найти отдельно кнопкой рядом. Продолжить?"
        ) != QMessageBox.Yes:
            return
        result = self.ctx.retention_service.archive_and_prune(months)
        self._refresh_db_size()
        self._refresh_retention_preview()
        where = f"\n\nАрхив: {result['path']}" if result["path"] else ""
        QMessageBox.information(
            self, "Готово",
            f"Удалено {result['deleted']} "
            + plural(result["deleted"], "сообщение", "сообщения", "сообщений")
            + f", в архив записано {result['archived']}.{where}")

    def _on_orphans(self) -> None:
        """Lists leftover files rather than deleting them. Media on disk is
        the one thing the app cannot recreate, so the decision stays with
        the user — the folder just gets opened."""
        orphans = self.ctx.retention_service.orphaned_media()
        if not orphans:
            QMessageBox.information(
                self, "Всё на месте",
                "Каждый файл в папках медиа привязан к сообщению в базе.")
            return
        total_mb = sum(p.stat().st_size for p in orphans if p.exists()) / (1024 * 1024)
        sample = "\n".join(str(p) for p in orphans[:10])
        more = f"\n… и ещё {len(orphans) - 10}" if len(orphans) > 10 else ""
        if QMessageBox.question(
            self, "Файлы без сообщений",
            f"{len(orphans)} " + plural(len(orphans), "файл", "файла", "файлов")
            + f" ({total_mb:.1f} МБ) больше не связаны ни с одним сообщением — "
            "обычно это остаётся после удаления чата или архивации.\n\n"
            f"{sample}{more}\n\nОткрыть папку, чтобы разобраться вручную?"
        ) == QMessageBox.Yes:
            open_in_explorer(self.ctx.paths.data_dir)

    # ---- source productivity -----------------------------------------
    def _refresh_productivity(self) -> None:
        rows = self.ctx.db.chat_productivity(30)
        rows.sort(key=lambda r: (r["watch_hits"] + r["triggers"], r["recent"]), reverse=True)
        self.prod_table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            values = [
                row["title"] or str(row["chat_id"]),
                str(row["recent"]),
                f"{row['per_day']:g}",
                str(row["watch_hits"]),
                str(row["triggers"]),
                str(row["media"] or 0),
            ]
            for col, value in enumerate(values):
                self.prod_table.setItem(i, col, QTableWidgetItem(value))
            self.prod_table.setRowHeight(i, 30)
        self.prod_table.resizeColumnsToContents()
        self.prod_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._fit_table(self.prod_table, 30, 220)

    # ---- scheduled exports -------------------------------------------
    def _populate_sched_presets(self) -> None:
        current = self.sched_preset_combo.currentText()
        self.sched_preset_combo.clear()
        names = [p["name"] for p in self.ctx.db.list_presets()]
        self.sched_preset_combo.addItems(names)
        if current in names:
            self.sched_preset_combo.setCurrentText(current)
        self.sched_preset_combo.setEnabled(bool(names))

    def _on_add_schedule(self) -> None:
        name = self.sched_preset_combo.currentText().strip()
        if not name:
            QMessageBox.information(
                self, "Нет пресетов",
                "Сначала сохраните набор параметров на экране «Выгрузка» — "
                "расписание запускает именно его.")
            return
        existing = [s for s in self.ctx.db.list_export_schedules()
                    if s["preset_name"] == name]
        if existing:
            QMessageBox.information(
                self, "Уже есть",
                f"Пресет «{name}» уже стоит на расписании. Удалите старую "
                "строку, если период нужно изменить.")
            return
        self.ctx.db.add_export_schedule(
            name, self.sched_hours_spin.value(), self.sched_hour_spin.value())
        self._refresh_schedules()

    def _refresh_schedules(self) -> None:
        schedules = self.ctx.db.list_export_schedules()
        self.sched_empty.setVisible(not schedules)
        self.sched_table.setVisible(bool(schedules))
        self.sched_table.setRowCount(len(schedules))
        for i, s in enumerate(schedules):
            hours = s["every_hours"]
            if hours % 24 == 0:
                days = hours // 24
                period = f"каждые {days} " + plural(days, "сутки", "суток", "суток")
            else:
                period = f"каждые {hours} " + plural(hours, "час", "часа", "часов")
            period += f", не раньше {s['at_hour']:02d}:00"
            last = short_dt(s["last_run_at"]) or "ещё не запускалась"
            self.sched_table.setItem(i, 0, QTableWidgetItem(s["preset_name"]))
            self.sched_table.setItem(i, 1, QTableWidgetItem(period))
            self.sched_table.setItem(i, 2, QTableWidgetItem(last))
            self.sched_table.setItem(i, 3, QTableWidgetItem(s["last_result"] or "—"))
            del_btn = button("Удалить", "ghost")
            del_btn.clicked.connect(
                lambda _c, sid=s["id"]: self._on_delete_schedule(sid))
            self.sched_table.setCellWidget(i, 4, del_btn)
            self.sched_table.setRowHeight(i, 34)
        self.sched_table.resizeColumnsToContents()
        sched_header = self.sched_table.horizontalHeader()
        sched_header.setSectionResizeMode(3, QHeaderView.Stretch)
        # Same fix as watch.py's rules_table: resizeColumnsToContents
        # doesn't measure a cellWidget's sizeHint, so "Удалить" comes out
        # clipped without an explicit floor here.
        sched_header.setSectionResizeMode(4, QHeaderView.Fixed)
        self.sched_table.setColumnWidth(4, 108)
        self._fit_table(self.sched_table, 34, 200)

    def _on_delete_schedule(self, schedule_id: int) -> None:
        self.ctx.db.delete_export_schedule(schedule_id)
        self._refresh_schedules()

    def _copy_build_info(self) -> None:
        """Version, platform and where the data actually lives — the facts
        a problem report needs, on the clipboard rather than retyped."""
        import platform
        import sys
        from PySide6.QtWidgets import QApplication
        info = (
            f"ChatGrab {__version__}\n"
            f"{platform.system()} {platform.release()} ({platform.machine()})\n"
            f"Python {platform.python_version()}, сборка: "
            f"{'exe' if getattr(sys, 'frozen', False) else 'из исходников'}\n"
            f"Стиль: {QApplication.instance().property('chatgrab_base_style') or '—'}\n"
            f"Данные: {self.ctx.paths.data_dir}\n"
            f"Журнал: {self.ctx.paths.log_path}"
        )
        QApplication.clipboard().setText(info)
        QMessageBox.information(self, "Скопировано", info)

    def _refresh_diagnostics_status(self) -> None:
        session = diagnostics.current()
        if session is not None and session.active and session.path is not None:
            self.diag_status.setText(f"Идёт запись: {session.path.name}")
        elif self.diag_cb.isChecked():
            self.diag_status.setText("Запись включится при следующем запуске приложения.")
        else:
            self.diag_status.setText("Запись выключена.")

    def _save_diagnostics(self) -> None:
        wanted = self.diag_cb.isChecked()
        self.ctx.db.set_setting(diagnostics.SETTING_KEY, wanted)
        session = diagnostics.current()
        if not wanted and session is not None and session.active:
            # Stopping mid-run closes the current file cleanly, so it can be
            # handed over without waiting for the app to exit.
            session.stop()
            QMessageBox.information(
                self, "Диагностика",
                "Запись остановлена, файл закрыт и готов к отправке.")
        elif wanted and (session is None or not session.active):
            QMessageBox.information(
                self, "Диагностика",
                "Запись начнётся при следующем запуске приложения — "
                "так в файл попадёт и то, что происходит при старте.")
        self._refresh_diagnostics_status()

    def _open_diagnostics_dir(self) -> None:
        target = self.ctx.paths.data_dir / "diagnostics"
        target.mkdir(parents=True, exist_ok=True)
        open_in_explorer(target)

    def _save_tray(self) -> None:
        self.ctx.db.set_setting("tray_minimize_on_close", self.tray_close_cb.isChecked())
        wanted = self.autostart_cb.isChecked()
        actual = tray.set_autostart(wanted)
        # Reflect what the registry actually says, not what was asked —
        # a refused write should not leave a checkbox lying about it.
        self.autostart_cb.setChecked(actual)
        if wanted and not actual and tray.autostart_supported():
            self.tray_status.setText("Не удалось включить автозапуск — Windows отклонил запись в реестр.")
        else:
            self.tray_status.setText("Сохранено.")

    def _save_misc(self) -> None:
        self.ctx.db.set_setting("gap_notify_days", self.gap_days_spin.value())
        self.ctx.db.set_setting("markdown_header", self.md_header_edit.toPlainText())
        QMessageBox.information(self, "Сохранено", "Настройки обновлены.")
