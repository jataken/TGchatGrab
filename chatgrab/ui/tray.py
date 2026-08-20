"""System-tray presence, desktop notifications, and Windows autostart.

The app is a background job by nature: it listens to chats for days at a
time. Until now that meant leaving a window open and finding out about a
lost login, an unreachable chat or a stopped bot only by opening the log.

Autostart is written to the per-user Run key, which needs no elevation and
affects only this account. Everything here degrades to a no-op off
Windows and where no tray is available, so the app still runs on a desktop
without a notification area.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

_logger = logging.getLogger("chatgrab")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_RUN_NAME = "ChatGrab"


# ---- Windows autostart --------------------------------------------------
def autostart_supported() -> bool:
    return sys.platform == "win32"


def _command() -> str:
    """What Windows should run at logon. A frozen build is the exe itself;
    running from source needs the interpreter and the entry script, and
    both paths are quoted since they routinely contain spaces."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    entry = Path(sys.argv[0]).resolve()
    return f'"{sys.executable}" "{entry}"'


def autostart_enabled() -> bool:
    if not autostart_supported():
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_RUN_NAME)
            return True
    except OSError:
        return False


def set_autostart(enabled: bool) -> bool:
    """Returns the state actually achieved, so the UI can reflect reality
    rather than the request if the registry write is refused.

    CreateKeyEx, а не OpenKey: ...\\CurrentVersion\\Run существует не в
    каждом профиле Windows — в свежем, где автозапуск ещё никто не
    настраивал, его просто нет, и OpenKey падает с «файл не найден».
    Ошибка при этом выглядела как отказ системы («Windows отклонил запись
    в реестр»), хотя отклонять было нечего. CreateKeyEx открывает
    существующий ключ и заводит отсутствующий.
    """
    if not autostart_supported():
        return False
    import winreg
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, APP_RUN_NAME, 0, winreg.REG_SZ, _command())
            else:
                try:
                    winreg.DeleteValue(key, APP_RUN_NAME)
                except FileNotFoundError:
                    pass
        return enabled
    except OSError:
        _logger.warning("не удалось изменить автозапуск", exc_info=True)
        return autostart_enabled()


# ---- tray ---------------------------------------------------------------
class TrayController:
    """Owns the tray icon and turns app events into notifications.

    Notifications are deliberately narrow: the things a user would want to
    be interrupted for — the account got logged out, a chat became
    unreachable, a bot died. Ordinary collection progress stays in the log,
    or the tray would cry wolf all day and get muted."""

    def __init__(self, window, ctx, on_show_widget=None):
        self.window = window
        self.ctx = ctx
        self.on_show_widget = on_show_widget
        self.tray: QSystemTrayIcon | None = None
        self._last_conn_ok: bool | None = None
        self._notified_chat_errors: set[int] = set()
        self._notified_bot_errors: set[int] = set()
        # П7: "клик [по уведомлению] открывает цепочку" — QSystemTrayIcon
        # only ever has one balloon showing and one messageClicked signal
        # with no per-message identity, so "which message" is just
        # whatever notify() was last called with a click handler for.
        self._last_message_click = None

        if not QSystemTrayIcon.isSystemTrayAvailable():
            _logger.info("системный трей недоступен — работаем без него")
            return

        icon = window.windowIcon()
        if icon.isNull():
            icon = QApplication.style().standardIcon(QApplication.style().SP_ComputerIcon)
        self.tray = QSystemTrayIcon(icon, window)
        self.tray.setToolTip("ChatGrab")

        menu = QMenu()
        self.open_action = QAction("Открыть ChatGrab", menu)
        self.open_action.triggered.connect(self.show_window)
        menu.addAction(self.open_action)
        if self.on_show_widget:
            widget_action = QAction("Показать виджет", menu)
            widget_action.triggered.connect(self.on_show_widget)
            menu.addAction(widget_action)
        menu.addSeparator()
        self.status_action = QAction("", menu)
        self.status_action.setEnabled(False)
        menu.addAction(self.status_action)
        menu.addSeparator()
        quit_action = QAction("Выйти", menu)
        quit_action.triggered.connect(self.quit_app)
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_activated)
        self.tray.messageClicked.connect(self._on_message_clicked)
        self.tray.show()

    def _on_message_clicked(self) -> None:
        handler, self._last_message_click = self._last_message_click, None
        if handler is not None:
            handler()

    # ---- window plumbing ------------------------------------------------
    def _on_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_window()

    def show_window(self) -> None:
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()

    def quit_app(self) -> None:
        self.window.allow_close = True
        self.window.close()
        QApplication.quit()

    def notify(self, title: str, text: str, warning: bool = False, on_click=None) -> None:
        if self.tray is None:
            return
        icon = QSystemTrayIcon.Warning if warning else QSystemTrayIcon.Information
        self._last_message_click = on_click
        self.tray.showMessage(title, text, icon, 8000)

    # ---- state -> tooltip and notifications -----------------------------
    def refresh(self) -> None:
        if self.tray is None:
            return
        db = self.ctx.db
        chats = db.list_chats()
        listening = [c for c in chats if c["enabled"]]
        loading = next((c for c in chats if c["status"] == "loading"), None)
        bots_running = [b for b in db.list_bots() if b["status"] == "running"]

        line = f"Слушаем {len(listening)} из {len(chats)} чатов"
        if loading:
            line += f"; грузится «{loading['title']}»"
        if bots_running:
            line += f"; ботов работает {len(bots_running)}"
        self.tray.setToolTip(f"ChatGrab — {line}")
        self.status_action.setText(line)

        connected = self.ctx.tg.authorized
        if self._last_conn_ok is None:
            self._last_conn_ok = connected
        elif connected != self._last_conn_ok:
            self._last_conn_ok = connected
            if connected:
                self.notify("ChatGrab", "Соединение с Telegram восстановлено.")
            else:
                self.notify("ChatGrab", "Аккаунт отключился от Telegram — "
                                          "сбор новых сообщений остановлен.", warning=True)

        # One notification per problem, not one per refresh tick; clearing
        # the problem re-arms it.
        for chat in chats:
            cid = chat["chat_id"]
            if chat["last_error"]:
                if cid not in self._notified_chat_errors:
                    self._notified_chat_errors.add(cid)
                    self.notify("Чат недоступен",
                                f"«{chat['title']}»: {chat['last_error']}", warning=True)
            else:
                self._notified_chat_errors.discard(cid)

        for bot in db.list_bots():
            bid = bot["id"]
            if bot["status"] == "error":
                if bid not in self._notified_bot_errors:
                    self._notified_bot_errors.add(bid)
                    self.notify("Бот остановился",
                                f"«{bot['name']}»: {bot['last_error'] or 'причина не указана'}",
                                warning=True)
            else:
                self._notified_bot_errors.discard(bid)
