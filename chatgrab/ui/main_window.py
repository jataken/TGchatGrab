from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QHBoxLayout, QLabel, QMainWindow, QPushButton,
    QStackedWidget, QVBoxLayout, QWidget,
)

from .. import APP_TITLE
from .. import diagnostics
from ..paths import resource_path
from .context import AppContext
from .util import fire
from .screens.bots import BotsScreen
from .screens.bots.funnels_screen import FunnelsScreen
from .screens.bots.leads_screen import LeadsScreen
from .screens.bots.log_screen import BotLogScreen
from .screens.bots.rules_screen import RulesScreen
from .screens.bots.scenario_screen import ScenarioScreen
from .screens.bots.templates_screen import TemplatesScreen
from .screens.bitrix_screen import BitrixScreen
from .screens.browse import BrowseScreen
from .screens.watch import WatchScreen
from .screens.chats import ChatsScreen
from .screens.collect import CollectScreen
from .screens.connect import ConnectScreen
from .screens.directions import DirectionsScreen
from .screens.export_screen import ExportScreen
from .screens.mail import MailScreen
from .screens.mail.attachments_screen import MailAttachmentsScreen
from .screens.mail.contacts_screen import MailContactsScreen
from .screens.mail.filters_screen import MailFiltersScreen
from .screens.mail.leads_screen import MailLeadsScreen
from .screens.mail.reports_screen import MailReportsScreen
from .screens.mail_settings import MailSettingsScreen
from .screens.mail_triage import MailTriageScreen
from .screens.reports_screen import ReportsScreen
from .screens.settings import SettingsScreen
from .screens.today import TodayScreen
from .tray import TrayController
from .widget_window import WidgetWindow

# The app is three functional blocks — collecting from Telegram chats,
# running bots on top of the same account, and the leads those two feed —
# switched with a segmented control at the top of the sidebar. Each block
# gets its own short nav list below it; "Подключение" and "Настройки" are
# shared account-level settings and stay pinned under all of them, so they
# don't need duplicating.
#
# «Лиды» used to be a screen inside «Боты» ("Заявки"). С3 makes leads a
# top-level block of their own — a bot is now one of three ways a lead
# gets created (manual, message-based, and scenario/rule-based being the
# others), so nesting it under «Боты» stopped matching what it actually is.
BLOCKS = [("collect", "Сбор"), ("bots", "Боты"), ("leads", "Лиды"), ("mail", "Почта")]

NAV_BY_BLOCK: dict[str, list[tuple[str, str]]] = {
    "collect": [
        ("today", "Сегодня"),
        ("chats", "Источники"),
        ("collect", "Сбор"),
        ("browse", "Собранное"),
        ("watch", "Наблюдение"),
        ("export", "Экспорт"),
    ],
    "bots": [
        ("bots", "Боты"),
        ("rules", "Правила"),
        ("scenario", "Сценарий"),
        ("templates", "Шаблоны"),
        ("botlog", "Журнал"),
    ],
    "leads": [
        ("leads", "Заявки"),
        ("funnels", "Воронки"),
        ("reports", "Отчёты"),
    ],
    # П2: самостоятельный блок теперь, когда есть что показывать помимо
    # списка ящиков — входящие с цепочками, поиском и пометкой «прочитано».
    "mail": [
        ("mail", "Входящие"),
        ("mail_triage", "Разбор"),
        ("mail_leads", "Заявки"),
        ("mail_filters", "Фильтры"),
        ("mail_contacts", "Адресная книга"),
        ("mail_attachments", "Вложения"),
        ("mail_reports", "Отчёты"),
        ("mail_settings", "Ящики"),
    ],
}

# Screens that edit one particular bot, and so follow the sidebar's bot
# selector. «Боты» lists them all and «Журнал» spans them, so those two
# are deliberately not here.
BOT_SCOPED_SCREENS = {"rules", "scenario", "templates"}
COMMON_ITEMS = [
    ("connect", "Подключение"),
    ("directions", "Направления"),
    ("bitrix", "Bitrix24"),
    ("settings", "Настройки"),
]

