"""Turn unhandled exceptions into a visible message instead of a silent
crash or freeze, and keep a record of what happened.

A windowed (console-less) build has no terminal to show a traceback in —
whatever goes wrong there is otherwise invisible to the user and to us.
This installs three safety nets: file logging, a sys.excepthook for
exceptions raised synchronously (e.g. inside a Qt slot), and an asyncio
exception handler for exceptions raised in background tasks that nothing
is directly awaiting (Telethon's own connection/update loops included).
"""
from __future__ import annotations

import asyncio
import logging
import sys
import traceback
from logging.handlers import RotatingFileHandler

from . import diagnostics
from .paths import Paths

_logger = logging.getLogger("chatgrab")


LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 3


def install(paths: Paths) -> None:
    paths.ensure()
    # Rotating, not a plain FileHandler: this file is appended to for the
    # life of the installation, and an app that runs for days at a time
    # would otherwise grow it without limit.
    handler = RotatingFileHandler(
        str(paths.log_path), maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    # Idempotent: a second install() (tests, a re-entered bootstrap) must
    # not stack handlers and write every line several times over.
    for existing in list(root.handlers):
        if isinstance(existing, RotatingFileHandler):
            root.removeHandler(existing)
            existing.close()
    root.addHandler(handler)
    sys.excepthook = _make_excepthook(paths)


def install_loop_handler(loop: asyncio.AbstractEventLoop, paths: Paths) -> None:
    loop.set_exception_handler(_make_loop_handler(paths))


def _show_dialog(title: str, text: str) -> None:
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        if QApplication.instance() is None:
            return
        box = QMessageBox()
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle(title)
        box.setText(text)
        box.exec()
    except Exception:
        pass  # the log file already has the details; never let the
              # error-reporting path itself take the app down


def _make_excepthook(paths: Paths):
    def excepthook(exc_type, exc, tb):
        _logger.error("Необработанная ошибка", exc_info=(exc_type, exc, tb))
        if isinstance(exc, BaseException):
            diagnostics.failure("необработанное исключение", exc)
        from .telegram.errors import humanize_error
        message = humanize_error(exc) if isinstance(exc, Exception) else str(exc)
        _show_dialog(
            "Непредвиденная ошибка",
            f"Что-то пошло не так:\n\n{message}\n\n"
            f"Подробности записаны в файл: {paths.log_path}\n"
            "Приложение постарается продолжить работу.",
        )
    return excepthook


def _make_loop_handler(paths: Paths):
    def handler(loop, context):
        exc = context.get("exception")
        message = context.get("message", "")
        if exc is not None:
            _logger.error(
                "Необработанная ошибка в фоновой задаче: %s\n%s",
                message, "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            )
            diagnostics.failure(f"фоновая задача ({message})", exc)
            from .telegram.errors import humanize_error
            _show_dialog("Что-то пошло не так", humanize_error(exc))
        else:
            _logger.error("Ошибка цикла событий: %s (%s)", message, context)
    return handler
