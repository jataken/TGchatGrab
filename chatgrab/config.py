"""Bootstrap configuration: Telegram API credentials and file locations.

Kept in a plain JSON file next to the executable, separate from the code
and separate from the SQLite database, per the "не хранить в коде"
requirement. Never logged, never included in exports.

When master-password protection is on (see security.py), api_hash is
encrypted at rest (api_hash_enc) and the plaintext api_hash field is
never written to this file — only ever held in memory for the running
session, after the vault has been unlocked.
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
    master_password_enabled: bool = False
    kdf_salt: str = ""
    api_hash_enc: str = ""

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
            master_password_enabled=bool(raw.get("master_password_enabled", False)),
            kdf_salt=str(raw.get("kdf_salt", "")),
            api_hash_enc=str(raw.get("api_hash_enc", "")),
        )
        cfg.save(paths)
        return cfg

    def save(self, paths: Paths = PATHS) -> None:
        data = asdict(self)
        if self.master_password_enabled:
            # The plaintext value only ever exists in memory for the
            # running session (populated by SecurityService.unlock) —
            # never let it reach disk once a master password is set.
            data["api_hash"] = ""
        paths.config_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @property
    def is_configured(self) -> bool:
        api_hash_present = bool(self.api_hash or self.api_hash_enc) if self.master_password_enabled \
            else bool(self.api_hash)
        return bool(self.api_id) and api_hash_present
