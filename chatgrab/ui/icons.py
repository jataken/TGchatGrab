"""Linear SVG sidebar icons — design-brief.md §3.1.

Files live in `resources/icons/*.svg`: viewBox 16, stroke-width 1.4,
`stroke="currentColor"`. Eight of the app's 26 sidebar entries
(today/connect/chats/collect/browse/export/bots/settings) copy their
`<path>`/`<rect>` outlines literally from `mockup-1a.html`, per the
brief's own "не рисуй свои иконки" instruction for those. The other 18
are new — the brief's mockup only ever covered a flat 8-item nav, and this
app kept its 4-block/26-item structure (see DESIGN_PLAN.md's resolved
Д-decisions) — drawn in the same visual language; the reasoning for each
is in DESIGN_PLAN.md's Д3 journal entry, not repeated here.

Recoloring is a literal `currentColor` → hex substitution on the raw SVG
text (these files use no other dynamic color), then rendered once into a
QPixmap per (key, color, size, opacity) and cached — sidebar rows redraw
often (every `_refresh_sidebar` tick) and re-parsing SVG on every repaint
would be wasteful for no visual benefit.
"""
from __future__ import annotations

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from ..paths import resource_path

# Nav keys with no SVG file of their own: the concept is literally the same
# as another key's icon (заявки/отчёты show up once under «Лиды» and once
# under «Почта» — same picture either way), so this maps to that file
# instead of duplicating it.
_ALIASES = {
    "mail_leads": "leads",
    "mail_reports": "reports",
}

_source_cache: dict[str, str | None] = {}
_icon_cache: dict[tuple[str, str, int, float], QIcon] = {}


def _svg_source(key: str) -> str | None:
    name = _ALIASES.get(key, key)
    if name in _source_cache:
        return _source_cache[name]
    path = resource_path("resources", "icons", f"{name}.svg")
    src = path.read_text(encoding="utf-8") if path.exists() else None
    _source_cache[name] = src
    return src


def nav_icon(key: str, color: str, size: int = 15, opacity: float = 1.0) -> QIcon | None:
    """A recolored QIcon for one sidebar nav key, or None if there's no SVG
    for it — callers fall back to a plain text-only row rather than a
    missing/broken icon glyph."""
    cache_key = (key, color, size, opacity)
    cached = _icon_cache.get(cache_key)
    if cached is not None:
        return cached
    src = _svg_source(key)
    if src is None:
        return None
    colored = src.replace("currentColor", color)
    renderer = QSvgRenderer(QByteArray(colored.encode("utf-8")))
    pixmap = QPixmap(QSize(size, size))
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setOpacity(max(0.0, min(1.0, opacity)))
    renderer.render(painter)
    painter.end()
    icon = QIcon(pixmap)
    _icon_cache[cache_key] = icon
    return icon
