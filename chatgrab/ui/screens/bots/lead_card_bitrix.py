"""Bitrix24 row on the lead card — send button + status line. Split out
of lead_card.py in Р5, the same composition pattern list_tab.py/
drafts_tab.py/analytics_tab.py already use on the Боты screen (see
bots/__init__.py). `on_changed` is called after a send is queued (and
again once the immediate drain attempt finishes) so the dialog that
embeds this panel can re-read the lead and refresh whatever else on
screen might care — the panel itself only ever touches its own two
widgets.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtWidgets import QHBoxLayout, QWidget

from ...context import AppContext
from ...format import short_dt
from ...util import fire
from ...widgets import button, muted
from ....integrations import bitrix


class LeadCardBitrixPanel(QWidget):
    def __init__(self, ctx: AppContext, lead_id: int,
                 on_changed: Callable[[], None] | None = None, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.lead_id = lead_id
        self._on_changed = on_changed or (lambda: None)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.send_btn = button("Отправить в Битрикс24", "secondary")
        self.send_btn.clicked.connect(self._on_send)
        row.addWidget(self.send_btn)
        self.status_label = muted("")
        row.addWidget(self.status_label, 1)

    def refresh(self, lead) -> None:
        configured = bitrix.get_webhook_url(self.ctx.db, self.ctx.security) is not None
        queue_entry = self.ctx.db.get_crm_queue_entry(self.lead_id)
        if not configured:
            self.send_btn.setEnabled(False)
            self.status_label.setText("Bitrix24 не настроен — задайте вебхук в «Настройках».")
        elif queue_entry is not None:
            self.send_btn.setEnabled(True)
            attempts = queue_entry["attempts"]
            self.status_label.setText(
                f"Отправка в очереди (попыток: {attempts})." if attempts else "Отправка в очереди.")
        elif lead["crm_id"]:
            self.send_btn.setEnabled(True)
            when = short_dt(lead["crm_synced_at"]) or "?"
            self.status_label.setText(f"В Bitrix24: ID {lead['crm_id']} (синхронизировано {when}).")
        else:
            self.send_btn.setEnabled(True)
            self.status_label.setText("Ещё не отправлялась в Bitrix24.")

    def _on_send(self) -> None:
        self.ctx.bitrix_sync_service.enqueue(self.lead_id)
        self._on_changed()
        # Ставит в очередь и сразу пробует — иначе клик ждал бы до 30
        # секунд следующего фонового тика без всякой видимой причины.
        # Фон остаётся страховкой на случай, если сети сейчас нет.
        task = fire(self.ctx.bitrix_sync_service.tick(), parent=self, on_error=lambda e: None)
        task.add_done_callback(lambda t: self._on_changed() if not t.cancelled() else None)
