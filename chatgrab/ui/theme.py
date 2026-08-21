"""Design tokens and QSS for «Плотный рефреш» — see DESIGN_PLAN.md (Д1) and
design/design-brief.md §2 for the source spec these constants are copied
from verbatim. Font falls back to the OS UI font (Segoe UI on Windows)
since bundling a font file isn't practical for a portable build; visually
it sits close enough to Inter for interface text. Consolas is assumed
present (it ships with Windows and most dev toolchains) with a plain
monospace fallback for platforms that lack it.

Qt Style Sheets have no `box-shadow` property — the brief's shadow specs
(button glow, tile-hover shadow) can't live in `build_qss()` at all; they're
kept below as plain-tuple constants for a later session's widgets to apply
via `QGraphicsDropShadowEffect` (theme.py itself takes no PySide6 import so
it stays safe to import from a non-Qt context, e.g. a script or test that
never builds a QApplication).

Everything scoped strictly to this session's own file boundary (Д1: tokens
+ QSS only) stays here — no QWidget subclasses, no icons, no per-screen
layout. The workspace grid background is a pure paintEvent-callable drawing
helper (draw_grid_background, bottom of file) rather than a wired-in
QWidget#root override: `root` itself is built in main_window.py, which is
Д3's file, not this one's.
"""
from __future__ import annotations

# ---- Цвета (design-brief.md §2) --------------------------------------------
BG = "#161826"
SIDEBAR_BG = "#13151F"
SURFACE = "#232532"
SURFACE_INPUT = "#1C1E2C"
LOG_BG = "#12141D"
DIVIDER = "#33354A"
DIVIDER_SOFT = "#2C2E3D"
BORDER_HOVER = "#3F424D"

TEXT = "#E9E9ED"
TEXT_MUTED = "#9A9AA3"
TEXT_FAINT = "#6C6C78"
TEXT_LOG = "#D6D6DB"

ACCENT = "#9184D9"
ACCENT_100 = "#F5F4FF"
ACCENT_200 = "#E7E5FE"
ACCENT_300 = "#D2CEFD"
ACCENT_400 = "#B5ABFC"
ACCENT_600 = "#796CBF"
ACCENT_700 = "#5D5294"
ACCENT_800 = "#423A6A"
ACCENT_900 = "#2B2741"
ACCENT_FILL = "#7A6BD0"
ACCENT_FILL_HOV = "#8878E0"
ACCENT_ON_FILL = "#F7F6FF"

GOOD = "#7FC79B"
GOOD_BG = "rgba(120,190,150,40)"
GOOD_FG = "#BFE5CD"
WARN = "#F0C6A0"
WARN_BG = "rgba(220,150,90,40)"
WARN_FG = "#E2C9B4"
# BAD/BAD_FG раньше были #C98A9A/#F0C6CF — приглушённее, чем нужно брифу, и
# не совпадали с уже верным STATUS_STYLES["error"] ниже. Приведено к тому
# же значению, которое там уже жило как "dot"/"fg" по отдельности.
BAD = "#C85A6E"
BAD_BG = "rgba(180,70,90,40)"
BAD_FG = "#E9B3BF"

# Полупрозрачные подложки (design-brief.md §2 «Полупрозрачные подложки») —
# именованные, чтобы новый код не набирал rgba(...) с руки. Значения,
# уже встречавшиеся как литералы в STATUS_STYLES/build_qss ниже, не
# продублированы заново под новым именем без нужды — они и так те же числа.
OVERLAY_ACCENT_WEAK = "rgba(145,132,217,31)"
OVERLAY_ACCENT_ACTIVE = "rgba(145,132,217,41)"
HOVER_NEUTRAL = "rgba(233,233,237,15)"
CHECKBOX_OFF_BG = "rgba(233,233,237,8)"
CARD_INNER_GLINT = "rgba(255,255,255,10)"

