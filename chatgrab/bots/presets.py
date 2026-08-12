"""Preset bundles: default scenario, templates, and trigger→action rules
seeded onto a newly created bot. A preset is a starting point, not a fixed
mode — everything it creates is ordinary rows the user can edit or delete
afterward through the rules/scenario editors."""
from __future__ import annotations

from ..db.database import Database

B2B_SCENARIO_STEPS = [
    {"question": "Здравствуйте! Расскажите, пожалуйста, из какой вы компании?",
     "field": "company", "validation": "text"},
    {"question": "Какой бюджет вы рассматриваете для этой задачи?",
     "field": "budget", "validation": "text"},
    {"question": "Как вас зовут и какая у вас роль в компании?",
     "field": "contact_role", "validation": "text"},
    {"question": "Опишите, пожалуйста, что именно вам нужно.",
     "field": "request", "validation": "text"},
]

B2C_SCENARIO_STEPS = [
    {"question": "Здравствуйте! Что вас интересует?",
     "field": "interest", "validation": "text"},
    {"question": "Как с вами удобнее связаться — телефон или продолжим здесь?",
     "field": "contact_method", "validation": "text"},
]

B2B_TEMPLATES = [
    {"name": "Приветствие B2B", "text": "Здравствуйте, {name}! Спасибо за обращение — уточним пару деталей, чтобы передать заявку нужному менеджеру.", "variables": ["name"]},
    {"name": "Заявка принята B2B", "text": "Спасибо! Заявка от {company} передана менеджеру, скоро с вами свяжутся.", "variables": ["company"]},
]

B2C_TEMPLATES = [
    {"name": "Приветствие B2C", "text": "Привет! Чем можем помочь?", "variables": []},
    {"name": "Заявка принята B2C", "text": "Спасибо за обращение! Мы скоро ответим.", "variables": []},
]

PRESETS = {
    "b2b": {"label": "B2B", "scenario_name": "B2B-квалификация", "steps": B2B_SCENARIO_STEPS, "templates": B2B_TEMPLATES},
    "b2c": {"label": "B2C", "scenario_name": "B2C-приём обращений", "steps": B2C_SCENARIO_STEPS, "templates": B2C_TEMPLATES},
    "custom": {"label": "Кастом", "scenario_name": "Новый сценарий", "steps": [], "templates": []},
}


def apply_preset(db: Database, bot_id: int, preset_name: str) -> None:
    """Seed a scenario + templates + the "incoming DM -> run scenario ->
    save lead -> notify manager" rule for a freshly created bot. Safe to
    call once, right after add_bot(); editing/removing what it creates is
    ordinary rules/scenario editor work afterward."""
    preset = PRESETS.get(preset_name, PRESETS["custom"])

    scenario_id = db.add_scenario(bot_id, preset["scenario_name"], preset["steps"])
    for tpl in preset["templates"]:
        db.add_template(bot_id, tpl["name"], tpl["text"], tpl["variables"])

    trigger_id = db.add_trigger(bot_id, "incoming_dm", {})
    if preset["steps"]:
        # The scenario owns the conversation through to lead creation —
        # continue_scenario() saves the lead and notifies the manager once
        # all steps are answered. Chaining save_lead/notify_manager here
        # too would file a second, premature lead from just the opening
        # message, before the scripted questions even ran.
        db.add_action(trigger_id, "run_scenario", {"scenario_id": scenario_id}, order_index=0)
    else:
        db.add_action(trigger_id, "save_lead", {}, order_index=0)
        db.add_action(trigger_id, "notify_manager", {}, order_index=1)
