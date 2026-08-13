from __future__ import annotations

import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QSplitter, QVBoxLayout, QWidget,
)

from ...context import AppContext
from ...widgets import button, card, muted
from .common import populate_bot_picker

_TRIGGER_TYPES = [
    ("incoming_dm", "Входящее личное сообщение боту"),
    ("keyword", "Ключевое слово (в любом чате бота)"),
    ("command", "Команда (/start, /price, …)"),
    ("chat_message", "Новое сообщение в чате X (юзербот)"),
    ("schedule", "Расписание"),
    ("inactivity", "Неактивность контакта N дней"),
]
_ACTION_TYPES = [
    ("send_dm", "Отправить сообщение в личку"),
    ("run_scenario", "Запустить сценарий"),
    ("save_lead", "Сохранить заявку"),
    ("forward_lead", "Переслать заявку менеджеру"),
    ("notify_manager", "Уведомить менеджера (авто-текст)"),
    ("notify", "Уведомить менеджера (свой текст)"),
    ("tag", "Проставить тег контакту"),
]
_TRIGGER_LABELS = dict(_TRIGGER_TYPES)
_ACTION_LABELS = dict(_ACTION_TYPES)


class RulesTab(QWidget):
    """Trigger → action rule editor. A rule is one trigger row plus its
    ordered actions; both are plain DB rows, edited directly here rather
    than through a drag-and-drop canvas (form fields map to Qt better)."""

    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.selected_bot_id: int | None = None
        self.selected_trigger_id: int | None = None

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
        left_lay.addWidget(muted("Триггеры"))
        self.trigger_list = QListWidget()
        self.trigger_list.currentItemChanged.connect(self._on_trigger_selected)
        left_lay.addWidget(self.trigger_list, 1)
        add_trigger_btn = button("＋ Добавить триггер", "secondary")
        add_trigger_btn.clicked.connect(self._on_add_trigger)
        left_lay.addWidget(add_trigger_btn)
        split.addWidget(left)

        right = card()
        self.right_lay = QVBoxLayout(right)
        self.right_lay.setContentsMargins(14, 14, 14, 14)
        self.empty_hint = muted("Выберите триггер слева или создайте новый.")
        self.right_lay.addWidget(self.empty_hint)

        self.detail_widget = QWidget()
        detail_lay = QVBoxLayout(self.detail_widget)
        detail_lay.setContentsMargins(0, 0, 0, 0)

        type_row = QHBoxLayout()
        type_row.addWidget(muted("Тип"))
        self.type_combo = QComboBox()
        for key, label in _TRIGGER_TYPES:
            self.type_combo.addItem(label, key)
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        type_row.addWidget(self.type_combo, 1)
        self.enabled_cb = QCheckBox("Включён")
        self.enabled_cb.setChecked(True)
        type_row.addWidget(self.enabled_cb)
        detail_lay.addLayout(type_row)

        self.config_hint = muted("")
        self.config_hint.setWordWrap(True)
        detail_lay.addWidget(self.config_hint)
        self.config_input = QLineEdit()
        self.config_input.setPlaceholderText("ключевые слова через запятую")
        detail_lay.addWidget(self.config_input)
        self.chat_id_input = QLineEdit()
        self.chat_id_input.setPlaceholderText("chat_id (необязательно — иначе применяется ко всем чатам бота)")
        detail_lay.addWidget(self.chat_id_input)

        save_row = QHBoxLayout()
        save_trigger_btn = button("Сохранить триггер", "primary")
        save_trigger_btn.clicked.connect(self._on_save_trigger)
        save_row.addWidget(save_trigger_btn)
        delete_trigger_btn = button("Удалить триггер", "ghost")
        delete_trigger_btn.clicked.connect(self._on_delete_trigger)
        save_row.addWidget(delete_trigger_btn)
        save_row.addStretch(1)
        detail_lay.addLayout(save_row)

        detail_lay.addWidget(muted("Действия (выполняются по порядку)"))
        self.action_list = QListWidget()
        self.action_list.setMaximumHeight(160)
        detail_lay.addWidget(self.action_list)

        action_add_row = QHBoxLayout()
        self.action_type_combo = QComboBox()
        for key, label in _ACTION_TYPES:
            self.action_type_combo.addItem(label, key)
        action_add_row.addWidget(self.action_type_combo, 1)
        self.action_config_input = QLineEdit()
        self.action_config_input.setPlaceholderText("текст сообщения / id сценария / тег — по типу действия")
        action_add_row.addWidget(self.action_config_input, 1)
        add_action_btn = button("＋", "secondary")
        add_action_btn.clicked.connect(self._on_add_action)
        action_add_row.addWidget(add_action_btn)
        detail_lay.addLayout(action_add_row)

        remove_action_btn = button("Удалить выбранное действие", "ghost")
        remove_action_btn.clicked.connect(self._on_remove_action)
        detail_lay.addWidget(remove_action_btn)

        self.detail_widget.setVisible(False)
        self.right_lay.addWidget(self.detail_widget, 1)
        split.addWidget(right)
        split.setSizes([260, 560])

    def on_show(self) -> None:
        populate_bot_picker(self.ctx, self.bot_picker)
        # populate_bot_picker blocks signals while setting the index, so
        # currentIndexChanged never fires on first load — read it directly
        # instead of relying on the (silent) signal to set selected_bot_id.
        self.selected_bot_id = self.bot_picker.currentData()
        self._reload_triggers()

    def _on_bot_changed(self, _index: int) -> None:
        self.selected_bot_id = self.bot_picker.currentData()
        self._reload_triggers()

    def _reload_triggers(self) -> None:
        self.trigger_list.clear()
        self.selected_trigger_id = None
        self.detail_widget.setVisible(False)
        self.empty_hint.setVisible(True)
        if self.selected_bot_id is None:
            return
        for trig in self.ctx.db.list_triggers(self.selected_bot_id):
            label = _TRIGGER_LABELS.get(trig["type"], trig["type"])
            suffix = "" if trig["enabled"] else " (выключен)"
            item = QListWidgetItem(f"{label}{suffix}")
            item.setData(Qt.UserRole, trig["id"])
            self.trigger_list.addItem(item)

    def _on_trigger_selected(self, current: QListWidgetItem, _prev) -> None:
        if current is None:
            self.detail_widget.setVisible(False)
            self.empty_hint.setVisible(True)
            self.selected_trigger_id = None
            return
        self.selected_trigger_id = current.data(Qt.UserRole)
        trig = self.ctx.db.get_trigger(self.selected_trigger_id)
        if not trig:
            return
        self.empty_hint.setVisible(False)
        self.detail_widget.setVisible(True)
        idx = self.type_combo.findData(trig["type"])
        self.type_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.enabled_cb.setChecked(bool(trig["enabled"]))
        cfg = json.loads(trig["config"])
        if trig["type"] in ("keyword", "chat_message"):
            self.config_input.setText(", ".join(cfg.get("keywords", [])))
        elif trig["type"] == "command":
            self.config_input.setText(cfg.get("command", ""))
        else:
            self.config_input.setText("")
        self.chat_id_input.setText(str(cfg.get("chat_id", "")) if cfg.get("chat_id") is not None else "")
        self._on_type_changed()
        self._reload_actions()

    def _on_type_changed(self) -> None:
        ttype = self.type_combo.currentData()
        hints = {
            "incoming_dm": "Срабатывает на любое личное сообщение боту — без дополнительной настройки.",
            "keyword": "Слова через запятую — сработает, если хотя бы одно встретится в тексте.",
            "command": "Одна команда без «/», например: start",
            "chat_message": "Слова через запятую (пусто = любое сообщение). Укажите chat_id ниже, "
                             "иначе применится ко всем отслеживаемым чатам юзербота.",
            "schedule": "Расписание настраивается отдельно — пока срабатывает как заглушка.",
            "inactivity": "Порог неактивности в днях — задаётся в поле ниже.",
        }
        self.config_hint.setText(hints.get(ttype, ""))
        self.config_input.setVisible(ttype in ("keyword", "command", "chat_message", "inactivity"))
        self.chat_id_input.setVisible(ttype == "chat_message")

    def _build_config(self) -> dict:
        ttype = self.type_combo.currentData()
        if ttype in ("keyword", "chat_message"):
            words = [w.strip() for w in self.config_input.text().split(",") if w.strip()]
            cfg = {"keywords": words}
            if ttype == "chat_message":
                raw = self.chat_id_input.text().strip()
                if raw:
                    try:
                        cfg["chat_id"] = int(raw)
                    except ValueError:
                        pass
            return cfg
        if ttype == "command":
            return {"command": self.config_input.text().strip()}
        if ttype == "inactivity":
            raw = self.config_input.text().strip()
            return {"days": int(raw)} if raw.isdigit() else {}
        return {}

    def _on_add_trigger(self) -> None:
        if self.selected_bot_id is None:
            QMessageBox.information(self, "Выберите бота", "Сначала выберите бота вверху экрана.")
            return
        trig_id = self.ctx.db.add_trigger(self.selected_bot_id, "incoming_dm", {})
        self._reload_triggers()
        for i in range(self.trigger_list.count()):
            if self.trigger_list.item(i).data(Qt.UserRole) == trig_id:
                self.trigger_list.setCurrentRow(i)
                break

    def _on_save_trigger(self) -> None:
        if self.selected_trigger_id is None:
            return
        self.ctx.db.set_trigger_field(
            self.selected_trigger_id, type=self.type_combo.currentData(),
            config=self._build_config(), enabled=1 if self.enabled_cb.isChecked() else 0,
        )
        self._reload_triggers()

    def _on_delete_trigger(self) -> None:
        if self.selected_trigger_id is None:
            return
        self.ctx.db.delete_trigger(self.selected_trigger_id)
        self._reload_triggers()

    def _reload_actions(self) -> None:
        self.action_list.clear()
        if self.selected_trigger_id is None:
            return
        for action in self.ctx.db.list_actions(self.selected_trigger_id):
            cfg = json.loads(action["config"])
            summary = cfg.get("text") or cfg.get("tag") or (f"сценарий {cfg['scenario_id']}" if "scenario_id" in cfg else "")
            label = _ACTION_LABELS.get(action["type"], action["type"])
            item = QListWidgetItem(f"{label}" + (f" — {summary}" if summary else ""))
            item.setData(Qt.UserRole, action["id"])
            self.action_list.addItem(item)

    def _on_add_action(self) -> None:
        if self.selected_trigger_id is None:
            return
        atype = self.action_type_combo.currentData()
        raw = self.action_config_input.text().strip()
        cfg: dict = {}
        if atype in ("send_dm", "notify"):
            cfg = {"text": raw}
        elif atype == "run_scenario":
            if raw.isdigit():
                cfg = {"scenario_id": int(raw)}
        elif atype == "tag":
            cfg = {"tag": raw}
        order = self.action_list.count()
        self.ctx.db.add_action(self.selected_trigger_id, atype, cfg, order_index=order)
        self.action_config_input.clear()
        self._reload_actions()

    def _on_remove_action(self) -> None:
        item = self.action_list.currentItem()
        if item is None:
            return
        self.ctx.db.delete_action(item.data(Qt.UserRole))
        self._reload_actions()
