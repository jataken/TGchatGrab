"""Declarative scripted-dialog runner: a scenario is a flat list of steps
(question, field to save the answer under, validation type), walked one
step per incoming message. State is persisted to bot_scenario_sessions
after every step, so a restart mid-conversation resumes exactly where it
left off instead of losing the contact's answers so far."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..db.database import Database

_PHONE_RE = re.compile(r"[\d+][\d\s\-()]{5,}\d")


@dataclass
class StepResult:
    done: bool
    question: str | None = None
    error: str | None = None
    answers: dict | None = None


def _validate(validation: str, text: str) -> str | None:
    """Returns an error message, or None if the answer is acceptable."""
    text = text.strip()
    if not text:
        return "Ответ не может быть пустым — попробуйте ещё раз."
    if validation == "phone" and not _PHONE_RE.search(text):
        return "Не похоже на номер телефона — укажите его цифрами."
    if validation == "number" and not text.replace(" ", "").replace(",", ".").lstrip("-").replace(".", "", 1).isdigit():
        return "Ожидалось число."
    return None


class ScenarioEngine:
    def __init__(self, db: Database):
        self.db = db

    def _steps(self, scenario_row) -> list[dict]:
        return json.loads(scenario_row["steps"])

    def start(self, bot_id: int, scenario_id: int, contact_telegram_id: int) -> StepResult:
        scenario = self.db.get_scenario(scenario_id)
        steps = self._steps(scenario) if scenario else []
        if not steps:
            return StepResult(done=True, answers={})
        self.db.start_scenario_session(bot_id, scenario_id, contact_telegram_id)
        return StepResult(done=False, question=steps[0]["question"])

    def resume_question(self, bot_id: int, contact_telegram_id: int) -> str | None:
        """The question for whatever step an in-flight session is currently
        on — used to re-show it if a contact's answer failed validation, or
        to display "where they are" in a test-mode dry run."""
        session = self.db.get_active_scenario_session(bot_id, contact_telegram_id)
        if not session:
            return None
        scenario = self.db.get_scenario(session["scenario_id"])
        steps = self._steps(scenario) if scenario else []
        if session["step_index"] >= len(steps):
            return None
        return steps[session["step_index"]]["question"]

    def submit_answer(self, bot_id: int, contact_telegram_id: int, text: str) -> StepResult:
        session = self.db.get_active_scenario_session(bot_id, contact_telegram_id)
        if not session:
            return StepResult(done=True, error="Нет активного сценария.")
        scenario = self.db.get_scenario(session["scenario_id"])
        steps = self._steps(scenario) if scenario else []
        idx = session["step_index"]
        if idx >= len(steps):
            self.db.update_scenario_session(session["id"], status="done")
            return StepResult(done=True, answers=json.loads(session["answers"]))

        step = steps[idx]
        error = _validate(step.get("validation", "text"), text)
        if error:
            return StepResult(done=False, question=step["question"], error=error)

        answers = json.loads(session["answers"])
        answers[step["field"]] = text.strip()
        next_idx = idx + 1

        if next_idx >= len(steps):
            self.db.update_scenario_session(session["id"], step_index=next_idx, answers=answers, status="done")
            return StepResult(done=True, answers=answers)

        self.db.update_scenario_session(session["id"], step_index=next_idx, answers=answers)
        return StepResult(done=False, question=steps[next_idx]["question"], answers=answers)

    def dry_run(self, scenario_id: int, sample_answers: list[str]) -> list[dict]:
        """Test-mode helper: walk a scenario with canned answers, entirely
        in memory (no session row written, nothing sent), returning the
        question/answer trail for display."""
        scenario = self.db.get_scenario(scenario_id)
        steps = self._steps(scenario) if scenario else []
        trail = []
        answers: dict = {}
        for i, step in enumerate(steps):
            trail.append({"question": step["question"], "field": step["field"]})
            if i < len(sample_answers):
                error = _validate(step.get("validation", "text"), sample_answers[i])
                trail[-1]["answer"] = sample_answers[i]
                trail[-1]["error"] = error
                if not error:
                    answers[step["field"]] = sample_answers[i]
        trail.append({"final_answers": answers})
        return trail
