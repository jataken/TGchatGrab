"""The preset library — JSON files under `presets/`, each installing a
scenario + templates + triggers + actions onto a bot in one call.

Built entirely on the existing engine, per PLAN.md С5's explicit "не
делаем": no graph model with its own node types. The original spec's
vocabulary maps onto what already exists — `delay` is an `inactivity`
trigger, a time `condition` is a `schedule` trigger or (for a
message-driven rule) a `time_window` in the trigger's own config
(rules_engine._within_time_window, added for this session's after_hours
preset), `handoff` is the `notify_manager` action.

A preset ships as a JSON file, never hardcoded in Python here, so the
library grows by adding a file — see presets/*.json for the five this
session ships, and `docs`-equivalent comments live in the JSON itself.
This is deliberately separate from bots/presets.py's b2b/b2c/custom
seeding, which stays as the simple, variable-free path it always was;
BotManager.create_bot dispatches between the two (see its own diff).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from ..db.database import Database
from ..paths import resource_path

_logger = logging.getLogger("chatgrab")

REQUIRED_KEYS = {"key", "version", "label", "variables", "templates", "triggers"}
VARIABLE_TYPES = {"text", "directions"}


class PresetError(ValueError):
    pass


def presets_dir() -> Path:
    return resource_path("presets")


def validate(spec: dict) -> None:
    missing = REQUIRED_KEYS - spec.keys()
    if missing:
        raise PresetError(f"нет обязательных полей: {sorted(missing)}")
    if not isinstance(spec["variables"], list):
        raise PresetError("variables должен быть списком")
    for v in spec["variables"]:
        if v.get("type") not in VARIABLE_TYPES:
            raise PresetError(f"неизвестный тип переменной: {v.get('type')!r}")
    if not isinstance(spec["templates"], list):
        raise PresetError("templates должен быть списком")
    if not isinstance(spec["triggers"], list) or not spec["triggers"]:
        raise PresetError("triggers должен быть непустым списком")
    template_keys = {t.get("key") for t in spec["templates"]}
    for trig in spec["triggers"]:
        for action in trig.get("actions", []):
            tpl_key = action.get("config", {}).get("template")
            if tpl_key and tpl_key not in template_keys:
                raise PresetError(f"действие ссылается на несуществующий шаблон «{tpl_key}»")
            if action.get("config", {}).get("scenario") and not spec.get("scenario"):
                raise PresetError("действие ссылается на сценарий, которого нет в пресете")


def list_preset_specs() -> list[dict]:
    """Every *.json under presets/, parsed and validated. An invalid file
    is skipped with a warning rather than crashing whatever screen lists
    presets — one bad file shouldn't take the wizard down, same reasoning
    as a broken direction/attachment elsewhere in this app."""
    out = []
    d = presets_dir()
    if not d.exists():
        return out
    for path in sorted(d.glob("*.json")):
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
            validate(spec)
        except (json.JSONDecodeError, PresetError, OSError) as e:
            _logger.warning("пресет %s пропущен: %s", path.name, e)
            continue
        out.append(spec)
    return out


def find_spec(key: str) -> dict | None:
    for spec in list_preset_specs():
        if spec["key"] == key:
            return spec
    return None


def default_answers(spec: dict, db: Database) -> dict:
    """What the wizard should pre-fill — the preset's own defaults, plus
    a live read of the directions catalogue for a "directions" variable,
    since a static default can't know what the user has set up."""
    answers = {}
    for v in spec["variables"]:
        if v["type"] == "directions":
            answers[v["name"]] = [dict(d) for d in db.list_directions(enabled_only=True)]
        else:
            answers[v["name"]] = v.get("default", "")
    return answers


def _sub(value, subs: dict):
    """Recursive {{var}} substitution — over template/question text and
    over trigger config values alike (after_hours' time_window.start
    references {{work_hours_start}}), so one helper covers both."""
    if isinstance(value, str):
        out = value
        for k, v in subs.items():
            out = out.replace("{{" + k + "}}", str(v))
        return out
    if isinstance(value, dict):
        return {k: _sub(v, subs) for k, v in value.items()}
    if isinstance(value, list):
        return [_sub(v, subs) for v in value]
    return value


def _text_subs(spec: dict, answers: dict) -> dict:
    """{{var}} only ever needs to become a string — a "directions" answer
    is a list of direction rows, spliced in as a joined, human-readable
    list of names."""
    subs = {}
    for v in spec["variables"]:
        val = answers.get(v["name"], v.get("default", ""))
        subs[v["name"]] = ", ".join(d["name"] for d in val) if v["type"] == "directions" else val
    return subs


def _direction_field(directions: list[dict], field: str) -> list[str]:
    """Union of one field (keywords/stop_words) across the chosen
    directions, deduplicated — chat_hunter's trigger config asks for this
    via a {"__from_directions__": "keywords"} marker rather than typing
    the same words into both the direction and the rule. Both fields are
    always a JSON-list string on a direction row (db/schema.py)."""
    out: list[str] = []
    for d in directions:
        for word in json.loads(d[field]):
            if word not in out:
                out.append(word)
    return out


def _resolve_directions_markers(value, directions: list[dict]):
    if isinstance(value, dict):
        marker = value.get("__from_directions__")
        if marker:
            return _direction_field(directions, marker)
        return {k: _resolve_directions_markers(v, directions) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_directions_markers(v, directions) for v in value]
    return value


def install(db: Database, bot_id: int, spec: dict, answers: dict) -> int | None:
    """Applies a validated preset to a bot. `answers` is keyed by
    variable name, same shape as default_answers() returns — the wizard
    UI fills it in from what the user actually picked. Returns the new
    scenario's id, or None if the preset doesn't have one (chat_hunter).
    """
    validate(spec)
    directions = answers.get("directions") or []
    subs = _text_subs(spec, answers)

    scenario_id = None
    if spec.get("scenario"):
        sc = spec["scenario"]
        steps = []
        for raw_step in sc["steps"]:
            step = dict(raw_step)
            if step.pop("options_from", None) == "directions":
                # Every option converges on the same next question (no
                # "next" set) — the direction only labels the choice, it
                # doesn't fork the conversation; see PLAN.md С5 journal.
                step["options"] = [{"label": d["name"], "next": None} for d in directions]
            step["question"] = _sub(step.get("question", ""), subs)
            steps.append(step)
        scenario_id = db.add_scenario(bot_id, _sub(sc["name"], subs), steps)
        kind = "branching" if any(s.get("options") for s in steps) else "linear"
        db.update_scenario(scenario_id, kind=kind)

    template_ids: dict[str, int] = {}
    for tpl in spec["templates"]:
        tid = db.add_template(
            bot_id, _sub(tpl["name"], subs), _sub(tpl["text"], subs), tpl.get("variables", []))
        template_ids[tpl["key"]] = tid
        if tpl.get("done") and scenario_id is not None:
            db.update_scenario(scenario_id, done_template_id=tid)

    for i, trig in enumerate(spec["triggers"]):
        cfg = _resolve_directions_markers(trig.get("config", {}), directions)
        cfg = _sub(cfg, subs)
        trigger_id = db.add_trigger(bot_id, trig["type"], cfg)
        for j, action in enumerate(trig.get("actions", [])):
            acfg = dict(action.get("config", {}))
            if acfg.pop("scenario", None):
                acfg["scenario_id"] = scenario_id
            tpl_key = acfg.pop("template", None)
            if tpl_key:
                acfg["template_id"] = template_ids.get(tpl_key)
            acfg = _sub(acfg, subs)
            db.add_action(trigger_id, action["type"], acfg, order_index=j)

    return scenario_id