SCREEN_BLOCK = {key: block for block, items in NAV_BY_BLOCK.items() for key, _ in items}


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.setWindowTitle(APP_TITLE)
        icon_path = resource_path("resources", "icon.png")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
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
        self.sidebar.setFixedWidth(238)
        side_lay = QVBoxLayout(self.sidebar)
        side_lay.setContentsMargins(12, 14, 12, 14)
        side_lay.setSpacing(2)

        self.active_block = "collect"
        self._nav_buttons: dict[str, QPushButton] = {}
        self._nav_labels: dict[str, str] = {}

        switcher = QWidget()
        # Scoped by object name. A selector-less stylesheet on a container
        # is inherited by every child, so the plain "background: …" this
        # used to carry was repainting the two tab buttons inside it — the
        # checked tab's accent fill was being overwritten by the track's
        # own colour, leaving only its border visible.
        switcher.setObjectName("blockSwitcher")
        switcher.setStyleSheet(
            "QWidget#blockSwitcher { background: rgba(233,233,237,13); border-radius: 9px; }"
        )
        switcher_lay = QHBoxLayout(switcher)
        switcher_lay.setContentsMargins(3, 3, 3, 3)
        switcher_lay.setSpacing(3)
        self.block_group = QButtonGroup(self)
        self.block_group.setExclusive(True)
        self.block_buttons: dict[str, QPushButton] = {}
        for key, title in BLOCKS:
            btn = QPushButton(title)
            btn.setProperty("class", "blocktab")
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _checked, k=key: self._switch_block(k))
            self.block_group.addButton(btn)
            switcher_lay.addWidget(btn, 1)
            self.block_buttons[key] = btn
        side_lay.addWidget(switcher)
        side_lay.addSpacing(11)

        # The bot the block's editing screens act on — chosen once here
        # instead of once per screen. See ui/bot_selection.py.
        self.bot_selector_box = QWidget()
        selector_lay = QVBoxLayout(self.bot_selector_box)
        selector_lay.setContentsMargins(0, 0, 0, 10)
        selector_lay.setSpacing(4)
        selector_lay.addWidget(QLabel("НАСТРАИВАЕМ БОТА"), 0)
        self.bot_selector_box.findChild(QLabel).setProperty("class", "kicker")
        self.bot_selector = QComboBox()
        self.bot_selector.currentIndexChanged.connect(self._on_bot_selector_changed)
        selector_lay.addWidget(self.bot_selector)
        side_lay.addWidget(self.bot_selector_box)

        self.nav_panels: dict[str, QWidget] = {}
        for block_key, _ in BLOCKS:
            panel = QWidget()
            panel_lay = QVBoxLayout(panel)
            panel_lay.setContentsMargins(0, 0, 0, 0)
            panel_lay.setSpacing(2)
            for key, title in NAV_BY_BLOCK[block_key]:
                self._add_nav_button(panel_lay, key, title)
            side_lay.addWidget(panel)
            self.nav_panels[block_key] = panel

        side_lay.addStretch(1)

        divider = QWidget()
        divider.setFixedHeight(1)
        divider.setStyleSheet("background: #33354a;")
        side_lay.addSpacing(8)
        side_lay.addWidget(divider)
        side_lay.addSpacing(8)

        for key, title in COMMON_ITEMS:
            self._add_nav_button(side_lay, key, title)

        self.queue_box = QLabel("")
        self.queue_box.setWordWrap(True)
        self.queue_box.setStyleSheet(
            "background: rgba(233,233,237,10); border-radius: 8px; padding: 10px; "
            "font-size: 12px; color: #c9c9d1; margin-top: 10px;"
        )
        side_lay.addWidget(self.queue_box)

        self.conn_label = QLabel("● Нет подключения")
        self.conn_label.setStyleSheet("font-size: 12px; color: #9a9aa3; padding: 4px;")
        side_lay.addWidget(self.conn_label)

        outer.addWidget(self.sidebar)

        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)

        self.screens: dict[str, QWidget] = {
            "today": TodayScreen(ctx, self.navigate),
            "connect": ConnectScreen(ctx, self.navigate),
            "directions": DirectionsScreen(ctx, self.navigate),
            "chats": ChatsScreen(ctx, self.navigate),
            "collect": CollectScreen(ctx, self.navigate),
            "browse": BrowseScreen(ctx, self.navigate),
            "watch": WatchScreen(ctx, self.navigate),
            "export": ExportScreen(ctx, self.navigate),
            "bots": BotsScreen(ctx, self.navigate),
            "leads": LeadsScreen(ctx, self.navigate),
            "funnels": FunnelsScreen(ctx, self.navigate),
            "reports": ReportsScreen(ctx, self.navigate),
            "rules": RulesScreen(ctx, self.navigate),
            "scenario": ScenarioScreen(ctx, self.navigate),
            "templates": TemplatesScreen(ctx, self.navigate),
            "botlog": BotLogScreen(ctx, self.navigate),
            "bitrix": BitrixScreen(ctx, self.navigate),
            "mail": MailScreen(ctx, self.navigate),
            "mail_triage": MailTriageScreen(ctx, self.navigate),
            "mail_leads": MailLeadsScreen(ctx, self.navigate),
            "mail_filters": MailFiltersScreen(ctx, self.navigate),
            "mail_contacts": MailContactsScreen(ctx, self.navigate),
            "mail_attachments": MailAttachmentsScreen(ctx, self.navigate),
            "mail_reports": MailReportsScreen(ctx, self.navigate),
            "mail_settings": MailSettingsScreen(ctx, self.navigate),
            "settings": SettingsScreen(ctx, self.navigate),
        }
        for key in self.screens:
            self.stack.addWidget(self.screens[key])

        ctx.collector.chats_changed.connect(self._on_state_changed)
        ctx.collector.log_event.connect(self._on_log_event)
        ctx.bot_manager.bots_changed.connect(self._on_state_changed)
        ctx.bot_manager.log_event.connect(self._on_log_event)

        self._sidebar_timer = QTimer(self)
        self._sidebar_timer.timeout.connect(self._refresh_sidebar)
        self._sidebar_timer.start(2000)

        # П8: a separate top-level window, not part of the stack above —
        # constructed before the tray so its show_and_raise can be handed
        # to TrayController as the "Показать виджет" menu action.
        self.widget_window = WidgetWindow(ctx, on_open_thread=self._on_widget_open_thread)

        # Set before the tray exists: quit_app flips it so closeEvent knows
        # the close is a real exit rather than a minimise-to-tray.
        self.allow_close = False
        self.tray = TrayController(self, ctx, on_show_widget=self.widget_window.show_and_raise)
        # A watch-list match is the one collection event worth interrupting
        # for — it is what the user explicitly asked to be told about.
        ctx.watch_service.on_hit = self._on_watch_hit
        ctx.lead_reminder_service.on_fire = self._on_lead_reminder
        ctx.mail_service.on_triage_hit = self._on_triage_hit

        self._switch_block("collect", navigate_to="today")
        self._refresh_sidebar()
        fire(self._startup_autoconnect(), parent=self, on_error=lambda e: None)

    def _add_nav_button(self, layout: QVBoxLayout, key: str, title: str) -> None:
        btn = QPushButton(title)
        btn.setCheckable(True)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(self._nav_button_qss())
        btn.clicked.connect(lambda checked, k=key: self.navigate(k))
        layout.addWidget(btn)
        self._nav_buttons[key] = btn
        self._nav_labels[key] = title

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
            text-align: left; padding: 8px 10px; border-radius: 8px; font-size: 13.5px;
            color: rgba(233,233,237,0.72); background: transparent; border: none;
        }}
        QPushButton:hover {{ background: rgba(233,233,237,15); }}
        QPushButton:checked {{
            color: {theme.ACCENT_400}; background: rgba(145,132,217,40);
        }}
        """

    def _switch_block(self, block_key: str, navigate_to: str | None = None) -> None:
        self.active_block = block_key
        self.block_buttons[block_key].setChecked(True)
        for key, panel in self.nav_panels.items():
            panel.setVisible(key == block_key)
        target = navigate_to or NAV_BY_BLOCK[block_key][0][0]
        self.navigate(target)

    def _refresh_bot_selector(self) -> None:
        """Keep the sidebar picker in step with the bots that exist, and
        show it only where it means something."""
        bots = self.ctx.db.list_bots()
        current = self.ctx.bot_selection.ensure_valid(bots)
        self.bot_selector.blockSignals(True)
        self.bot_selector.clear()
        for bot in bots:
            self.bot_selector.addItem(bot["name"], bot["id"])
        idx = self.bot_selector.findData(current)
        self.bot_selector.setCurrentIndex(idx if idx >= 0 else -1)
        self.bot_selector.blockSignals(False)

        current_screen = next(
            (k for k, w in self.screens.items() if w is self.stack.currentWidget()), None
        )
        self.bot_selector_box.setVisible(
            bool(bots) and current_screen in BOT_SCOPED_SCREENS
        )

    def _on_watch_hit(self, rule, record) -> None:
        chat = self.ctx.db.get_chat(record.get("chat_id"))
        where = chat["title"] if chat else "отслеживаемый чат"
        text = (record.get("text") or record.get("media_caption") or "").strip()
        self.tray.notify(f"Сработало слово «{rule['phrase']}»",
                          f"{where}: {text[:160]}")
        self._refresh_sidebar()

    def _on_lead_reminder(self, lead) -> None:
        handle = lead["display_name"] or (f"@{lead['username']}" if lead["username"] else None) \
            or "заявка"
        text = lead["next_action_text"] or "пора вернуться к этому лиду"
        self.tray.notify(f"Напоминание: {handle}", text)
        self._refresh_sidebar()

    def _on_triage_hit(self, message, score, category, reasons) -> None:
        who = message["sender_name"] or message["sender_address"] or "—"
        mailbox = self.ctx.db.get_mailbox(message["mailbox_id"])
        subject = message["subject"] or "(без темы)"
        mailbox_id, thread_id = message["mailbox_id"], message["thread_id"]
        self.tray.notify(
            f"Похоже на {category}: {subject}",
            f"{who} · {mailbox['address'] if mailbox else '?'} · балл {score}",
            on_click=lambda: self.navigate("mail", mailbox_id=mailbox_id, thread_id=thread_id))

    def _on_widget_open_thread(self, mailbox_id: int, thread_id: int) -> None:
        # П8: «клик по письму открывает главное окно на этой цепочке» —
        # same show/raise/activate the tray icon uses, then the same
        # deep-link navigate() П7's tray-notification click already goes
        # through, not a second, parallel path to the same destination.
        self.tray.show_window()
        self.navigate("mail", mailbox_id=mailbox_id, thread_id=thread_id)

    def _on_bot_selector_changed(self, _index: int) -> None:
        self.ctx.bot_selection.set_current(self.bot_selector.currentData())

    def navigate(self, key: str, **kwargs) -> None:
        block = SCREEN_BLOCK.get(key)
        if block is not None and block != self.active_block:
            self.active_block = block
            self.block_buttons[block].setChecked(True)
            for bk, panel in self.nav_panels.items():
                panel.setVisible(bk == block)
        for k, btn in self._nav_buttons.items():
            btn.setChecked(k == key)
        widget = self.screens[key]
        diagnostics.screen(self._nav_labels.get(key, key))
        self.stack.setCurrentWidget(widget)
        # Before on_show, so a bot-scoped screen reads a selection that is
        # already pointing at a bot that exists.
        self._refresh_bot_selector()
        on_show = getattr(widget, "on_show", None)
        if on_show:
            on_show(**kwargs)

    def _on_state_changed(self) -> None:
        self._refresh_sidebar()

    def _on_log_event(self, entry: dict) -> None:
        # Into the trace as well: these lines are visible on screen only
        # when the matching log panel happens to be open, so a warning
        # during manual testing is otherwise easy to miss entirely.
        session = diagnostics.current()
        if session is not None:
            session.app_event("бот" if "bot" in entry else "сбор", entry)
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

        self.tray.refresh()

        bots = db.list_bots()
        new_leads = len(db.list_leads(status="new"))
        bad_bots = len([b for b in bots if b["status"] == "error"])

        self._set_badge("chats", str(len(chats)) if chats else "")
        self._set_badge("collect", "●" if loading else "", accent=True)
        self._set_badge("bots", str(len(bots)) if bots else "")
        unseen = db.unseen_watch_count()
        self._set_badge("watch", str(unseen) if unseen else "")
        self._set_badge("leads", str(new_leads) if new_leads else "")
        self._set_badge(
            "collect_block", "●" if loading else "", accent=True, button=self.block_buttons["collect"],
            base_label="Сбор",
        )
        self._set_badge(
            "bots_block", "!" if bad_bots else "", accent=False, button=self.block_buttons["bots"],
            base_label="Боты",
        )
        self._set_badge(
            "leads_block", str(new_leads) if new_leads else "", accent=True,
            button=self.block_buttons["leads"], base_label="Лиды",
        )

    def _set_badge(self, key: str, badge: str, accent: bool = False,
                    button: QPushButton | None = None, base_label: str | None = None) -> None:
        btn = button or self._nav_buttons.get(key)
        if btn is None:
            return
        label = base_label if base_label is not None else self._nav_labels.get(key, "")
        btn.setText(f"{label}   {badge}" if badge else label)

    def closeEvent(self, event) -> None:  # noqa: N802
        """Closing the window hides it to the tray instead of quitting —
        the app's whole job is to keep listening. Exiting for real happens
        from the tray menu, and the first time we hide we say so, since a
        window that vanishes without explanation reads as a crash."""
        minimize = self.ctx.db.get_setting("tray_minimize_on_close", True)
        if self.allow_close or self.tray.tray is None or not minimize:
            event.accept()
            return
        event.ignore()
        self.hide()
        if not self.ctx.db.get_setting("tray_hint_shown", False):
            self.ctx.db.set_setting("tray_hint_shown", True)
            self.tray.notify(
                "ChatGrab продолжает работать",
                "Окно свёрнуто в область уведомлений — сбор сообщений идёт. "
                "Чтобы выйти совсем, нажмите «Выйти» в меню значка."
            )
