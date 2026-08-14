"""Session trace for hands-on testing — TEMPORARY.

Purpose: when the app is driven by a real person against a real Telegram
account, produce one file that says what they did and what the app did
about it, including the failures that never reach the screen.

This exists to be read once and thrown away. It is not part of what the
app is for, and the whole feature is meant to be deleted (or left
permanently off) once manual testing is done — see TEMPORARY.md. It is
kept deliberately self-contained so removing it is one file plus a
handful of call sites.

What it records:

- every screen the user opens, in order;
- every button they press, with its label and the screen it was on —
  captured through one application-level event filter rather than by
  touching every widget;
- everything the app logs, down to DEBUG, including the warnings that are
  currently swallowed on purpose (a failed send, a rule that matched
  nothing, a chat that went unreachable) — these are exactly the "breaks
  but you cannot see it" cases;
- collector and bot log events, which the user sees only if they happen
  to have the right screen open;
- unhandled exceptions and failed background tasks, with tracebacks.

What it must never do: interfere. Every write is guarded, and a failure
to record is silently ignored — a diagnostic tool that can crash the
program it is diagnosing is worse than no diagnostic tool.

Privacy: message text is not written here. Chat titles, screen names and
button labels are, since without them the trace is unreadable. Telegram
credentials and bot tokens never pass through it.
"""
from __future__ import annotations

import datetime as dt
import logging
import platform
import sys
from pathlib import Path

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QAbstractButton, QApplication

from . import APP_TITLE, __version__
from .paths import Paths

_logger = logging.getLogger("chatgrab")
_trace = logging.getLogger("chatgrab.trace")

SETTING_KEY = "diagnostics_enabled"


class _TraceFormatter(logging.Formatter):
    """Two shapes in one file. Lines the trace writes itself are already
    labelled («ЭКРАН», «КЛИК»), so repeating the logger name on them is
    noise; lines from the rest of the app keep their level and origin,
    because that is what tells you which part misbehaved."""

    def format(self, record: logging.LogRecord) -> str:
        stamp = self.formatTime(record, self.datefmt)
        if record.name == "chatgrab.trace":
            body = record.getMessage()
        else:
            body = f"{record.levelname:<8} {record.name}: {record.getMessage()}"
        if record.exc_info:
            body += "\n" + self.formatException(record.exc_info)
        return f"{stamp}  {body}"


class _ButtonWatcher(QObject):
    """One event filter on the application, instead of a signal connection
    on every button in the app. Records presses on anything that behaves
    like a button, together with the screen it belongs to."""

    def __init__(self, session: "DiagnosticSession"):
        super().__init__()
        self.session = session

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt override)
        try:
            if event.type() == QEvent.MouseButtonRelease and isinstance(obj, QAbstractButton):
                label = obj.text().replace("\n", " ").strip() or obj.objectName() or type(obj).__name__
                state = ""
                if obj.isCheckable():
                    state = " [включено]" if obj.isChecked() else " [выключено]"
                self.session.event("клик", f"«{label}»{state}")
        except Exception:
            pass  # never let instrumentation break input handling
        return False


class DiagnosticSession:
    """Writes one file per app run: diagnostics/session-<timestamp>.log"""

    def __init__(self, paths: Paths):
        self.paths = paths
        self.dir = paths.data_dir / "diagnostics"
        self.path: Path | None = None
        self._handler: logging.Handler | None = None
        self._watcher: _ButtonWatcher | None = None
        self.active = False

    # ---- lifecycle ---------------------------------------------------
    def start(self) -> bool:
        if self.active:
            return True
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            self.path = self.dir / f"session-{stamp}.log"

            handler = logging.FileHandler(self.path, encoding="utf-8")
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(_TraceFormatter(datefmt="%H:%M:%S"))
            # Attach to the app's own logger tree and lower its level, so
            # the warnings that are normally filtered out (level WARNING on
            # the root file log) are captured here in full.
            _logger.addHandler(handler)
            _logger.setLevel(logging.DEBUG)
            self._handler = handler
            self.active = True

            self._write_header()

            app = QApplication.instance()
            if app is not None:
                self._watcher = _ButtonWatcher(self)
                app.installEventFilter(self._watcher)
            return True
        except Exception:
            _logger.warning("не удалось начать диагностическую запись", exc_info=True)
            self.active = False
            return False

    def stop(self) -> None:
        if not self.active:
            return
        try:
            self.event("сессия", "запись остановлена")
            app = QApplication.instance()
            if app is not None and self._watcher is not None:
                app.removeEventFilter(self._watcher)
            if self._handler is not None:
                _logger.removeHandler(self._handler)
                self._handler.close()
        except Exception:
            pass
        finally:
            self._watcher = None
            self._handler = None
            self.active = False
            _logger.setLevel(logging.WARNING)

    def _write_header(self) -> None:
        self.event("сессия", "запись начата")
        _trace.info(
            "  окружение: %s %s | Python %s | %s",
            APP_TITLE, __version__, platform.python_version(),
            f"{platform.system()} {platform.release()}",
        )
        _trace.info("  папка данных: %s", self.paths.data_dir)
        _trace.info("  замороженная сборка: %s", bool(getattr(sys, "frozen", False)))
        _trace.info("%s", "-" * 72)

    # ---- recording ---------------------------------------------------
    def event(self, kind: str, detail: str = "") -> None:
        """A user-visible action or app milestone."""
        if not self.active:
            return
        try:
            _trace.info("%-10s %s", kind.upper(), detail)
        except Exception:
            pass

    def screen(self, name: str) -> None:
        self.event("экран", f"открыт «{name}»")

    def app_event(self, source: str, entry: dict) -> None:
        """Collector/bot log lines — the ones a user only sees when the
        matching screen happens to be open."""
        if not self.active:
            return
        tone = entry.get("tone") or "info"
        who = entry.get("chat") or entry.get("bot") or ""
        self.event(source, f"[{tone}] {who} — {entry.get('text', '')}")

    def failure(self, where: str, exc: BaseException) -> None:
        if not self.active:
            return
        try:
            _trace.error("СБОЙ       %s: %s: %s", where, type(exc).__name__, exc, exc_info=exc)
        except Exception:
            pass


# A module-level handle so call sites don't have to thread it through every
# constructor. None until the app decides to enable tracing.
_session: DiagnosticSession | None = None


def install(paths: Paths, enabled: bool) -> DiagnosticSession | None:
    global _session
    if not enabled:
        _session = None
        return None
    session = DiagnosticSession(paths)
    if session.start():
        _session = session
        return session
    _session = None
    return None


def current() -> DiagnosticSession | None:
    return _session


def event(kind: str, detail: str = "") -> None:
    if _session is not None:
        _session.event(kind, detail)


def screen(name: str) -> None:
    if _session is not None:
        _session.screen(name)


def failure(where: str, exc: BaseException) -> None:
    if _session is not None:
        _session.failure(where, exc)
