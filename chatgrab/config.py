"""Bootstrap configuration: Telegram API credentials and file locations.

Kept in a plain JSON file next to the executable, separate from the code
and separate from the SQLite database, per the "не хранить в коде"
requirement. Never logged, never included in exports.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from .paths import PATHS, Paths


@dataclass
class AppConfig:
    api_id: str = ""
    api_hash: str = ""
    session_path: str = ""
    photos_dir: str = ""
    exports_dir: str = ""
    backups_dir: str = ""
    photos_enabled: bool = True

    @classmethod
    def load(cls, paths: Paths = PATHS) -> "AppConfig":
        paths.ensure()
        if paths.config_path.exists():
            try:
                raw = json.loads(paths.config_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                raw = {}
        else:
            raw = {}
        cfg = cls(
            api_id=str(raw.get("api_id", "")),
            api_hash=str(raw.get("api_hash", "")),
            session_path=raw.get("session_path") or str(paths.session_path),
            photos_dir=raw.get("photos_dir") or str(paths.photos_dir),
            exports_dir=raw.get("exports_dir") or str(paths.exports_dir),
            backups_dir=raw.get("backups_dir") or str(paths.backups_dir),
            photos_enabled=bool(raw.get("photos_enabled", True)),
        )
        cfg.save(paths)
        return cfg

    def save(self, paths: Paths = PATHS) -> None:
        paths.config_path.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.api_id and self.api_hash)
