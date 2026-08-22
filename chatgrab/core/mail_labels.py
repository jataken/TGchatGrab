"""П6: pure helpers for mail labels — the default set seeded on a new
mailbox, and the IMAP custom-keyword name a label maps to when pushed as
a server-side flag. No sqlite, no Qt, no socket — same split as
mail_compose.py/mail_thread.py, kept testable without a database.
"""
from __future__ import annotations

# «Набор по умолчанию» from PLAN.md's П6 checklist, seeded once per new
# mailbox (see db/mixins/mail.py: seed_default_mail_labels). Six labels,
# hotkeys 1-6 assigned in this order — 7-9 stay free for labels the user
# adds themselves.
DEFAULT_LABELS: list[tuple[str, str]] = [
    ("Заказ", "#4f7cff"),
    ("Запрос КП", "#f0a63a"),
    ("Счёт", "#28a99e"),
    ("Срочно", "#e5484d"),
    ("Рассылка", "#8a8f98"),
    ("Личное", "#a875e8"),
]


def label_keyword(label_id: int) -> str:
    """The IMAP keyword (custom flag) a label is pushed/pulled as, when
    the server's PERMANENTFLAGS allows arbitrary keywords (RFC 3501
    §2.3.2) — "Ярлыки отражаются в IMAP-ключевые слова" from the
    checklist. Deliberately built from the label's *id*, not its display
    name: an id is a valid IMAP atom with zero sanitizing (no spaces, no
    Cyrillic, nothing RFC 3501's atom-specials would reject) and never
    changes on rename — a renamed label keeps pointing at the exact same
    server-side flag instead of orphaning whatever was already set under
    the old name. The trade-off is that another client (Outlook, if it
    even exposes arbitrary IMAP keywords at all) sees an opaque tag like
    "ChatGrabLabel3", not "Заказ" — visibility of *that* a message is
    labelled carries over; the label's own text does not.
    """
    return f"ChatGrabLabel{label_id}"