STATUS_STYLES = {
    "idle": {"label": "простаивает", "bg": "rgba(233,233,237,20)", "fg": "#b3b3ba", "dot": "#75798c"},
    "loading": {"label": "грузит историю", "bg": "rgba(145,132,217,46)", "fg": "#d2cefd", "dot": ACCENT_400},
    "queued": {"label": "в очереди", "bg": "rgba(145,132,217,26)", "fg": "#b5afe8", "dot": "#5d5294"},
    "listening": {"label": "слушает новые", "bg": "rgba(120,190,150,36)", "fg": "#bfe5cd", "dot": GOOD},
    "off": {"label": "сбор выключен", "bg": "rgba(233,233,237,13)", "fg": "#6c6c78", "dot": "#3f424d"},
    "running": {"label": "работает", "bg": "rgba(120,190,150,36)", "fg": "#bfe5cd", "dot": GOOD},
    "stopped": {"label": "остановлен", "bg": "rgba(233,233,237,13)", "fg": "#6c6c78", "dot": "#3f424d"},
    "error": {"label": "ошибка", "bg": "rgba(200,90,110,40)", "fg": "#e9b3bf", "dot": "#c85a6e"},
}

# ---- Типографика (design-brief.md §2 «Типографика») ------------------------
FONT_SANS = '"Segoe UI", "Inter", sans-serif'
FONT_MONO = '"Consolas", "Courier New", monospace'

# ---- Геометрия (design-brief.md §2 «Геометрия») -----------------------------
RADIUS_CARD = 11
RADIUS_CARD_OUTER = 12  # панель журнала и внешние контейнеры
RADIUS_BUTTON = 8
RADIUS_PILL = 6  # плашка статуса
RADIUS_ICON_BUTTON = 7
RADIUS_CHECKBOX = 5
BORDER_WIDTH = 1
PADDING_CARD = "14px 16px"
GAP_TILE_GRID = 12

# Тени — вне QSS (см. докстринг модуля), для QGraphicsDropShadowEffect в
# более поздней сессии. Простые кортежи, не QColor: theme.py не тянет
# PySide6 сам по себе.
SHADOW_BUTTON_PRIMARY = {"rgba": (122, 107, 208, 77), "blur": 16, "offset": (0, 6)}
SHADOW_TILE_HOVER = {"rgba": (0, 0, 0, 89), "blur": 26, "offset": (0, 10)}

# Едва заметная сетка фона рабочей области (design-brief.md §2 «Фон рабочей
# области») — сами линии, шаг между ними.
GRID_STEP = 32
GRID_LINE_RGBA = (145, 132, 217, 9)


# Единственный стиль на всех платформах — см. apply_theme ниже.
BASE_STYLE = "Fusion"


def apply_theme(app) -> None:
    """Pin the widget style *and* the stylesheet, in that order.

    Both halves matter and only together: the стиль decides whether the
    QSS is honoured at all. Everything that renders the app — the real
    entry point and the test harness alike — goes through here, so a
    screenshot taken during development is a screenshot of what ships.
    """
    app.setStyle(BASE_STYLE)
    # Once a stylesheet is set, app.style() is a QStyleSheetStyle wrapper
    # that no longer reports the base style's name, so record it here —
    # both the test and «сведения о сборке» read it back.
    app.setProperty("chatgrab_base_style", app.style().objectName())
    app.setStyleSheet(build_qss())


