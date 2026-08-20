"""П7: «Почта → Разбор» — the weights/threshold behind mail scoring, and a
live check against real, already-collected mail so tuning them isn't a
guessing game. See core/mail_triage.py for the rule itself.
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from ..context import AppContext
from ..util import fire, run_blocking
from ..widgets import button, card, h1, label, muted

_WEIGHT_LABELS = [
    ("direction_keyword", "Ключевое слово направления (тема, тело или вложение)"),
    ("request_phrase", "Запросный оборот («пришлите КП», «стоимость», «наличие», «объём»)"),
    ("lead_like_attachment", "Вложение похоже на заявку (имя файла или таблица)"),
    ("known_sender", "Отправитель уже встречался в заявках"),
    ("reply_in_thread", "Ответ в цепочке, где мы уже писали"),
    ("direction_stop_word", "Стоп-слово направления"),
    ("bulk_signal", "Признаки массовой рассылки (List-Unsubscribe/Precedence)"),
    ("noreply_address", "Адрес вида no-reply@/notification@"),
]


class MailTriageScreen(QWidget):
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
        outer.addWidget(h1("Разбор"))
        outer.addWidget(muted(
            "Каждое новое письмо получает балл — сумму сработавших сигналов ниже. "
            "Веса и порог настраиваются здесь; причины видны на каждом письме, "
            "чтобы правки не были догадкой."))
        outer.addSpacing(18)

        outer.addWidget(self._build_settings_card())
        outer.addSpacing(20)
        outer.addWidget(self._build_results_card())
        outer.addStretch(1)

        self._refresh_settings()
        self._run_preview()

    # ---- настройки --------------------------------------------------------
    def _build_settings_card(self) -> QWidget:
        c = card()
        lay = QVBoxLayout(c)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)
        lay.addWidget(label("ВЕСА СИГНАЛОВ", "kicker"))

        self._weight_fields: dict[str, QSpinBox] = {}
        for key, title in _WEIGHT_LABELS:
            row = QHBoxLayout()
            row.addWidget(QLabel(title), 1)
            spin = QSpinBox()
            spin.setRange(-100, 100)
            spin.setSuffix(" балл.")
            row.addWidget(spin)
            lay.addLayout(row)
            self._weight_fields[key] = spin

        lay.addSpacing(6)
        threshold_row = QHBoxLayout()
        threshold_row.addWidget(QLabel("Порог уведомления"), 1)
        self.threshold_field = QSpinBox()
        self.threshold_field.setRange(-200, 200)
        threshold_row.addWidget(self.threshold_field)
        lay.addLayout(threshold_row)

        cap_row = QHBoxLayout()
        cap_row.addWidget(QLabel("Уведомлений за один заход, не больше"), 1)
        self.cap_field = QSpinBox()
        self.cap_field.setRange(1, 100)
        cap_row.addWidget(self.cap_field)
        lay.addLayout(cap_row)

        self.llm_checkbox = QCheckBox("Второй проход LLM для спорных писем (балл 40–60), выключено по умолчанию")
        lay.addWidget(self.llm_checkbox)

        buttons_row = QHBoxLayout()
        save_btn = button("Сохранить", "primary")
        save_btn.clicked.connect(self._on_save)
        buttons_row.addWidget(save_btn)
        check_btn = button("Проверить с этими весами", "secondary")
        check_btn.clicked.connect(self._run_preview)
        buttons_row.addWidget(check_btn)
        rescan_btn = button("Пересчитать (сохранённые веса)", "ghost")
        rescan_btn.clicked.connect(self._on_rescan)
        buttons_row.addWidget(rescan_btn)
        lay.addLayout(buttons_row)

        self.settings_status = muted("")
        lay.addWidget(self.settings_status)
        return c

    def _refresh_settings(self) -> None:
        settings = self.ctx.mail_service.get_triage_settings()
        for key, spin in self._weight_fields.items():
            spin.setValue(int(settings["weights"][key]))
        self.threshold_field.setValue(int(settings["threshold"]))
        self.cap_field.setValue(int(settings["max_notifications_per_tick"]))
        self.llm_checkbox.setChecked(bool(settings["llm_borderline_enabled"]))

    def _form_settings(self) -> dict:
        return {
            "weights": {key: spin.value() for key, spin in self._weight_fields.items()},
            "threshold": self.threshold_field.value(),
            "max_notifications_per_tick": self.cap_field.value(),
            "llm_borderline_enabled": self.llm_checkbox.isChecked(),
        }

    def _on_save(self) -> None:
        self.ctx.mail_service.set_triage_settings(self._form_settings())
        self.settings_status.setText("Сохранено.")
        self._run_preview()

    def _on_rescan(self) -> None:
        self.settings_status.setText("Пересчитываю…")

        async def _go():
            return await run_blocking(self.ctx.mail_service.rescan_triage, None, 50)

        def on_error(e):
            self.settings_status.setText(f"Не получилось: {e}")

        task = fire(_go(), parent=self, on_error=on_error)

        def _apply(t):
            if t.cancelled() or t.exception() is not None:
                return
            self.settings_status.setText(f"Пересчитано, писем с баллом выше порога: {t.result()}.")
            self._run_preview()

        task.add_done_callback(_apply)

    # ---- проверка на живых письмах ----------------------------------------
    def _build_results_card(self) -> QWidget:
        c = card()
        lay = QVBoxLayout(c)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(6)
        lay.addWidget(label("ПОСЛЕДНИЕ 50 ПИСЕМ", "kicker"))
        self.results_box = QVBoxLayout()
        self.results_box.setSpacing(10)
        lay.addLayout(self.results_box)
        return c

    def _run_preview(self) -> None:
        while self.results_box.count():
            item = self.results_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        settings = self._form_settings()
        messages = self.ctx.db.list_recent_mail_messages(limit=50)
        if not messages:
            self.results_box.addWidget(muted("Писем ещё нет."))
            return
        for message in messages:
            result = self.ctx.mail_service.preview_score(message["id"], settings)
            if result is None:
                continue
            self.results_box.addWidget(self._build_result_row(message, *result))

    def _build_result_row(self, message, score: int, category: str, reasons: list[str]) -> QWidget:
        row = QWidget()
        lay = QVBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        subject = message["subject"] or "(без темы)"
        who = message["sender_name"] or message["sender_address"] or "—"
        head = QLabel(f"{score:+d} · {category} — {subject}")
        head.setWordWrap(True)
        head.setStyleSheet("font-weight: 600;")
        lay.addWidget(head)
        lay.addWidget(muted(who))
        if reasons:
            reasons_label = muted("; ".join(reasons))
            reasons_label.setWordWrap(True)
            lay.addWidget(reasons_label)
        else:
            lay.addWidget(muted("сигналов не сработало"))
        return row

    def on_show(self, **kwargs) -> None:
        self._refresh_settings()
        self._run_preview()
