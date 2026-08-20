"""П9: pulling lead fields out of an email — phone/ИНН/volume from the
message body by regex, and product/volume/unit/deadline from an
attachment's table (a real grid — xlsx/csv rows — matched by a header
row's column names; PDF and Word don't have a grid this module can see,
so they fall back to the same body-text regex extraction, per the
checklist's own split: "в таблице ищется строка заголовков... из PDF и
Word текст разбирается регулярными выражениями").

No sqlite, no Qt, no network — same discipline as mail_thread.py/
mail_triage.py: everything here is a pure function over text or a grid
of strings the caller already has. Every result is a *proposal*: nothing
here writes to a lead — the caller (the mail lead-creation dialog) shows
what was found and lets a human decide what to keep, per "предлагает
машина, подтверждает человек".
"""
from __future__ import annotations

import re

_PHONE_RE = re.compile(
    r"(?:\+?7|8)[\s\-.]?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{2}[\s\-.]?\d{2}")

# ИНН: 10 digits (a company) or 12 (a sole proprietor/individual) — a
# bare digit run, so it's anchored to word boundaries to avoid matching
# the middle of a longer number (an invoice id, a phone number already
# consumed above).
_INN_RE = re.compile(r"(?<!\d)(\d{10}|\d{12})(?!\d)")

# "2 тонны", "500 кг", "1,5 т", "10.5 тонн" — a number (., or , as the
# decimal separator) followed by a unit word, common ones only; this is
# a heuristic, not a parser, same spirit as mail_triage.py's phrase
# lists.
_VOLUME_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(тонн[а-я]*|кг|килограмм[а-я]*|шт\.?|штук[а-я]*|"
    r"л(?:итр[а-я]*)?|м3|куб(?:ов|а)?|упаковк[а-я]*|коробк[а-я]*)",
    re.IGNORECASE,
)

# Column-name synonyms a header row is matched against — first cell in
# a row containing one of these (case-insensitive substring) marks that
# column for that field. Order matters only for the reasons list, not
# extraction: a header could match more than one field's synonym set,
# each field claims its own column independently.
_COLUMN_SYNONYMS = {
    "product": ["наименование", "товар", "продукт", "название"],
    "volume": ["объём", "объем", "количество", "кол-во"],
    "unit": ["ед.", "единица", "ед изм"],
    "deadline": ["срок", "дата поставки", "поставка"],
}


def extract_phone(text: str) -> str | None:
    m = _PHONE_RE.search(text or "")
    return m.group(0) if m else None


def extract_inn(text: str) -> str | None:
    m = _INN_RE.search(text or "")
    return m.group(1) if m else None


def extract_volume(text: str) -> tuple[str, str] | None:
    """(value, unit) from the first match, e.g. ("2", "тонны") — the
    caller decides how to join them into bot_leads.volume/unit."""
    m = _VOLUME_RE.search(text or "")
    if not m:
        return None
    return m.group(1), m.group(2)


def extract_body_fields(text: str) -> dict:
    """Everything regex can find directly in a message body (or a PDF/
    Word attachment's flattened text — the caller passes whichever).
    Keys present only when actually found; a caller merges this into
    whatever a table extraction (below) already proposed."""
    out: dict = {}
    phone = extract_phone(text)
    if phone:
        out["phone"] = phone
    inn = extract_inn(text)
    if inn:
        out["inn"] = inn
    volume = extract_volume(text)
    if volume:
        out["volume"], out["unit"] = volume
    return out


def extract_table_fields(grid: list[list[str]]) -> dict:
    """grid: rows of cell strings (mail_attachment_text.read_xlsx_grid()'s
    per-sheet rows, or an equivalent CSV grid) — finds the first row
    where at least two cells match a known column synonym (a lone match
    is too weak a signal: many spreadsheets have exactly one column
    that happens to contain a word like "срок" without being a real
    order table), maps each matched column to the field it stands for,
    then pulls the first non-empty row below it as the proposed values.
    {} if no header row clears that bar, or the header has nothing below
    it — "found nothing" is a valid, common outcome, not a caller-visible
    error."""
    header_index = None
    column_fields: dict[int, str] = {}
    for row_index, row in enumerate(grid):
        matched: dict[int, str] = {}
        for col_index, cell in enumerate(row):
            low = (cell or "").strip().lower()
            if not low:
                continue
            for field, synonyms in _COLUMN_SYNONYMS.items():
                if any(syn in low for syn in synonyms):
                    matched[col_index] = field
                    break
        if len(matched) >= 2:
            header_index = row_index
            column_fields = matched
            break
    if header_index is None:
        return {}

    for row in grid[header_index + 1:]:
        out = {}
        for col_index, field in column_fields.items():
            if col_index < len(row) and (row[col_index] or "").strip():
                out[field] = row[col_index].strip()
        if out:
            return out
    return {}
