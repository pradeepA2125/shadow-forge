"""Prompt + schema for the task-narrative synthesis (summarize_run)."""
from __future__ import annotations

TASK_NARRATIVE_RESPONSE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "One line: what the task did."},
        "points": {
            "type": "array", "items": {"type": "string"},
            "description": "3-6 short bullet points: what changed / what happened, in order.",
        },
    },
    "required": ["headline", "points"],
}

_SYSTEM = (
    "You write a short, factual narrative of an automated coding task's run for a human "
    "reviewer and for re-use as context in the next chat turn. Be concrete and specific; "
    "name files and what changed. If the run failed or was aborted, say what was attempted "
    "and where it stopped. If there were replans, mention the course-correction briefly."
)


def build_narrative_payload(
    *, goal: str, outcome: str, run_events: list[dict[str, object]],
    deviations: list[str], modified_files: list[str],
) -> dict[str, object]:
    return {
        "goal": goal,
        "outcome": outcome,
        "run_events": run_events,
        "deviations": deviations,
        "modified_files": modified_files,
    }


def format_narrative_system_prompt() -> str:
    return _SYSTEM
