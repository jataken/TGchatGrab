from __future__ import annotations

import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QSizePolicy, QSpinBox, QSplitter,
    QStackedWidget, QTimeEdit, QVBoxLayout, QWidget,
)

from ... import theme
from ...context import AppContext
from ...widgets import Card, TabletCheckBox, button, dashed_button, label, muted

_TRIGGER_TYPES = [
    ("incoming_dm", "Написали боту в личку"),
    ("keyword", "Ключевое слово в любом сообщении"),
    ("command", "Команда (/start, /price, …)"),
    ("chat_message", "Сообщение в отслеживаемом чате"),
    ("schedule", "По расписанию"),
    ("inactivity", "Контакт молчит N дней"),
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
_WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

# Action types that send text, and so can use a template.
_TEXT_ACTIONS = ("send_dm", "notify")


class RulesTab(QWidget):
    """Trigger → action rule editor. A rule is one trigger row plus its
    ordered actions.

    Every reference to another object — a tracked chat, a scenario, a
    message template — is chosen from a list, never typed as a numeric id.
    Problems that would make a rule silently do nothing (no actions, a
    deleted scenario, a manager notification with no manager set) are
    shown on the rule itself rather than discovered at runtime."""

    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.selected_bot_id: int | None = None
        self.selected_trigger_id: int | None = None
        ctx.bot_selection.changed.connect(self._on_bot_changed)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        split = QSplitter()
        outer.addWidget(split, 1)

        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 8, 0)
        left_lay.addWidget(label("ТРИГГЕРЫ", "kicker"))
        self.trigger_list = QListWidget()
        self.trigger_list.currentItemChanged.connect(self._on_trigger_selected)
        left_lay.addWidget(self.trigger_list, 1)
        add_trigger_btn = dashed_button("＋ Новый триггер")
        add_trigger_btn.clicked.connect(self._on_add_trigger)
        left_lay.addWidget(add_trigger_btn)
        split.addWidget(left)

        right = Card()
        self.right_lay = QVBoxLayout(right)
        self.right_lay.setContentsMargins(14, 14, 14, 14)
        self.empty_hint = muted("Выберите правило слева или создайте новое.")
        self.right_lay.addWidget(self.empty_hint)

        self.detail_widget = QWidget()
        detail_lay = QVBoxLayout(self.detail_widget)
        detail_lay.setContentsMargins(0, 0, 0, 0)

        type_row = QHBoxLayout()
        type_row.addWidget(muted("Когда"))
        self.type_combo = QComboBox()
        for key, lbl in _TRIGGER_TYPES:
            self.type_combo.addItem(lbl, key)
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        type_row.addWidget(self.type_combo, 1)
        self.enabled_cb = TabletCheckBox("Включено")
        self.enabled_cb.setChecked(True)
        type_row.addWidget(self.enabled_cb)
        detail_lay.addLayout(type_row)

        self.config_hint = muted("")
        self.config_hint.setWordWrap(True)
        detail_lay.addWidget(self.config_hint)

        # One page per trigger type — the alternative (a single free-text
        # box meaning something different per type) is what forced users to
        # know that "chat_id" wanted a raw negative number.
        self.trigger_config = QStackedWidget()
        self._build_trigger_pages()
        detail_lay.addWidget(self.trigger_config)

        save_row = QHBoxLayout()
        save_trigger_btn = button("Сохранить правило", "primary")
        save_trigger_btn.clicked.connect(self._on_save_trigger)
        save_row.addWidget(save_trigger_btn)
        delete_trigger_btn = button("Удалить правило", "ghost")
        delete_trigger_btn.clicked.connect(self._on_delete_trigger)
        save_row.addWidget(delete_trigger_btn)
        save_row.addStretch(1)
        detail_lay.addLayout(save_row)

        self.problem_label = QLabel("")
        self.problem_label.setTextFormat(Qt.PlainText)
        self.problem_label.setWordWrap(True)
        self.problem_label.setStyleSheet(f"color: {theme.WARN}; font-size: 12px;")
        detail_lay.addWidget(self.problem_label)

        detail_lay.addSpacing(8)
        detail_lay.addWidget(muted("Что сделать (по порядку)"))
        self.action_list = QListWidget()
        self.action_list.setMaximumHeight(150)
        detail_lay.addWidget(self.action_list)

        action_add_row = QHBoxLayout()
        self.action_type_combo = QComboBox()
        for key, lbl in _ACTION_TYPES:
            self.action_type_combo.addItem(lbl, key)
        self.action_type_combo.currentIndexChanged.connect(self._on_action_type_changed)
        action_add_row.addWidget(self.action_type_combo, 1)
        self.action_config = QStackedWidget()
        self._build_action_pages()
        action_add_row.addWidget(self.action_config, 1)
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
        split.setSizes([280, 620])

    # ---- config pages ---------------------------------------------------
    def _build_trigger_pages(self) -> None:
        self._trigger_pages: dict[str, int] = {}

        blank = QWidget()
        self._trigger_pages["_blank"] = self.trigger_config.addWidget(blank)

        words_page = QWidget()
        words_lay = QVBoxLayout(words_page)
        words_lay.setContentsMargins(0, 0, 0, 0)
        self.keywords_input = QLineEdit()
        self.keywords_input.setPlaceholderText("слова через запятую: куплю, ищем поставщика, нужен объём")
        words_lay.addWidget(self.keywords_input)
        self._trigger_pages["keyword"] = self.trigger_config.addWidget(words_page)

        cmd_page = QWidget()
        cmd_lay = QVBoxLayout(cmd_page)
        cmd_lay.setContentsMargins(0, 0, 0, 0)
        self.command_input = QLineEdit()
        self.command_input.setPlaceholderText("одна команда без «/», например: price")
        cmd_lay.addWidget(self.command_input)
        self._trigger_pages["command"] = self.trigger_config.addWidget(cmd_page)

        chat_page = QWidget()
        chat_lay = QVBoxLayout(chat_page)
        chat_lay.setContentsMargins(0, 0, 0, 0)
        chat_row = QHBoxLayout()
        chat_row.addWidget(muted("Чат"))
        self.chat_combo = QComboBox()
        chat_row.addWidget(self.chat_combo, 1)
        chat_lay.addLayout(chat_row)
        self.chat_keywords_input = QLineEdit()
        self.chat_keywords_input.setPlaceholderText("слова через запятую (пусто — любое сообщение в чате)")
        chat_lay.addWidget(self.chat_keywords_input)
        self._trigger_pages["chat_message"] = self.trigger_config.addWidget(chat_page)

        sched_page = QWidget()
        sched_lay = QVBoxLayout(sched_page)
        sched_lay.setContentsMargins(0, 0, 0, 0)
        time_row = QHBoxLayout()
        time_row.addWidget(muted("Во сколько"))
        self.schedule_time = QTimeEdit()
        self.schedule_time.setDisplayFormat("HH:mm")
        time_row.addWidget(self.schedule_time)
        time_row.addStretch(1)
        sched_lay.addLayout(time_row)
        days_row = QHBoxLayout()
        self.weekday_boxes: list[QCheckBox] = []
        for name in _WEEKDAYS:
            cb = QCheckBox(name)
            cb.setChecked(True)
            self.weekday_boxes.append(cb)
            days_row.addWidget(cb)
        days_row.addStretch(1)
        sched_lay.addLayout(days_row)
        self._trigger_pages["schedule"] = self.trigger_config.addWidget(sched_page)

        inact_page = QWidget()
        inact_lay = QHBoxLayout(inact_page)
        inact_lay.setContentsMargins(0, 0, 0, 0)
        inact_lay.addWidget(muted("Молчит дольше"))
        self.inactivity_days = QSpinBox()
        self.inactivity_days.setRange(1, 365)
        self.inactivity_days.setValue(7)
        self.inactivity_days.setSuffix(" сут.")
        inact_lay.addWidget(self.inactivity_days)
        inact_lay.addStretch(1)
        self._trigger_pages["inactivity"] = self.trigger_config.addWidget(inact_page)

    def _build_action_pages(self) -> None:
        self._action_pages: dict[str, int] = {}

        blank = QWidget()
        self._action_pages["_blank"] = self.action_config.addWidget(blank)

        text_page = QWidget()
        text_lay = QHBoxLayout(text_page)
        text_lay.setContentsMargins(0, 0, 0, 0)
        self.template_combo = QComboBox()
        text_lay.addWidget(self.template_combo, 1)
        self._action_pages["text"] = self.action_config.addWidget(text_page)

        scenario_page = QWidget()
        sc_lay = QHBoxLayout(scenario_page)
        sc_lay.setContentsMargins(0, 0, 0, 0)
        self.scenario_combo = QComboBox()
        sc_lay.addWidget(self.scenario_combo, 1)
        self._action_pages["run_scenario"] = self.action_config.addWidget(scenario_page)

        tag_page = QWidget()
        tag_lay = QHBoxLayout(tag_page)
        tag_lay.setContentsMargins(0, 0, 0, 0)
        self.tag_input = QLineEdit()
        self.tag_input.setPlaceholderText("тег, например: интерес-цена")
        tag_lay.addWidget(self.tag_input, 1)
        self._action_pages["tag"] = self.action_config.addWidget(tag_page)

    # ---- lifecycle -------------------------------------------------------
    def on_show(self) -> None:
        self.selected_bot_id = self.ctx.bot_selection.current
        self._reload_reference_lists()
        self._reload_triggers()

    def _on_bot_changed(self, bot_id) -> None:
        self.selected_bot_id = bot_id
        self._reload_reference_lists()
        self._reload_triggers()

    def _reload_reference_lists(self) -> None:
        """Refill the pickers that reference other objects — the tracked
        chats, and this bot's scenarios and templates."""
        current_chat = self.chat_combo.currentData()
        self.chat_combo.clear()
        self.chat_combo.addItem("любой отслеживаемый чат", None)
        for chat in self.ctx.db.list_chats():
            self.chat_combo.addItem(chat["title"], chat["chat_id"])
        idx = self.chat_combo.findData(current_chat)
        self.chat_combo.setCurrentIndex(max(0, idx))

        self.scenario_combo.clear()
        self.template_combo.clear()
        if self.selected_bot_id is None:
            return
        for sc in self.ctx.db.list_scenarios(self.selected_bot_id):
            self.scenario_combo.addItem(sc["name"], sc["id"])
        for tpl in self.ctx.db.list_templates(self.selected_bot_id):
            self.template_combo.addItem(tpl["name"], tpl["id"])

    # ---- trigger list ----------------------------------------------------
    def _reload_triggers(self) -> None:
        self.trigger_list.clear()
        self.selected_trigger_id = None
        self.detail_widget.setVisible(False)
        self.empty_hint.setVisible(True)
        if self.selected_bot_id is None:
            return
        for trig in self.ctx.db.list_triggers(self.selected_bot_id):
            lbl = _TRIGGER_LABELS.get(trig["type"], trig["type"])
            suffix = "" if trig["enabled"] else "  (выключено)"
            problems = self._problems_for(trig)
            if problems:
                suffix += "   ⚠"
            item = QListWidgetItem(f"{lbl}{suffix}")
            item.setData(Qt.UserRole, trig["id"])
            if problems:
                item.setToolTip("\n".join(problems))
            self.trigger_list.addItem(item)

    def _problems_for(self, trig) -> list[str]:
        """Everything about this rule that would make it quietly do nothing."""
        db = self.ctx.db
        problems: list[str] = []
        actions = db.list_actions(trig["id"])
        if not actions:
            problems.append("У правила нет ни одного действия — сработает и ничего не сделает.")

        cfg = json.loads(trig["config"]) if trig["config"] else {}
        if trig["type"] == "keyword" and not cfg.get("keywords"):
            problems.append("Не заданы ключевые слова — правило никогда не сработает.")
        if trig["type"] == "command" and not cfg.get("command"):
            problems.append("Не задана команда — правило никогда не сработает.")
        if trig["type"] == "inactivity" and not cfg.get("days"):
            problems.append("Не задано число суток молчания.")
        if trig["type"] == "chat_message":
            chat_id = cfg.get("chat_id")
            if chat_id is not None and db.get_chat(chat_id) is None:
                problems.append("Выбранный чат больше не отслеживается.")

        bot = db.get_bot(trig["bot_id"])
        for action in actions:
            acfg = json.loads(action["config"]) if action["config"] else {}
            if action["type"] == "run_scenario":
                sc_id = acfg.get("scenario_id")
                if sc_id is None or db.get_scenario(sc_id) is None:
                    problems.append("Действие «запустить сценарий» ссылается на удалённый сценарий.")
            if action["type"] in _TEXT_ACTIONS:
                tpl_id = acfg.get("template_id")
                if tpl_id is not None and db.get_template(tpl_id) is None:
                    problems.append("Действие отправки ссылается на удалённый шаблон.")
                elif tpl_id is None and not acfg.get("text"):
                    problems.append("У действия отправки нет ни шаблона, ни текста.")
            if action["type"] in ("notify", "notify_manager", "forward_lead"):
                if bot and not bot["manager_chat_id"]:
                    problems.append("У бота не задан менеджер — уведомлять некого.")
        return problems

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

        cfg = json.loads(trig["config"]) if trig["config"] else {}
        self.keywords_input.setText(", ".join(cfg.get("keywords", []))
                                     if trig["type"] == "keyword" else "")
        self.command_input.setText(cfg.get("command", "") if trig["type"] == "command" else "")
        if trig["type"] == "chat_message":
            self.chat_keywords_input.setText(", ".join(cfg.get("keywords", [])))
            chat_idx = self.chat_combo.findData(cfg.get("chat_id"))
            self.chat_combo.setCurrentIndex(max(0, chat_idx))
        if trig["type"] == "schedule":
            from PySide6.QtCore import QTime
            at = QTime.fromString(cfg.get("at", "10:00"), "HH:mm")
            if at.isValid():
                self.schedule_time.setTime(at)
            days = cfg.get("days", list(range(7)))
            for i, cb in enumerate(self.weekday_boxes):
                cb.setChecked(i in days)
        if trig["type"] == "inactivity":
            self.inactivity_days.setValue(int(cfg.get("days") or 7))

        self._on_type_changed()
        self._reload_actions()
        self.problem_label.setText("\n".join(self._problems_for(trig)))

    def _on_type_changed(self) -> None:
        ttype = self.type_combo.currentData()
        hints = {
            "incoming_dm": "Любое личное сообщение боту — без дополнительной настройки.",
            "keyword": "Сработает, если хотя бы одно слово встретится в тексте сообщения.",
            "command": "Одна команда без «/», например: price",
            "chat_message": "Сообщение в чате, который вы уже собираете на экране «Источники».",
            "schedule": "Срабатывает раз в сутки в выбранное время, по выбранным дням.",
            "inactivity": "Срабатывает один раз, когда контакт молчит дольше заданного срока. "
                           "Если он ответит и снова замолчит — сработает снова.",
        }
        self.config_hint.setText(hints.get(ttype, ""))
        page = self._trigger_pages.get(ttype, self._trigger_pages["_blank"])
        self.trigger_config.setCurrentIndex(page)
        # A QStackedWidget otherwise reserves the height of its tallest
        # page for every page, leaving a dead gap under the short ones.
        for i in range(self.trigger_config.count()):
            policy = QSizePolicy.Preferred if i == page else QSizePolicy.Ignored
            w = self.trigger_config.widget(i)
            w.setSizePolicy(QSizePolicy.Preferred, policy)
        self.trigger_config.adjustSize()

    def _build_config(self) -> dict:
        ttype = self.type_combo.currentData()
        if ttype == "keyword":
            return {"keywords": [w.strip() for w in self.keywords_input.text().split(",") if w.strip()]}
        if ttype == "command":
            return {"command": self.command_input.text().strip().lstrip("/")}
        if ttype == "chat_message":
            cfg: dict = {
                "keywords": [w.strip() for w in self.chat_keywords_input.text().split(",") if w.strip()]
            }
            chat_id = self.chat_combo.currentData()
            if chat_id is not None:
                cfg["chat_id"] = chat_id
            return cfg
        if ttype == "schedule":
            return {
                "at": self.schedule_time.time().toString("HH:mm"),
                "days": [i for i, cb in enumerate(self.weekday_boxes) if cb.isChecked()],
            }
        if ttype == "inactivity":
            return {"days": self.inactivity_days.value()}
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
        current = self.selected_trigger_id
        self._reload_triggers()
        for i in range(self.trigger_list.count()):
            if self.trigger_list.item(i).data(Qt.UserRole) == current:
                self.trigger_list.setCurrentRow(i)
                break

    def _on_delete_trigger(self) -> None:
        if self.selected_trigger_id is None:
            return
        n_actions = len(self.ctx.db.list_actions(self.selected_trigger_id))
        if QMessageBox.question(
            self, "Удалить правило",
            f"Удалить правило вместе с его действиями ({n_actions})? Отменить это нельзя."
        ) != QMessageBox.Yes:
            return
        self.ctx.db.delete_trigger(self.selected_trigger_id)
        self._reload_triggers()

    # ---- actions ---------------------------------------------------------
    def _on_action_type_changed(self) -> None:
        atype = self.action_type_combo.currentData()
        if atype in _TEXT_ACTIONS:
            key = "text"
        elif atype in ("run_scenario", "tag"):
            key = atype
        else:
            key = "_blank"
        self.action_config.setCurrentIndex(self._action_pages[key])

    def _reload_actions(self) -> None:
        self.action_list.clear()
        if self.selected_trigger_id is None:
            return
        db = self.ctx.db
        for action in db.list_actions(self.selected_trigger_id):
            cfg = json.loads(action["config"]) if action["config"] else {}
            summary = ""
            if action["type"] in _TEXT_ACTIONS:
                tpl_id = cfg.get("template_id")
                if tpl_id is not None:
                    tpl = db.get_template(tpl_id)
                    summary = f"шаблон «{tpl['name']}»" if tpl else "шаблон удалён ⚠"
                elif cfg.get("text"):
                    summary = cfg["text"][:40]
            elif action["type"] == "run_scenario":
                sc = db.get_scenario(cfg["scenario_id"]) if "scenario_id" in cfg else None
                summary = f"«{sc['name']}»" if sc else "сценарий удалён ⚠"
            elif action["type"] == "tag":
                summary = cfg.get("tag", "")
            lbl = _ACTION_LABELS.get(action["type"], action["type"])
            item = QListWidgetItem(f"{lbl}" + (f" — {summary}" if summary else ""))
            item.setData(Qt.UserRole, action["id"])
            self.action_list.addItem(item)

    def _on_add_action(self) -> None:
        if self.selected_trigger_id is None:
            return
        atype = self.action_type_combo.currentData()
        cfg: dict = {}
        if atype in _TEXT_ACTIONS:
            tpl_id = self.template_combo.currentData()
            if tpl_id is None:
                QMessageBox.information(
                    self, "Нужен шаблон",
                    "У этого бота ещё нет шаблонов сообщений. Создайте шаблон "
                    "в «Шаблоны сообщений» — там же задаются переменные вида {name}."
                )
                return
            cfg = {"template_id": tpl_id}
        elif atype == "run_scenario":
            sc_id = self.scenario_combo.currentData()
            if sc_id is None:
                QMessageBox.information(
                    self, "Нужен сценарий",
                    "У этого бота ещё нет сценариев. Создайте его на экране «Сценарий»."
                )
                return
            cfg = {"scenario_id": sc_id}
        elif atype == "tag":
            tag = self.tag_input.text().strip()
            if not tag:
                QMessageBox.information(self, "Нужен тег", "Укажите тег для контакта.")
                return
            cfg = {"tag": tag}

        order = self.action_list.count()
        self.ctx.db.add_action(self.selected_trigger_id, atype, cfg, order_index=order)
        self.tag_input.clear()
        self._reload_actions()
        trig = self.ctx.db.get_trigger(self.selected_trigger_id)
        if trig:
            self.problem_label.setText("\n".join(self._problems_for(trig)))

    def _on_remove_action(self) -> None:
        item = self.action_list.currentItem()
        if item is None:
            return
        self.ctx.db.delete_action(item.data(Qt.UserRole))
        self._reload_actions()
        trig = self.ctx.db.get_trigger(self.selected_trigger_id)
        if trig:
            self.problem_label.setText("\n".join(self._problems_for(trig)))
