"""Colors and QSS translating the Nocturne design tokens from the mockup
into Qt style sheets. Font falls back to the OS UI font (Segoe UI on
Windows) since bundling a font file isn't practical for a portable build;
visually it sits close enough to Inter for interface text."""
from __future__ import annotations

BG = "#161826"
SURFACE = "#232532"
TEXT = "#e9e9ed"
TEXT_MUTED = "#9a9aa3"
TEXT_FAINT = "#6c6c78"
DIVIDER = "#33354a"
ACCENT = "#9184d9"
ACCENT_100 = "#f5f4ff"
ACCENT_200 = "#e7e5fe"
ACCENT_300 = "#d2cefd"
ACCENT_400 = "#b5abfc"
ACCENT_600 = "#796cbf"
ACCENT_700 = "#5d5294"
ACCENT_800 = "#423a6a"
ACCENT_900 = "#2b2741"
SIDEBAR_BG = "#13151f"
LOG_BG = "#12141d"
GOOD = "#7fc79b"
GOOD_BG = "rgba(120,190,150,40)"
GOOD_FG = "#bfe5cd"
WARN = "#f0c6a0"
WARN_BG = "rgba(220,150,90,40)"
BAD = "#c98a9a"
BAD_BG = "rgba(180,70,90,40)"
BAD_FG = "#f0c6cf"

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


