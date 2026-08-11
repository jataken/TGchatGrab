from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QMainWindow, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from .. import APP_TITLE
from .context import AppContext
from .util import fire
from .screens.browse import BrowseScreen
from .screens.chats import ChatsScreen
from .screens.collect import CollectScreen
from .screens.connect import ConnectScreen
from .screens.export_screen import ExportScreen
from .screens.overview import OverviewScreen
from .screens.settings import SettingsScreen

NAV_ITEMS = [
    ("overview", "Обзор"),
    ("connect", "Подключение"),
    ("chats", "Чаты"),
    ("collect", "Сбор данных"),
    ("browse", "Поиск в собранном"),
    ("export", "Экспорт"),
    ("settings", "Настройки"),
]


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.setWindowTitle(APP_TITLE)
        self.resize(1320, 860)
        self.setMinimumSize(980, 620)

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.sidebar = QWidget()
        self.sidebar.setProperty("class", "sidebar")
        self.sidebar.setFixedWidth(232)
        side_lay = QVBoxLayout(self.sidebar)
        side_lay.setContentsMargins(12, 16, 12, 14)
        side_lay.setSpacing(2)

        section_label = QLabel("РАЗДЕЛЫ")
        section_label.setProperty("class", "kicker")
        side_lay.addWidget(section_label)

        self._nav_buttons: dict[str, QPushButton] = {}
        for key, title in NAV_ITEMS:
            btn = QPushButton(title)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(self._nav_button_qss())
            btn.clicked.connect(lambda checked, k=key: self.navigate(k))
            side_lay.addWidget(btn)
            self._nav_buttons[key] = btn

        side_lay.addStretch(1)

        self.queue_box = QLabel("")
        self.queue_box.setWordWrap(True)
        self.queue_box.setStyleSheet(
            "background: rgba(233,233,237,10); border-radius: 8px; padding: 10px; "
            "font-size: 12px; color: #c9c9d1;"
        )
        side_lay.addWidget(self.queue_box)

        self.conn_label = QLabel("● Нет подключения")
        self.conn_label.setStyleSheet("font-size: 12px; color: #9a9aa3; padding: 4px;")
        side_lay.addWidget(self.conn_label)

        outer.addWidget(self.sidebar)

        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)

        self.screens: dict[str, QWidget] = {
            "overview": OverviewScreen(ctx, self.navigate),
            "connect": ConnectScreen(ctx, self.navigate),
            "chats": ChatsScreen(ctx, self.navigate),
            "collect": CollectScreen(ctx, self.navigate),
            "browse": BrowseScreen(ctx, self.navigate),
            "export": ExportScreen(ctx, self.navigate),
            "settings": SettingsScreen(ctx, self.navigate),
        }
        for key, _ in NAV_ITEMS:
            self.stack.addWidget(self.screens[key])

        ctx.collector.chats_changed.connect(self._on_state_changed)
        ctx.collector.log_event.connect(self._on_log_event)

        self._sidebar_timer = QTimer(self)
        self._sidebar_timer.timeout.connect(self._refresh_sidebar)
        self._sidebar_timer.start(2000)

        self.navigate("overview")
        self._refresh_sidebar()
        fire(self._startup_autoconnect(), parent=self, on_error=lambda e: None)

    async def _startup_autoconnect(self) -> None:
        """Resume an already-authorized session automatically on launch —
        without this, the account only (re)connected once the user
        happened to open the Подключение screen, so a restart looked like
        it had forgotten the login even with a valid saved session."""
        if not self.ctx.config.is_configured:
            return
        connect_screen = self.screens["connect"]
        await connect_screen._check_auth()
        self._refresh_sidebar()

    @staticmethod
    def _nav_button_qss() -> str:
        from . import theme
        return f"""
        QPushButton {{
            text-align: left; padding: 8px 9px; border-radius: 8px; font-size: 13.5px;
            color: rgba(233,233,237,0.72); background: transparent; border: none;
        }}
        QPushButton:hover {{ background: rgba(233,233,237,15); }}
        QPushButton:checked {{
            color: {theme.ACCENT_400}; background: rgba(145,132,217,40);
        }}
        """

    def navigate(self, key: str, **kwargs) -> None:
        for k, btn in self._nav_buttons.items():
            btn.setChecked(k == key)
        widget = self.screens[key]
        self.stack.setCurrentWidget(widget)
        on_show = getattr(widget, "on_show", None)
        if on_show:
            on_show(**kwargs)

    def _on_state_changed(self) -> None:
        self._refresh_sidebar()

    def _on_log_event(self, entry: dict) -> None:
        self._refresh_sidebar()

    def _refresh_sidebar(self) -> None:
        db = self.ctx.db
        chats = db.list_chats()
        loading = [c for c in chats if c["status"] == "loading"]
        queued = [c for c in chats if c["status"] == "queued"]
        if loading:
            self.queue_box.setText(
                f"Сейчас грузится история «{loading[0]['title']}», в очереди ещё {len(queued)}."
            )
        elif queued:
            self.queue_box.setText(f"В очереди {len(queued)} чат(ов) на загрузку истории.")
        else:
            self.queue_box.setText("История загружена, очередь пуста.")

        if self.ctx.tg.authorized:
            self.conn_label.setText("●  Аккаунт подключён")
            self.conn_label.setStyleSheet("font-size: 12px; color: #7fc79b; padding: 4px;")
        else:
            self.conn_label.setText("●  Нет подключения")
            self.conn_label.setStyleSheet("font-size: 12px; color: #c98a9a; padding: 4px;")

    def closeEvent(self, event) -> None:  # noqa: N802
        event.accept()
