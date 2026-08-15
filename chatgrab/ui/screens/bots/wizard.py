from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QRadioButton, QVBoxLayout, QHBoxLayout, QWidget,
)

from ...context import AppContext
from ...widgets import FieldRow, button, muted
from ....bots import preset_library as pl


class BotWizardDialog(QDialog):
    """Name, type, token/session choice, preset — matches the spec's
    "мастер создания бота" structure. Userbot-type bots need the parser's
    Telegram session already authorized (there's no second login); Bot API
    bots just need a token from @BotFather.

    С5: the preset picker now also lists the JSON preset library
    (presets/*.json) alongside the original b2b/b2c/custom seed. Picking
    one of those reveals 1-3 extra fields — the preset's own `variables`
    (a manager's display name, which directions apply, work hours) — this
    IS the "мастер установки" PLAN.md asks for, not a separate dialog:
    three or four questions on top of the bot fields already being asked
    here, not a whole new flow."""

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle("Новый бот")
        self.setMinimumWidth(480)
        self.created_bot_id: int | None = None
        self._preset_specs = {s["key"]: s for s in pl.list_preset_specs()}
        self._var_widgets: dict[str, QWidget] = {}

        lay = QVBoxLayout(self)

        self.name_row = FieldRow("Название бота", "например, «Приём заявок»")
        lay.addWidget(self.name_row)

        lay.addWidget(muted("Тип"))
        type_row = QHBoxLayout()
        self.type_bot_api = QRadioButton("Бот-ассистент (Bot API)")
        self.type_bot_api.setChecked(True)
        self.type_userbot = QRadioButton("Юзербот-триггеры (текущий аккаунт)")
        type_row.addWidget(self.type_bot_api)
        type_row.addWidget(self.type_userbot)
        lay.addLayout(type_row)

        self.type_hint = muted(
            "Бот-ассистент: приём личных сообщений через токен @BotFather, ведёт диалог по сценарию. "
            "Юзербот: реагирует на сообщения в чатах текущего аккаунта — использует уже выполненный вход, "
            "второй сессии не требует."
        )
        self.type_hint.setWordWrap(True)
        lay.addWidget(self.type_hint)

        self.token_row = FieldRow("Токен Bot API", "12345:AA... (выдаёт @BotFather)", password=True)
        lay.addWidget(self.token_row)

        self.userbot_status = QLabel("")
        self.userbot_status.setWordWrap(True)
        lay.addWidget(self.userbot_status)

        self.manager_row = FieldRow(
            "Кому пересылать заявки",
            "Telegram ID менеджера или @username",
        )
        lay.addWidget(self.manager_row)

        lay.addWidget(muted("Пресет"))
        self.preset_combo = QComboBox()
        self.preset_combo.addItem("B2B — длинная квалификация, маршрутизация менеджеру", "b2b")
        self.preset_combo.addItem("B2C — короткие автоответы, высокая частота", "b2c")
        self.preset_combo.addItem("Кастом — собрать с нуля", "custom")
        for key, spec in self._preset_specs.items():
            self.preset_combo.addItem(f"{spec['label']} — {spec['description']}", key)
        lay.addWidget(self.preset_combo)

        self.preset_hint = muted("")
        self.preset_hint.setWordWrap(True)
        lay.addWidget(self.preset_hint)

        self.variables_lay = QVBoxLayout()
        self.variables_lay.setSpacing(8)
        lay.addLayout(self.variables_lay)

        self.type_bot_api.toggled.connect(self._sync_type)
        self.preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        self._sync_type()
        self._on_preset_changed()

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel_btn = button("Отмена", "secondary")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        self.confirm_btn = button("Создать бота", "primary")
        self.confirm_btn.clicked.connect(self._on_confirm)
        btn_row.addWidget(self.confirm_btn)
        lay.addLayout(btn_row)

    def _sync_type(self) -> None:
        is_bot_api = self.type_bot_api.isChecked()
        self.token_row.setVisible(is_bot_api)
        self.userbot_status.setVisible(not is_bot_api)
        if not is_bot_api:
            authed = self.ctx.tg.authorized
            self.userbot_status.setText(
                "Текущий аккаунт подключён — юзербот будет использовать этот вход."
                if authed else
                "Аккаунт ещё не подключён — сначала войдите на экране «Подключение», "
                "иначе юзербот не сможет получать сообщения."
            )
            self.userbot_status.setStyleSheet(
                "color: #7fc79b; font-size: 12px;" if authed else "color: #e0a8b0; font-size: 12px;"
            )

    def _clear_variables(self) -> None:
        while self.variables_lay.count():
            item = self.variables_lay.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        self._var_widgets.clear()

    def _on_preset_changed(self, _index: int = 0) -> None:
        self._clear_variables()
        key = self.preset_combo.currentData()
        spec = self._preset_specs.get(key)
        if spec is None:
            self.preset_hint.setText("")
            return
        self.preset_hint.setText(spec["description"])
        for v in spec["variables"]:
            self.variables_lay.addWidget(muted(v["label"]))
            if v["type"] == "directions":
                picker = QListWidget()
                picker.setSelectionMode(QListWidget.NoSelection)
                picker.setMaximumHeight(110)
                directions = self.ctx.db.list_directions(enabled_only=True)
                for d in directions:
                    item = QListWidgetItem(d["name"])
                    item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                    item.setCheckState(Qt.Checked)
                    item.setData(Qt.UserRole, dict(d))
                    picker.addItem(item)
                if not directions:
                    self.variables_lay.addWidget(muted(
                        "Направлений пока нет — добавьте их на экране «Направления», "
                        "иначе пресет установится без них."))
                self.variables_lay.addWidget(picker)
                self._var_widgets[v["name"]] = picker
            else:
                field = QLineEdit()
                field.setText(str(v.get("default", "")))
                self.variables_lay.addWidget(field)
                self._var_widgets[v["name"]] = field

    def _collect_answers(self, spec: dict) -> dict:
        answers = {}
        for v in spec["variables"]:
            widget = self._var_widgets.get(v["name"])
            if v["type"] == "directions":
                picked = []
                for i in range(widget.count()):
                    item = widget.item(i)
                    if item.checkState() == Qt.Checked:
                        picked.append(item.data(Qt.UserRole))
                answers[v["name"]] = picked
            else:
                answers[v["name"]] = widget.text().strip() or v.get("default", "")
        return answers

    def _on_confirm(self) -> None:
        name = self.name_row.text().strip()
        if not name:
            QMessageBox.warning(self, "Не хватает данных", "Укажите название бота.")
            return
        is_bot_api = self.type_bot_api.isChecked()
        if is_bot_api:
            token = self.token_row.text().strip()
            if not token:
                QMessageBox.warning(self, "Не хватает данных", "Укажите токен Bot API от @BotFather.")
                return
        else:
            token = None
            if not self.ctx.tg.authorized:
                QMessageBox.warning(
                    self, "Аккаунт не подключён",
                    "Сначала войдите в Telegram на экране «Подключение» — юзерботу нужен активный вход."
                )
                return

        preset = self.preset_combo.currentData()
        manager = self.manager_row.text().strip() or None
        type_ = "bot_api" if is_bot_api else "userbot"
        spec = self._preset_specs.get(preset)
        answers = self._collect_answers(spec) if spec else None
        try:
            self.created_bot_id = self.ctx.bot_manager.create_bot(
                name, type_, token, preset, manager, preset_answers=answers)
        except Exception as e:
            QMessageBox.warning(self, "Не получилось создать бота", str(e))
            return
        self.accept()
