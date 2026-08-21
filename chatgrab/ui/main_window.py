from __future__ import annotations

from PySide6.QtCore import QPropertyAnimation, Qt, QTimer, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QFrame, QGraphicsOpacityEffect, QHBoxLayout,
    QLabel, QMainWindow, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from .. import APP_TITLE
from .. import diagnostics
from ..paths import resource_path
from . import theme
from .context import AppContext
from .icons import nav_icon
from .util import fire
from .widgets import AnimatedProgressBar, PulseDot, label
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


class _NavItem(QFrame):
    """One sidebar row (Д3, design-brief.md §3.1): a 15×15 recolored SVG
    icon, 13px title, an optional right-aligned badge, and a 2px accent
    stripe at the left edge that fades in on selection (150мс) rather than
    snapping. Base/hover/checked backgrounds live in theme.py's shared
    "navitem"/"navtitle" QSS classes (one rule set for all ~26 rows, the
    same convention "card"/"chip"/"blocktab" already use) rather than a
    per-instance stylesheet; only the icon pixmap is Python-recolored,
    since a rendered SVG raster can't follow a QSS cascade.

    Exposes `isChecked`/`setChecked`/`clicked` in the same shape
    `MainWindow.navigate()` already used for the plain `QPushButton` this
    replaces, so that method's own logic doesn't have to change."""

    clicked = Signal(str)

    def __init__(self, key: str, title: str):
        super().__init__()
        self._key = key
        self._checked = False
        self.setProperty("class", "navitem")
        self.setProperty("navChecked", "false")
        self.setAttribute(Qt.WA_Hover, True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(31)

        self._stripe = QFrame(self)
        self._stripe.setStyleSheet(f"background: {theme.ACCENT_400}; border-radius: 1px;")
        self._stripe_effect = QGraphicsOpacityEffect(self._stripe)
        self._stripe.setGraphicsEffect(self._stripe_effect)
        self._stripe_effect.setOpacity(0.0)
        self._stripe_anim = QPropertyAnimation(self._stripe_effect, b"opacity", self)
        self._stripe_anim.setDuration(150)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 7, 8, 7)
        lay.setSpacing(9)
        self._icon_label = QLabel()
        self._icon_label.setFixedSize(15, 15)
        lay.addWidget(self._icon_label)
        self._title_label = label(title, "navtitle")
        lay.addWidget(self._title_label)
        lay.addStretch(1)
        self._badge_dot = PulseDot(theme.ACCENT_400, diameter=6, halo=False)
        self._badge_dot.hide()
        lay.addWidget(self._badge_dot)
        self._badge_label = label("", "navbadge")
        self._badge_label.hide()
        lay.addWidget(self._badge_label)

        self._update_icon()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._stripe.setGeometry(0, 8, 2, max(0, self.height() - 16))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        super().mousePressEvent(event)
        self.clicked.emit(self._key)

    def isChecked(self) -> bool:  # noqa: N802 (mirrors QAbstractButton, the widget this replaces)
        return self._checked

    def setChecked(self, checked: bool) -> None:  # noqa: N802
        if checked == self._checked:
            return
        self._checked = checked
        self.setProperty("navChecked", "true" if checked else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        self._stripe_anim.stop()
        self._stripe_anim.setStartValue(self._stripe_effect.opacity())
        self._stripe_anim.setEndValue(1.0 if checked else 0.0)
        self._stripe_anim.start()
        self._update_icon()

    def _update_icon(self) -> None:
        color = theme.ACCENT_300 if self._checked else theme.TEXT
        opacity = 1.0 if self._checked else 0.66
        icon = nav_icon(self._key, color, size=15, opacity=opacity)
        self._icon_label.setPixmap(icon.pixmap(15, 15) if icon else QPixmap())

    def set_badge_text(self, text: str, color: str) -> None:
        self._badge_dot.set_pulsing(False)
        self._badge_dot.hide()
        self._badge_label.setText(text)
        self._badge_label.setStyleSheet(f"color: {color};")
        self._badge_label.setVisible(bool(text))

    def set_badge_dot(self, color: str, pulsing: bool = False) -> None:
        self._badge_label.setVisible(False)
        self._badge_dot.set_color(color)
        self._badge_dot.set_pulsing(pulsing)
        self._badge_dot.show()

    def clear_badge(self) -> None:
        self._badge_label.setVisible(False)
        self._badge_dot.hide()
        self._badge_dot.set_pulsing(False)


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
        self.sidebar.setFixedWidth(232)
        side_lay = QVBoxLayout(self.sidebar)
        side_lay.setContentsMargins(10, 16, 10, 12)
        side_lay.setSpacing(2)

        self.active_block = "collect"
        self._nav_buttons: dict[str, _NavItem] = {}
        self._nav_labels: dict[str, str] = {}

        logo_row = QWidget()
        logo_lay = QHBoxLayout(logo_row)
        logo_lay.setContentsMargins(8, 0, 8, 14)
        logo_lay.setSpacing(8)
        logo_badge = QLabel("CG")
        logo_badge.setFixedSize(22, 22)
        logo_badge.setAlignment(Qt.AlignCenter)
        logo_badge.setStyleSheet(
            "QLabel { background: qlineargradient(x1:0, y1:0, x2:0.35, y2:1, "
            f"stop:0 {theme.ACCENT}, stop:1 {theme.ACCENT_700}); border-radius: 6px; "
            f"color: #F5F4FF; font-family: {theme.FONT_MONO}; font-size: 10px; font-weight: 700; }}"
        )
        logo_lay.addWidget(logo_badge)
        logo_text = label("ChatGrab")
        logo_text.setStyleSheet("font-size: 12.5px; font-weight: 600; letter-spacing: .02em; background: transparent;")
        logo_lay.addWidget(logo_text)
        logo_lay.addStretch(1)
        side_lay.addWidget(logo_row)

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

        # Design-brief.md §3.1 has one mono "РАЗДЕЛЫ" kicker above a flat
        # 8-item list; this app keeps its 4-block structure (resolved
        # question, see DESIGN_PLAN.md), so each block's own panel gets its
        # own kicker instead — the block's own existing label from BLOCKS,
        # uppercased, not a new piece of copy.
        self.nav_panels: dict[str, QWidget] = {}
        for block_key, block_title in BLOCKS:
            panel = QWidget()
            panel_lay = QVBoxLayout(panel)
            panel_lay.setContentsMargins(0, 0, 0, 0)
            panel_lay.setSpacing(2)
            panel_lay.addWidget(label(block_title.upper(), "kicker"))
            panel_lay.addSpacing(4)
            for key, title in NAV_BY_BLOCK[block_key]:
                self._add_nav_item(panel_lay, key, title)
            side_lay.addWidget(panel)
            self.nav_panels[block_key] = panel

        side_lay.addStretch(1)

        divider = QWidget()
        divider.setFixedHeight(1)
        divider.setStyleSheet("background: #33354a;")
        side_lay.addSpacing(8)
        side_lay.addWidget(divider)
        side_lay.addSpacing(8)
        side_lay.addWidget(label("ОБЩЕЕ", "kicker"))
        side_lay.addSpacing(4)

        for key, title in COMMON_ITEMS:
            self._add_nav_item(side_lay, key, title)

        self.queue_card = QFrame()
        self.queue_card.setObjectName("queueCard")
        self.queue_card.setStyleSheet(
            "QFrame#queueCard { background: rgba(145,132,217,23); "
            "border: 1px solid rgba(145,132,217,56); border-radius: 9px; }"
        )
        qc_lay = QVBoxLayout(self.queue_card)
        qc_lay.setContentsMargins(10, 10, 10, 10)
        qc_lay.setSpacing(7)
        qc_head = QHBoxLayout()
        qc_head.setContentsMargins(0, 0, 0, 0)
        qc_head.addWidget(label("ОЧЕРЕДЬ", "kicker"))
        qc_head.addStretch(1)
        self.queue_count_label = label("0 / 0")
        self.queue_count_label.setStyleSheet(
            f"color: {theme.ACCENT_300}; font-family: {theme.FONT_MONO}; font-size: 9.5px; "
            "background: transparent;"
        )
        qc_head.addWidget(self.queue_count_label)
        qc_lay.addLayout(qc_head)
        self.queue_text_label = label("")
        self.queue_text_label.setWordWrap(True)
        self.queue_text_label.setStyleSheet("color: #C9C9D1; font-size: 11.5px; background: transparent;")
        qc_lay.addWidget(self.queue_text_label)
        self.queue_progress = AnimatedProgressBar(height=4)
        qc_lay.addWidget(self.queue_progress)
        side_lay.addWidget(self.queue_card)
        side_lay.addSpacing(9)

        account_row = QWidget()
        ar_lay = QHBoxLayout(account_row)
        ar_lay.setContentsMargins(4, 0, 4, 2)
        ar_lay.setSpacing(7)
        self.account_dot = PulseDot(theme.BAD, diameter=6, halo=True)
        ar_lay.addWidget(self.account_dot)
        self.account_label = label("Нет подключения")
        self.account_label.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 11.5px; background: transparent;")
        ar_lay.addWidget(self.account_label, 1)
        side_lay.addWidget(account_row)

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

    def _add_nav_item(self, layout: QVBoxLayout, key: str, title: str) -> None:
        item = _NavItem(key, title)
        item.clicked.connect(self.navigate)
        layout.addWidget(item)
        self._nav_buttons[key] = item
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

        # Карточка очереди (design-brief.md §3.1, п.5). N/M — сколько чатов
        # уже полностью загружены из общего числа отслеживаемых, не только
        # тех, что сейчас активны в очереди — тот же источник данных
        # (db.list_chats()), просто дополнительное поле history_done.
        done = len([c for c in chats if c["history_done"]])
        self.queue_count_label.setText(f"{done} / {len(chats)}")
        if loading:
            self.queue_text_label.setText(f"Грузится «{loading[0]['title']}»")
            count = db.message_count(loading[0]["chat_id"])
            approx = loading[0]["approx_total"]
            if approx:
                self.queue_progress.set_progress(min(100.0, 100.0 * count / approx))
            else:
                self.queue_progress.set_progress(None)
            self.queue_progress.set_active(True)
        else:
            self.queue_progress.set_active(False)
            # Не 100% всегда — доля уже загруженных чатов от общего числа,
            # тот же N/M, что и в счётчике над текстом.
            self.queue_progress.set_progress(100.0 * done / len(chats) if chats else 0.0)
            if queued:
                self.queue_text_label.setText(f"В очереди {len(queued)} чат(ов)")
            else:
                self.queue_text_label.setText("История загружена, очередь пуста")

        # Ник аккаунта (design-brief.md §3.1, п.6) не показан — ctx.tg.me()
        # асинхронный, а этот метод дергается таймером синхронно; заводить
        # отдельный async-опрос ради подписи в сайдбаре — не то, что просит
        # эта сессия («данные из тех же источников, что сейчас», а сейчас
        # источник — только сам булев ctx.tg.authorized).
        if self.ctx.tg.authorized:
            self.account_dot.set_color(theme.GOOD)
            self.account_dot.set_pulsing(True)
            self.account_label.setText("Аккаунт подключён")
        else:
            self.account_dot.set_color(theme.BAD)
            self.account_dot.set_pulsing(False)
            self.account_label.setText("Нет подключения")

        self.tray.refresh()

        bots = db.list_bots()
        new_leads = len(db.list_leads(status="new"))
        bad_bots = len([b for b in bots if b["status"] == "error"])
        unseen = db.unseen_watch_count()

        self._set_nav_badge("chats", str(len(chats)) if chats else "")
        self._set_nav_badge("collect", "●" if loading else "", accent=True)
        self._set_nav_badge("bots", str(len(bots)) if bots else "", accent=True)
        self._set_nav_badge("watch", str(unseen) if unseen else "")
        self._set_nav_badge("leads", str(new_leads) if new_leads else "")
        connect_item = self._nav_buttons.get("connect")
        if connect_item is not None:
            connect_item.set_badge_dot(theme.GOOD if self.ctx.tg.authorized else theme.BAD)

        self._set_block_badge(
            "●" if loading else "", accent=True, button=self.block_buttons["collect"], base_label="Сбор",
        )
        self._set_block_badge(
            "!" if bad_bots else "", accent=False, button=self.block_buttons["bots"], base_label="Боты",
        )
        self._set_block_badge(
            str(new_leads) if new_leads else "", accent=True,
            button=self.block_buttons["leads"], base_label="Лиды",
        )

    def _set_nav_badge(self, key: str, badge: str, accent: bool = False) -> None:
        """Badges on the sidebar's per-screen rows (§3.1's "число чатов,
        число ботов, точки статусов") — `_NavItem`'s own dot/text badge
        API, not the block switcher's plain button text below."""
        item = self._nav_buttons.get(key)
        if item is None:
            return
        if not badge:
            item.clear_badge()
        elif badge == "●":
            item.set_badge_dot(theme.ACCENT_400 if accent else theme.TEXT_FAINT, pulsing=accent)
        else:
            # «Боты» — число запущенных ботов цветом GOOD_FG (design-brief.md
            # §3.1); остальные счётчики остаются нейтральными.
            color = theme.GOOD_FG if key == "bots" and accent else theme.TEXT_FAINT
            item.set_badge_text(badge, color)

    def _set_block_badge(self, badge: str, accent: bool, button: QPushButton, base_label: str) -> None:
        """The block-switcher tabs (Сбор/Боты/Лиды/Почта) stay plain
        `QPushButton`s — restyled by Д1's "blocktab" QSS, not rebuilt into
        `_NavItem`s — so their badge is still plain appended text."""
        button.setText(f"{base_label}   {badge}" if badge else base_label)

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