def build_qss() -> str:
    return f"""
    * {{ font-family: {FONT_SANS}; }}
    QMainWindow, QWidget#root {{ background: {BG}; color: {TEXT}; }}
    QWidget {{ color: {TEXT}; }}
    QLabel {{ background: transparent; }}
    QLabel[class="h1"] {{ font-size: 20px; font-weight: 600; }}
    QLabel[class="h2"] {{ font-size: 16px; font-weight: 600; }}
    QLabel[class="muted"] {{ color: {TEXT_MUTED}; font-size: 12px; }}
    QLabel[class="faint"] {{ color: {TEXT_FAINT}; font-size: 11px; }}
    /* Кикер — моно, не обычный текст (ключевая часть нового языка, см.
       брифа §2 «Технический шрифт») — letter-spacing здесь чуть шире, чем
       у обычного текста, ради разрежённого uppercase-вида; текст самого
       кикера уже должен приходить в верхнем регистре из вызывающего
       кода — полагаться на CSS text-transform для кириллицы ненадёжно
       между платформами. */
    QLabel[class="kicker"] {{
        color: {TEXT_FAINT}; font-size: 9.5px; font-family: {FONT_MONO};
        letter-spacing: 1px;
    }}
    /* Крупное число-метрика — моно, tabular по природе самого моно-шрифта,
       отдельной настройки tabular-nums не требуется. */
    QLabel[class="metric"] {{ color: {TEXT}; font-size: 19px; font-family: {FONT_MONO}; }}
    QLabel[class="metric-tile"] {{ color: {TEXT}; font-size: 21px; font-family: {FONT_MONO}; }}
    QLabel[class="metric-giant"] {{ color: {TEXT}; font-size: 30px; font-family: {FONT_MONO}; }}
    QLabel[class="mono"] {{ font-family: {FONT_MONO}; }}
    QLabel[class="handle"] {{ color: {TEXT_FAINT}; font-size: 10.5px; font-family: {FONT_MONO}; }}

    QWidget[class="card"] {{
        background: {SURFACE}; border-radius: {RADIUS_CARD}px; border: 1px solid {DIVIDER};
    }}
    QWidget[class="sidebar"] {{ background: {SIDEBAR_BG}; }}
    QWidget[class="logpanel"] {{
        background: {LOG_BG}; border-radius: {RADIUS_CARD_OUTER}px; border: 1px solid {DIVIDER};
    }}

    /* Sidebar nav row (Д3, design-brief.md §3.1) — one shared rule set for
       every _NavItem instead of ~26 duplicated per-widget stylesheets, the
       same convention "card"/"chip"/"blocktab" already use. Checked state
       is a dynamic property (`navChecked`), toggled via unpolish/polish —
       the standard Qt idiom for state that isn't a native pseudo-class. */
    QFrame[class="navitem"] {{ background: transparent; border-radius: 8px; }}
    QFrame[class="navitem"]:hover {{ background: {HOVER_NEUTRAL}; }}
    QFrame[class="navitem"][navChecked="true"] {{ background: {OVERLAY_ACCENT_ACTIVE}; }}
    QLabel[class="navtitle"] {{ color: rgba(233,233,237,.66); font-size: 13px; background: transparent; }}
    QFrame[class="navitem"]:hover QLabel[class="navtitle"] {{ color: {TEXT}; }}
    QFrame[class="navitem"][navChecked="true"] QLabel[class="navtitle"] {{ color: {ACCENT_300}; }}
    QLabel[class="navbadge"] {{
        color: {TEXT_FAINT}; font-family: {FONT_MONO}; font-size: 10.5px; background: transparent;
    }}

    QPushButton {{
        border-radius: {RADIUS_BUTTON}px; padding: 6px 14px; font-size: 12.5px; font-weight: 500;
        border: 1px solid transparent; background: transparent; color: {TEXT};
    }}
    /* Заливка, не обводка — главное отличие от прежнего вида (design-brief.md
       §3.3): «залитая» primary с собственным акцентным цветом текста и
       затенением при наведении/выключении, а не рамка поверх фона карточки. */
    QPushButton[class="primary"] {{ background: {ACCENT_FILL}; color: {ACCENT_ON_FILL}; border: none; }}
    QPushButton[class="primary"]:hover {{ background: {ACCENT_FILL_HOV}; }}
    QPushButton[class="primary"]:pressed {{ background: {ACCENT_700}; }}
    QPushButton[class="primary"]:disabled {{ background: rgba(122,107,208,89); color: {TEXT_FAINT}; }}
    QPushButton[class="secondary"] {{ border: 1px solid {DIVIDER}; }}
    QPushButton[class="secondary"]:hover {{ background: {HOVER_NEUTRAL}; }}
    QPushButton[class="ghost"] {{ color: {ACCENT_600}; }}
    QPushButton[class="ghost"]:hover {{ color: {ACCENT_400}; background: transparent; }}
    QPushButton[class="danger"] {{ color: {TEXT_MUTED}; }}
    QPushButton[class="danger"]:hover {{ background: rgba(200,90,110,41); color: {BAD_FG}; }}
    /* Иконка-кнопка (✕ и подобные) — квадрат 24×24, задаётся вызывающим
       кодом через setFixedSize; здесь только цвет/форма. */
    QPushButton[class="icon"] {{
        border-radius: {RADIUS_ICON_BUTTON}px; padding: 0; color: {TEXT_FAINT}; border: none;
    }}
    QPushButton[class="icon"]:hover {{ background: rgba(200,90,110,41); color: {BAD_FG}; }}

    /* Block switcher (Сбор / Боты / Лиды / Почта) and filter chips — a
       checkable pill, plain text so an unread-count badge can be appended
       to it. */
    QPushButton[class="blocktab"] {{
        border-radius: 7px; padding: 7px 4px; font-size: 12.5px; font-weight: 500;
        color: {TEXT_MUTED}; background: transparent; border: 1px solid transparent;
    }}
    QPushButton[class="blocktab"]:hover {{ background: {HOVER_NEUTRAL}; color: {TEXT}; }}
    QPushButton[class="blocktab"]:checked {{
        color: {ACCENT_200}; background: {OVERLAY_ACCENT_ACTIVE};
        border: 1px solid {ACCENT};
    }}
    QPushButton[class="chip"] {{
        border-radius: 7px; padding: 5px 11px; font-size: 12.5px;
        color: {TEXT_MUTED}; background: {CHECKBOX_OFF_BG};
        border: 1px solid {DIVIDER_SOFT};
    }}
    QPushButton[class="chip"]:hover {{ background: {HOVER_NEUTRAL}; }}
    QPushButton[class="chip"]:checked {{
        color: {ACCENT_400}; background: rgba(145,132,217,36); border-color: rgba(145,132,217,115);
    }}

    QLineEdit, QDateEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
        background: {SURFACE_INPUT}; border: 1px solid {DIVIDER}; border-radius: {RADIUS_BUTTON}px;
        padding: 6px 10px; font-size: 13px; min-height: 20px; color: {TEXT};
        selection-background-color: {ACCENT_700};
    }}
    QLineEdit:focus, QComboBox:focus, QDateEdit:focus {{ border: 1px solid {ACCENT}; }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QTextEdit, QPlainTextEdit {{
        background: {SURFACE_INPUT}; border: 1px solid {DIVIDER}; border-radius: {RADIUS_BUTTON}px;
        padding: 6px; font-size: 13px; color: {TEXT};
    }}

    /* Any list/table/dropdown-popup view: dark background + light text,
       explicitly — otherwise it falls back to the OS's (usually light)
       palette while text stays light, which is unreadable. */
    QAbstractItemView {{
        background: {SURFACE}; color: {TEXT}; border: 1px solid {DIVIDER};
        border-radius: {RADIUS_BUTTON}px; outline: none;
        selection-background-color: rgba(145,132,217,60);
        selection-color: {ACCENT_100};
    }}
    QComboBox QAbstractItemView {{
        background: {SURFACE}; color: {TEXT}; border: 1px solid {DIVIDER};
        selection-background-color: rgba(145,132,217,60); selection-color: {ACCENT_100};
    }}
    QListWidget, QListView, QTreeView {{
        background: {SURFACE}; color: {TEXT}; border: 1px solid {DIVIDER};
        border-radius: {RADIUS_BUTTON}px;
    }}
    QListWidget::item, QListView::item {{ padding: 5px 8px; border-radius: 5px; }}
    QListWidget::item:selected, QListView::item:selected {{
        background: rgba(145,132,217,60); color: {ACCENT_100};
    }}
    QListWidget::item:hover, QListView::item:hover {{ background: {HOVER_NEUTRAL}; }}

    QTableWidget {{
        background: transparent; border: none; gridline-color: {DIVIDER};
        selection-background-color: rgba(145,132,217,30); color: {TEXT}; font-size: 13px;
    }}
    QTableWidget::item {{ padding: 6px; border-bottom: 1px solid {DIVIDER}; color: {TEXT}; }}
    QHeaderView::section {{
        background: transparent; color: {TEXT_FAINT}; border: none;
        border-bottom: 1px solid {DIVIDER}; padding: 6px; font-size: 9.5px;
        font-family: {FONT_MONO}; text-transform: uppercase;
    }}

    /* QMessageBox / QInputDialog / our own dialogs are all QDialog under
       the hood — style the base class once so none of them silently
       revert to a light native background under dark text. */
    QDialog {{ background: {BG}; color: {TEXT}; }}
    QMessageBox {{ background: {BG}; }}
    QMessageBox QLabel {{ color: {TEXT}; background: transparent; }}
    /* Only the auto-generated OK/Cancel buttons of native QMessageBox /
       QInputDialog live in a QDialogButtonBox — our own dialog buttons
       carry an explicit class (primary/secondary/ghost) and are styled
       by the rules above instead, so this can't fight with those. */
    QDialogButtonBox QPushButton {{
        border: 1px solid {DIVIDER}; border-radius: {RADIUS_BUTTON}px; padding: 6px 14px;
        min-width: 64px; background: rgba(233,233,237,6); color: {TEXT};
    }}
    QDialogButtonBox QPushButton:hover {{ background: {HOVER_NEUTRAL}; }}

    /* QScrollArea auto-creates an internal viewport widget that paints its
       own (light) palette background by default, independent of any
       stylesheet on the QScrollArea itself — reach through it and the
       content widget explicitly, or every scrollable screen renders as a
       white page with barely visible text on top. */
    QScrollArea {{ border: none; background: transparent; }}
    QScrollArea > QWidget > QWidget {{ background: transparent; }}
    QScrollBar:vertical {{ width: 10px; background: transparent; }}
    QScrollBar::handle:vertical {{ background: {BORDER_HOVER}; border-radius: 5px; min-height: 24px; }}
    QScrollBar::handle:vertical:hover {{ background: #55586a; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    QScrollBar:horizontal {{ height: 10px; background: transparent; }}
    QScrollBar::handle:horizontal {{ background: {BORDER_HOVER}; border-radius: 5px; }}

    QProgressBar {{
        background: rgba(233,233,237,20); border: none; border-radius: 4px; height: 8px;
    }}
    QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}

    /* Системный чекбокс/индикатор — сохраняется как рабочая база везде,
       где ещё не подключён TabletCheckBox (Д2). Цвета уже на новых
       токенах, чтобы переходное состояние не выглядело чужеродным. */
    QCheckBox {{ font-size: 13px; spacing: 8px; }}
    QCheckBox::indicator {{
        width: 16px; height: 16px; border-radius: {RADIUS_CHECKBOX}px; border: 1px solid {DIVIDER};
    }}
    QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}

    QRadioButton {{ font-size: 13px; spacing: 8px; }}

    QTabWidget::pane {{ border: 1px solid {DIVIDER}; border-radius: 10px; }}
    QTabBar::tab {{
        background: transparent; padding: 8px 14px; color: {TEXT_MUTED}; font-size: 12.5px;
    }}
    QTabBar::tab:selected {{ color: {ACCENT_400}; }}

    QToolTip {{
        background: {SURFACE}; color: {TEXT}; border: 1px solid {DIVIDER}; padding: 4px 8px;
    }}
    """


def draw_grid_background(painter, rect) -> None:
    """The barely-there 32×32 grid behind the workspace (design-brief.md
    §2 «Фон рабочей области») — a pure drawing routine over an already-
    constructed QPainter/QRect, not a QWidget. Import is local to this
    function, not module-level: everything else in theme.py is plain
    strings/numbers, importable from a script that never builds a
    QApplication, and this is the one function that actually needs Qt.

    Not wired into `root` yet — that widget is built in main_window.py,
    which is Д3's file, not this session's. Д3 calls this from a
    paintEvent override on the root/content container.
    """
    from PySide6.QtGui import QColor

    pen = painter.pen()
    pen.setColor(QColor(*GRID_LINE_RGBA))
    painter.setPen(pen)
    x = rect.left()
    while x < rect.right():
        painter.drawLine(x, rect.top(), x, rect.bottom())
        x += GRID_STEP
    y = rect.top()
    while y < rect.bottom():
        painter.drawLine(rect.left(), y, rect.right(), y)
        y += GRID_STEP
