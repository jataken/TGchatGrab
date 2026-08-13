"""Per-bot sending limits.

These exist to keep a userbot from behaving like a spammer. Telegram
restricts an ordinary account for *bursts* of unsolicited outgoing
messages, and the previous per-contact cooldown did nothing to prevent
one: it was keyed on (bot, contact), so a reminder sweep over 500 silent
contacts sent 500 messages back-to-back with no delay at all, which is
precisely the pattern that gets a number limited.

Three limits, each answering a different question:

- `dm_cooldown_seconds` — how soon may this bot message *the same* person
  again. Stops a loop or a duplicated rule from pestering one contact.
- `send_gap_seconds` — minimum spacing between *any* two sends by this
  bot. This is the one that flattens a burst across many contacts.
- `max_reminders_per_tick` — a ceiling on one inactivity sweep, so a
  backlog is spread over several ticks instead of going out at once.

Defaults are deliberately conservative: with a 3-second gap and a cap of
25, a sweep tops out at roughly a message every three seconds for about
75 seconds, which reads as a person working through a list.
"""
from __future__ import annotations

import json

DEFAULTS: dict[str, float | int] = {
    "dm_cooldown_seconds": 30,
    "send_gap_seconds": 3.0,
    "max_reminders_per_tick": 25,
}

# Guard rails — a value outside these can only hurt the account it is
# meant to protect, so the UI and any hand-edited database row are both
# clamped rather than trusted.
BOUNDS: dict[str, tuple[float, float]] = {
    "dm_cooldown_seconds": (0, 86400),
    "send_gap_seconds": (0.0, 600.0),
    "max_reminders_per_tick": (1, 500),
}


def load(bot_row) -> dict:
    """Settings for a bot row, with defaults filled in and values clamped."""
    raw = {}
    if bot_row is not None:
        try:
            # `settings` is absent on rows read by older code paths in tests.
            raw = json.loads(bot_row["settings"] or "{}")
        except (json.JSONDecodeError, TypeError, IndexError, KeyError):
            raw = {}
    return normalize(raw)


def normalize(raw: dict) -> dict:
    out = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        if key not in raw or raw[key] is None:
            continue
        try:
            value = type(default)(raw[key])
        except (TypeError, ValueError):
            continue
        low, high = BOUNDS[key]
        out[key] = max(low, min(high, value))
    return out


def dumps(values: dict) -> str:
    return json.dumps(normalize(values), ensure_ascii=False)