def build_qss() -> str:
    return f"""
    * {{ font-family: "Segoe UI", "Inter", sans-serif; }}
    QMainWindow, QWidget#root {{ background: {BG}; color: {TEXT}; }}
    QWidget {{ color: {TEXT}; }}
    QLabel {{ background: transparent; }}
    QLabel[class="h1"] {{ font-size: 21px; font-weight: 600; }}
    QLabel[class="h2"] {{ font-size: 16px; font-weight: 600; }}
    QLabel[class="muted"] {{ color: {TEXT_MUTED}; font-size: 12px; }}
    QLabel[class="faint"] {{ color: {TEXT_FAINT}; font-size: 11px; }}
    QLabel[class="kicker"] {{ color: {TEXT_MUTED}; font-size: 10px; letter-spacing: 1px; }}

    QWidget[class="card"] {{
        background: {SURFACE}; border-radius: 12px; border: 1px solid {DIVIDER};
    }}
    QWidget[class="sidebar"] {{ background: {SIDEBAR_BG}; }}
    QWidget[class="logpanel"] {{ background: {LOG_BG}; border-radius: 12px; border: 1px solid {DIVIDER}; }}

    QPushButton {{
        border-radius: 8px; padding: 7px 16px; font-size: 13px; font-weight: 500;
        border: 1px solid transparent; background: transparent; color: {TEXT};
    }}
    QPushButton[class="primary"] {{ color: {ACCENT_400}; border: 1px solid {ACCENT}; }}
    QPushButton[class="primary"]:hover {{ background: rgba(145,132,217,30); }}
    QPushButton[class="primary"]:pressed {{ background: rgba(145,132,217,55); }}
    QPushButton[class="primary"]:disabled {{ color: {TEXT_FAINT}; border-color: {DIVIDER}; }}
    QPushButton[class="secondary"] {{ border: 1px solid {DIVIDER}; }}
    QPushButton[class="secondary"]:hover {{ background: rgba(233,233,237,18); }}
    QPushButton[class="ghost"] {{ color: {ACCENT}; }}
    QPushButton[class="ghost"]:hover {{ background: rgba(145,132,217,26); }}
    QPushButton[class="danger"] {{ color: {BAD_FG}; }}
    QPushButton[class="danger"]:hover {{ background: rgba(200,90,110,40); }}

    /* Block switcher (Сбор / Боты) and filter chips — a checkable pill,
       plain text so an unread-count badge can be appended to it. */
    QPushButton[class="blocktab"] {{
        border-radius: 7px; padding: 6px 4px; font-size: 12.5px; font-weight: 500;
        color: rgba(233,233,237,0.72); background: transparent; border: none;
    }}
    QPushButton[class="blocktab"]:hover {{ background: rgba(233,233,237,10); }}
    QPushButton[class="blocktab"]:checked {{ color: {TEXT}; background: rgba(145,132,217,56); }}
    QPushButton[class="chip"] {{
        border-radius: 7px; padding: 5px 11px; font-size: 12.5px;
        color: rgba(233,233,237,0.7); background: rgba(233,233,237,8);
        border: 1px solid rgba(233,233,237,20);
    }}
    QPushButton[class="chip"]:hover {{ background: rgba(233,233,237,18); }}
    QPushButton[class="chip"]:checked {{
        color: {ACCENT_400}; background: rgba(145,132,217,36); border-color: rgba(145,132,217,115);
    }}

    QLineEdit, QDateEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
        background: {SURFACE}; border: 1px solid {DIVIDER}; border-radius: 8px;
        padding: 6px 10px; font-size: 13px; min-height: 20px; color: {TEXT};
        selection-background-color: {ACCENT_700};
    }}
    QLineEdit:focus, QComboBox:focus, QDateEdit:focus {{ border: 1px solid {ACCENT}; }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QTextEdit, QPlainTextEdit {{
        background: {SURFACE}; border: 1px solid {DIVIDER}; border-radius: 8px;
        padding: 6px; font-size: 13px; color: {TEXT};
    }}

    /* Any list/table/dropdown-popup view: dark background + light text,
       explicitly — otherwise it falls back to the OS's (usually light)
       palette while text stays light, which is unreadable. */
    QAbstractItemView {{
        background: {SURFACE}; color: {TEXT}; border: 1px solid {DIVIDER};
        border-radius: 8px; outline: none;
        selection-background-color: rgba(145,132,217,60);
        selection-color: {ACCENT_100};
    }}
    QComboBox QAbstractItemView {{
        background: {SURFACE}; color: {TEXT}; border: 1px solid {DIVIDER};
        selection-background-color: rgba(145,132,217,60); selection-color: {ACCENT_100};
    }}
    QListWidget, QListView, QTreeView {{
        background: {SURFACE}; color: {TEXT}; border: 1px solid {DIVIDER};
        border-radius: 8px;
    }}
    QListWidget::item, QListView::item {{ padding: 5px 8px; border-radius: 5px; }}
    QListWidget::item:selected, QListView::item:selected {{
        background: rgba(145,132,217,60); color: {ACCENT_100};
    }}
    QListWidget::item:hover, QListView::item:hover {{ background: rgba(233,233,237,15); }}

    QTableWidget {{
        background: transparent; border: none; gridline-color: {DIVIDER};
        selection-background-color: rgba(145,132,217,30); color: {TEXT}; font-size: 13px;
    }}
    QTableWidget::item {{ padding: 6px; border-bottom: 1px solid {DIVIDER}; color: {TEXT}; }}
    QHeaderView::section {{
        background: transparent; color: {TEXT_MUTED}; border: none;
        border-bottom: 1px solid {DIVIDER}; padding: 6px; font-size: 11px;
        text-transform: uppercase;
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
        border: 1px solid {DIVIDER}; border-radius: 8px; padding: 6px 14px;
        min-width: 64px; background: rgba(233,233,237,6); color: {TEXT};
    }}
    QDialogButtonBox QPushButton:hover {{ background: rgba(233,233,237,16); }}

    /* QScrollArea auto-creates an internal viewport widget that paints its
       own (light) palette background by default, independent of any
       stylesheet on the QScrollArea itself — reach through it and the
       content widget explicitly, or every scrollable screen (Обзор,
       Поиск, Экспорт, Настройки) renders as a white page with barely
       visible text on top. */
    QScrollArea {{ border: none; background: transparent; }}
    QScrollArea > QWidget > QWidget {{ background: transparent; }}
    QScrollBar:vertical {{ width: 10px; background: transparent; }}
    QScrollBar::handle:vertical {{ background: #3f424d; border-radius: 5px; min-height: 24px; }}
    QScrollBar::handle:vertical:hover {{ background: #55586a; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    QScrollBar:horizontal {{ height: 10px; background: transparent; }}
    QScrollBar::handle:horizontal {{ background: #3f424d; border-radius: 5px; }}

    QProgressBar {{
        background: rgba(233,233,237,20); border: none; border-radius: 4px; height: 8px;
    }}
    QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}

    QCheckBox {{ font-size: 13px; spacing: 8px; }}
    QCheckBox::indicator {{
        width: 16px; height: 16px; border-radius: 5px; border: 1px solid {DIVIDER};
    }}
    QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}

    QRadioButton {{ font-size: 13px; spacing: 8px; }}

    QTabWidget::pane {{ border: 1px solid {DIVIDER}; border-radius: 10px; }}
    QTabBar::tab {{
        background: transparent; padding: 8px 14px; color: {TEXT_MUTED}; font-size: 13px;
    }}
    QTabBar::tab:selected {{ color: {ACCENT_400}; }}

    QToolTip {{
        background: {SURFACE}; color: {TEXT}; border: 1px solid {DIVIDER}; padding: 4px 8px;
    }}
    """
