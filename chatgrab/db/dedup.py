"""Fingerprinting repeated message text.

On a trading-board chat the same offer gets reposted for weeks — «Флаконы
ПЭТ 250 мл в наличии, 12 000 шт» verbatim every Monday. Those are distinct
messages with distinct ids, so the existing (chat_id, message_id)
deduplication does nothing for them, and they all land in the export: more
noise to read through and more tokens to pay for.

A fingerprint is a hash of the text after normalization, stored on the row
so repeats can be found with an index instead of comparing every pair.

Deliberately conservative about *what* gets a fingerprint:

- Text shorter than MIN_LENGTH gets none. Short replies («да», «в личку»,
  «актуально?») legitimately repeat all day and are not what anyone means
  by a duplicate; fingerprinting them would collapse real conversation.
- Media-only messages get none — identical captions on different photos
  are different offers.

Normalization folds the things that change between reposts without
changing meaning: case, whitespace, and the decorative separators people
put around price lists.
"""
from __future__ import annotations

import hashlib
import re

MIN_LENGTH = 40

_WS_RE = re.compile(r"\s+")
_DECOR_RE = re.compile(r"[·•—–\-=_*~«»\"'`|/\\]+")


def normalize(text: str) -> str:
    text = (text or "").lower()
    text = _DECOR_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text)
    return text.strip()


def fingerprint(text: str) -> str | None:
    """A stable short hash of the normalized text, or None when this text
    should never be treated as a repeat."""
    normalized = normalize(text)
    if len(normalized) < MIN_LENGTH:
        return None
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]
