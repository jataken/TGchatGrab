"""Generic app_settings key/value store — every app-wide setting that
isn't its own column somewhere (Bitrix24/LLM credentials, tray behaviour,
diagnostics toggle, and so on) goes through this one table."""
from __future__ import annotations

import json
from typing import Any


class SettingsMixin:
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value FROM app_settings WHERE key = ?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return default

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO app_settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )
