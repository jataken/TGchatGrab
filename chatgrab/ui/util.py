"""Bridges Qt slots (sync) to Telethon/collector coroutines (async), running
on the same qasync-driven event loop as the GUI."""
from __future__ import annotations

import asyncio
from typing import Callable, Coroutine

from PySide6.QtWidgets import QMessageBox, QWidget

from ..telegram.errors import humanize_error


def fire(coro: Coroutine, parent: QWidget | None = None,
         on_error: Callable[[Exception], None] | None = None,
         on_done: Callable[[], None] | None = None) -> asyncio.Task:
    task = asyncio.ensure_future(coro)

    def _finished(t: asyncio.Task) -> None:
        try:
            t.result()
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001 — surfaced to the user, not swallowed
            if on_error:
                on_error(e)
            else:
                QMessageBox.warning(parent, "Не получилось", humanize_error(e))
            return
        if on_done:
            on_done()

    task.add_done_callback(_finished)
    return task
