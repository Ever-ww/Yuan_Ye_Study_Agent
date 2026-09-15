"""LLM prompt and validation contract for recursive visible Observer state."""

from __future__ import annotations

import json

from Agent.observer import ObserverState, VisibleObserverEvent
from gateway.event_store import canonical_json


_SYSTEM_PROMPT = """You update a small Observer state from one new user-visible event.

Return exactly one JSON object with this schema and no Markdown:
{
  "user_problem": "",
  "completed_tasks": [],
  "in_progress_task": "",
  "current_agent_action": "",
  "intent_alignment": {"status": "aligned|uncertain|drifted", "reason": ""}
}

Rules:
- Use only previous_state and visible_event. Never infer hidden reasoning, prompts,
  tool arguments, tool results, credentials, or facts absent from visible input.
- Recursively update the prior state; completed_tasks may be corrected or removed.
- Keep at most 20 concise completed tasks, one current in-progress task, and one
  current action. Keep every string under 240 characters.
- Preserve user_problem exactly.
- If terminal is false, alignment is provisional and cannot trigger user correction.
- If terminal is true, evaluate whether the final visible result actually addresses
  user_problem. Use drifted only for a clear mismatch and uncertain when evidence is
  insufficient or the run did not complete normally.
"""


class RecursiveVisibleObserver:
    plugin_id = "builtin.observer"
    plugin_version = "1"
    state_schema_version = 1

    def initial_state(self, user_problem: str) -> ObserverState:
        return ObserverState(
            user_problem=user_problem.strip(),
            in_progress_task="理解用户问题",
            current_agent_action="准备执行",
        )

    def model_messages(
        self,
        previous: ObserverState,
        event: VisibleObserverEvent,
        *,
        terminal: bool,
    ) -> tuple[dict[str, str], ...]:
        return (
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": canonical_json({
                "previous_state": previous.model_dump(mode="json"),
                "visible_event": event.model_dump(mode="json"),
                "terminal": terminal,
            })},
        )

    def parse_state(self, raw: str, previous: ObserverState) -> ObserverState:
        text = raw.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if len(lines) >= 3 and lines[-1].strip() == "```":
                text = "\n".join(lines[1:-1]).strip()
                if text.startswith("json"):
                    text = text[4:].lstrip()
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("Observer model output must be a JSON object")
        raw_completed = value.get("completed_tasks", [])
        if not isinstance(raw_completed, list):
            raise ValueError("Observer completed_tasks must be a JSON array")
        raw_alignment = value.get("intent_alignment", {})
        if not isinstance(raw_alignment, dict):
            raise ValueError("Observer intent_alignment must be a JSON object")
        normalized = {
            # The model may summarize state, but it may not rewrite the user's task.
            "user_problem": previous.user_problem,
            "completed_tasks": tuple(
                _short(str(item)) for item in raw_completed[-20:] if str(item).strip()
            ),
            "in_progress_task": _short(str(value.get("in_progress_task", ""))),
            "current_agent_action": _short(str(value.get("current_agent_action", ""))),
            "intent_alignment": {
                "status": str(raw_alignment.get("status", "uncertain")),
                "reason": _short(str(raw_alignment.get("reason", ""))),
            },
        }
        # Strict JSON validation accepts the wire enum/list representation while
        # still rejecting extra fields and invalid status values.
        return ObserverState.model_validate_json(canonical_json(normalized), strict=True)

    def migrate_state(
        self, state: dict[str, object], from_schema_version: int,
    ) -> ObserverState:
        if from_schema_version != self.state_schema_version:
            raise ValueError(
                f"unsupported Observer state migration: {from_schema_version}"
            )
        return ObserverState.model_validate(state, strict=True)


def create_observer_plugin() -> RecursiveVisibleObserver:
    return RecursiveVisibleObserver()


def _short(value: str, limit: int = 240) -> str:
    normalized = " ".join(value.split())
    return normalized if len(normalized) <= limit else normalized[: limit - 1] + "…"
