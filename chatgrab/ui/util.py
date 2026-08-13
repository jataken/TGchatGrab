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


def run_blocking(func: Callable, *args, **kwargs):
    """Await-able wrapper for a synchronous, potentially slow call (writing
    a large .xlsx export, etc.) on the event loop's default thread-pool
    executor. qasync runs one shared loop for the whole app — Qt, Telethon,
    and aiogram bot polling alike — so a call like this left running
    directly on that loop would freeze the entire UI, and every bot's
    message handling, until it finished. Database is explicitly safe to
    call from a worker thread this way (see db/database.py)."""
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(None, lambda: func(*args, **kwargs))
