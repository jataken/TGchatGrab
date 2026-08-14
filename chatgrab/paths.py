"""Filesystem layout. Everything lives next to the executable (or the
project root when run from source), so the whole folder is portable —
copy it anywhere and the app keeps working with the same data."""
from __future__ import annotations

import sys
from pathlib import Path


def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        # PyInstaller onefile: sys.executable is the real .exe location,
        # not the temp extraction dir — safe to anchor data next to it.
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_path(*parts: str) -> Path:
    """Bundled read-only assets (icons, etc.) — these ship *inside* the
    onefile exe and are unpacked to PyInstaller's temp extraction dir at
    runtime (sys._MEIPASS), unlike user data which lives next to the exe."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", base_dir()))
    else:
        base = Path(__file__).resolve().parent.parent
    return base.joinpath(*parts)


class Paths:
    def __init__(self, root: Path | None = None):
        self.root = root or base_dir()
        self.data_dir = self.root / "data"
        self.db_path = self.data_dir / "chatgrab.db"
        self.photos_dir = self.data_dir / "photos"
        self.videos_dir = self.data_dir / "videos"
        self.voice_dir = self.data_dir / "voice"
        self.documents_dir = self.data_dir / "documents"
        self.exports_dir = self.data_dir / "exports"
        self.session_dir = self.data_dir / "session"
        self.session_path = self.session_dir / "worker.session"
        self.backups_dir = self.data_dir / "backups"
        self.archives_dir = self.data_dir / "archives"
        self.config_path = self.root / "config.json"
        self.log_path = self.data_dir / "chatgrab.log"

    def ensure(self) -> None:
        for d in (self.data_dir, self.photos_dir, self.videos_dir, self.voice_dir,
                  self.documents_dir, self.exports_dir, self.session_dir,
                  self.backups_dir, self.archives_dir):
            d.mkdir(parents=True, exist_ok=True)

    def photo_path(self, chat_id: int, message_id: int) -> Path:
        return self.photos_dir / str(chat_id) / f"{message_id}.jpg"

    def video_path(self, chat_id: int, message_id: int, ext: str = ".mp4") -> Path:
        return self.videos_dir / str(chat_id) / f"{message_id}{ext}"

    def voice_path(self, chat_id: int, message_id: int, ext: str = ".ogg") -> Path:
        return self.voice_dir / str(chat_id) / f"{message_id}{ext}"

    def document_path(self, chat_id: int, message_id: int, filename: str) -> Path:
        return self.documents_dir / str(chat_id) / f"{message_id}_{filename}"


PATHS = Paths()
