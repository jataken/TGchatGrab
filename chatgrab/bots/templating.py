"""Message templating: `{variable}` substitution for outgoing bot text.

Until this existed, the Шаблоны library was write-only — templates were
stored and edited but no runner ever read them, and a `{name}` typed into
an action's text was delivered to the contact literally. Both paths now
come through `render`.

Substitution is deliberately not `str.format`: template text is
user-authored and routinely contains prices, JSON snippets and stray
braces, any of which would make `format` raise mid-send (or, worse, reach
into object attributes). This walks the string and replaces only names it
actually knows, leaving everything else exactly as typed.
"""
from __future__ import annotations

import json
import re

_VAR_RE = re.compile(r"\{(\w+)\}")


def variables_in(text: str) -> list[str]:
    """Every distinct `{name}` in the text, in first-appearance order."""
    seen: list[str] = []
    for name in _VAR_RE.findall(text or ""):
        if name not in seen:
            seen.append(name)
    return seen


def render(text: str, values: dict[str, object]) -> str:
    """Replace `{name}` with values[name]. Unknown names are left as-is —
    a half-filled message is easier for the user to diagnose than a
    silently blanked one, and an exception here would abort a send."""
    if not text:
        return ""

    def sub(match: re.Match) -> str:
        name = match.group(1)
        if name in values and values[name] is not None:
            return str(values[name])
        return match.group(0)

    return _VAR_RE.sub(sub, text)


def context_for(db, bot_id: int, contact_row=None, answers: dict | None = None,
                 event_text: str | None = None) -> dict[str, object]:
    """Assemble what a template may refer to: the contact's own fields, the
    answers collected so far in a scenario, and the bot's identity.

    Scenario answers come last so a field the user named `name` in their
    own scenario wins over the contact's Telegram username — the scenario
    is the more specific, more deliberate source.
    """
    ctx: dict[str, object] = {}
    bot = db.get_bot(bot_id)
    if bot:
        ctx["bot_name"] = bot["name"]
        ctx["manager"] = bot["manager_chat_id"] or ""

    if contact_row is not None:
        username = contact_row["username"] or ""
        ctx["username"] = f"@{username}" if username else ""
        # `name` is what a template author reaches for first; fall back
        # through the identifiers Telegram actually gives us.
        ctx["name"] = username or str(contact_row["telegram_id"])
        ctx["telegram_id"] = contact_row["telegram_id"]
        try:
            tags = json.loads(contact_row["tags"])
        except (json.JSONDecodeError, TypeError):
            tags = []
        ctx["tags"] = ", ".join(tags)

    if event_text is not None:
        ctx["text"] = event_text

    for key, value in (answers or {}).items():
        ctx[key] = value

    return ctx


def resolve_action_text(db, action_cfg: dict, bot_id: int, values: dict[str, object]) -> str:
    """The text an action should send, from either a chosen template or its
    own inline text.

    `template_id` wins when set; `text` remains supported so actions
    configured before templates were wired up keep working untouched.
    A template that has since been deleted falls back to the inline text
    rather than sending an empty message.
    """
    template_id = action_cfg.get("template_id")
    if template_id is not None:
        template = db.get_template(template_id)
        if template is not None:
            return render(template["text"], values)
    return render(action_cfg.get("text", ""), values)
