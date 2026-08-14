"""Declarative scripted-dialog runner.

Два вида сценариев живут рядом, а не сменяют друг друга:

- `linear` — плоский список шагов, один шаг за сообщение. Ровно то, что
  работало раньше; ни одна уже настроенная анкета не меняет поведения.
- `branching` — тот же список шагов, но у шага есть варианты ответа, и
  каждый вариант называет следующий шаг по его `id`. Ветка может
  закончить сценарий досрочно (`__end__`).

Почему по id, а не по номеру: вставка шага в середину живого сценария
иначе молча перенаправила бы все существующие переходы. Идентификаторы
шагам раздаются с самого начала (см. db.database._with_step_ids), так что
переход всегда указывает на тот шаг, который выбрал автор.

Состояние пишется в bot_scenario_sessions после каждого шага: перезапуск
приложения посреди разговора продолжает с того же места, а не теряет уже
собранные ответы. Ветвящаяся сессия хранит ещё и id текущего шага —
номер позиции для неё ничего не значит.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..db.database import Database

_PHONE_RE = re.compile(r"[\d+][\d\s\-()]{5,}\d")

LINEAR = "linear"
BRANCHING = "branching"
END = "__end__"


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


def _norm(text: str) -> str:
    return " ".join((text or "").split()).strip().lower().replace("ё", "е")


def options_of(step: dict) -> list[dict]:
    """Варианты ответа шага, без мусора: вариант без подписи никому не
    показать, а значит и выбрать его нельзя."""
    return [o for o in (step.get("options") or [])
            if isinstance(o, dict) and str(o.get("label", "")).strip()]


def format_question(step: dict) -> str:
    """Вопрос с вариантами, пронумерованными для ответа.

    Отвечают текстом, а не нажатием: юзербот пишет из обычного аккаунта,
    кнопок у него нет. Номер рядом с подписью — чтобы не пришлось
    перепечатывать «Косметическое сырьё, малотоннаж».
    """
    question = step.get("question", "")
    options = options_of(step)
    if not options:
        return question
    lines = [question]
    for i, option in enumerate(options, 1):
        lines.append(f"{i}. {option['label']}")
    return "\n".join(lines)


def match_option(step: dict, text: str) -> dict | None:
    """Какой вариант выбрал контакт: по номеру или по тексту подписи."""
    options = options_of(step)
    if not options:
        return None
    answer = _norm(text)
    if answer.rstrip(".").isdigit():
        idx = int(answer.rstrip(".")) - 1
        if 0 <= idx < len(options):
            return options[idx]
    for option in options:
        if _norm(option["label"]) == answer:
            return option
    # Подпись целиком внутри ответа («хочу второе — упаковку»): полезно,
    # но только когда подходит ровно один вариант, иначе выбор угадан.
    partial = [o for o in options if _norm(o["label"]) and _norm(o["label"]) in answer]
    return partial[0] if len(partial) == 1 else None


class ScenarioEngine:
    def __init__(self, db: Database):
        self.db = db

    def _steps(self, scenario_row) -> list[dict]:
        return json.loads(scenario_row["steps"])

    @staticmethod
    def _kind(scenario_row) -> str:
        if scenario_row is None:
            return LINEAR
        keys = scenario_row.keys() if hasattr(scenario_row, "keys") else scenario_row
        return (scenario_row["kind"] if "kind" in keys else None) or LINEAR

    @staticmethod
    def _find(steps: list[dict], step_id: str | None) -> tuple[int, dict | None]:
        for i, step in enumerate(steps):
            if step.get("id") == step_id:
                return i, step
        return -1, None

    # ---- start ---------------------------------------------------------
    def start(self, bot_id: int, scenario_id: int, contact_telegram_id: int) -> StepResult:
        scenario = self.db.get_scenario(scenario_id)
        steps = self._steps(scenario) if scenario else []
        if not steps:
            return StepResult(done=True, answers={})
        first = steps[0]
        self.db.start_scenario_session(bot_id, scenario_id, contact_telegram_id,
                                        step_id=first.get("id"))
        return StepResult(done=False, question=format_question(first))

    def resume_question(self, bot_id: int, contact_telegram_id: int) -> str | None:
        """The question for whatever step an in-flight session is currently
        on — used to re-show it if a contact's answer failed validation, or
        to display "where they are" in a test-mode dry run."""
        session = self.db.get_active_scenario_session(bot_id, contact_telegram_id)
        if not session:
            return None
        scenario = self.db.get_scenario(session["scenario_id"])
        steps = self._steps(scenario) if scenario else []
        step = self._current_step(session, scenario, steps)
        return format_question(step) if step else None

    def _current_step(self, session, scenario, steps: list[dict]) -> dict | None:
        if self._kind(scenario) == BRANCHING:
            keys = session.keys() if hasattr(session, "keys") else session
            step_id = session["step_id"] if "step_id" in keys else None
            _, step = self._find(steps, step_id)
            if step is not None:
                return step
            # Сценарий перевели в ветвящийся посреди разговора: продолжаем
            # с той же позиции, а не роняем сессию.
        idx = session["step_index"]
        return steps[idx] if 0 <= idx < len(steps) else None

    # ---- answer --------------------------------------------------------
    def submit_answer(self, bot_id: int, contact_telegram_id: int, text: str) -> StepResult:
        session = self.db.get_active_scenario_session(bot_id, contact_telegram_id)
        if not session:
            return StepResult(done=True, error="Нет активного сценария.")
        scenario = self.db.get_scenario(session["scenario_id"])
        steps = self._steps(scenario) if scenario else []
        step = self._current_step(session, scenario, steps)
        if step is None:
            self.db.update_scenario_session(session["id"], status="done")
            return StepResult(done=True, answers=json.loads(session["answers"]))

        options = options_of(step)
        chosen = match_option(step, text) if options else None
        if options and chosen is None:
            return StepResult(
                done=False, question=format_question(step),
                error="Не понял выбор — ответьте номером варианта или его словами.")
        if not options:
            error = _validate(step.get("validation", "text"), text)
            if error:
                return StepResult(done=False, question=format_question(step), error=error)

        answers = json.loads(session["answers"])
        answers[step["field"]] = chosen["label"] if chosen else text.strip()

        next_step, next_index = self._next(steps, step, chosen)
        if next_step is None:
            self.db.update_scenario_session(
                session["id"], step_index=len(steps), step_id=None,
                answers=answers, status="done")
            return StepResult(done=True, answers=answers)

        self.db.update_scenario_session(
            session["id"], step_index=next_index, step_id=next_step.get("id"), answers=answers)
        return StepResult(done=False, question=format_question(next_step), answers=answers)

    def _next(self, steps: list[dict], step: dict, chosen: dict | None) -> tuple[dict | None, int]:
        """Куда идти дальше: сначала вариант, потом собственный переход
        шага, потом просто следующий по списку.

        Порядок именно такой, потому что каждый следующий пункт — более
        общее правило: конкретный ответ важнее умолчания шага, умолчание
        шага важнее порядка в списке.
        """
        target = None
        if chosen is not None:
            target = chosen.get("next")
        if target is None:
            target = step.get("next")
        if target == END:
            return None, len(steps)
        if target:
            idx, found = self._find(steps, target)
            if found is not None:
                return found, idx
            # Переход указывает на удалённый шаг: закончить разговор
            # честнее, чем молча продолжить не туда.
            return None, len(steps)
        idx, _ = self._find(steps, step.get("id"))
        if idx < 0:
            return None, len(steps)
        nxt = idx + 1
        return (steps[nxt], nxt) if nxt < len(steps) else (None, len(steps))

    # ---- test mode -----------------------------------------------------
    def dry_run(self, scenario_id: int, sample_answers: list[str]) -> list[dict]:
        """Test-mode helper: walk a scenario with canned answers, entirely
        in memory (no session row written, nothing sent), returning the
        question/answer trail for display. Ветвление проходится по тем же
        правилам, что и вживую, — иначе проверка показывала бы не тот
        путь, по которому пойдёт настоящий разговор."""
        scenario = self.db.get_scenario(scenario_id)
        steps = self._steps(scenario) if scenario else []
        trail: list[dict] = []
        answers: dict = {}
        step = steps[0] if steps else None
        seen: set[str] = set()
        for answer in list(sample_answers) + [None] * (len(steps) - len(sample_answers)):
            if step is None:
                break
            entry = {"question": format_question(step), "field": step["field"]}
            trail.append(entry)
            if answer is None:
                break
            entry["answer"] = answer
            options = options_of(step)
            chosen = match_option(step, answer) if options else None
            if options and chosen is None:
                entry["error"] = "Не понял выбор — ответьте номером варианта или его словами."
                break
            if not options:
                error = _validate(step.get("validation", "text"), answer)
                entry["error"] = error
                if error:
                    break
            answers[step["field"]] = chosen["label"] if chosen else answer
            # Защита от цикла: автор может замкнуть ветку саму на себя, и
            # проверка не должна из-за этого зависнуть.
            if step.get("id") in seen:
                break
            seen.add(step.get("id"))
            step, _ = self._next(steps, step, chosen)
        trail.append({"final_answers": answers})
        return trail
