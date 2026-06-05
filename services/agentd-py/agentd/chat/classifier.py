from __future__ import annotations

import logging
from typing import Any

from agentd.chat.models import IntentClassification, IntentType

logger = logging.getLogger(__name__)

_CLASSIFY_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["qa", "small_change", "large_change", "resume", "clarify"]},
        "rationale": {"type": "string"},
        "likely_targets": {"type": "array", "items": {"type": "string"}},
        "answer": {"type": ["string", "null"]},
        "clarify_question": {"type": ["string", "null"]},
    },
    "required": ["intent", "rationale", "likely_targets", "answer", "clarify_question"],
}

_SYSTEM_PROMPT = """\
You are classifying a user's chat message and, for qa intent, answering it in one step.

Intent values:
  qa           — question or discussion, no file changes needed
  small_change — 1-2 files, localised edit, no interface or schema changes
  large_change — 3+ files, interface/schema changes, new files, or ambiguous scope
  resume       — user wants to continue/retry a recently failed task
  clarify      — user message is ambiguous between resume and something else; ask a question

You receive:
  conversation_history — recent messages; use to resolve "fix that", "also update tests", etc.
  explore_context      — files already read and search results gathered from the workspace
  graph_blast_radius   — workspace files structurally connected (imports/calls/references) to the
                         files in explore_context. These are files a change to the explored files
                         would likely RIPPLE INTO, even though they weren't read. A wide blast
                         radius spanning multiple modules is a strong large_change signal.
  recent_task          — if present: {task_id, status, goal, messages_since} for the most recent failed task in this thread

Rules:
- Judge scope from BOTH explore_context AND graph_blast_radius. The explore phase may only read
  1-2 files, but if graph_blast_radius shows the change ripples into many files across different
  modules (e.g. domain/, api/, orchestrator/ as well as the files explored), it is large_change.
  A change is small_change ONLY when both the explored files AND their blast radius are localised
  (1-2 files, one module, no interface/schema change). Be conservative — prefer large_change when
  scope is unclear or the blast radius is wide.
- resume: only choose if recent_task is provided AND the user clearly refers to continuing that specific task (e.g. "continue", "retry", "resume", "keep going") AND the conversation has not moved to a different topic. If recent_task is null, never choose resume.
- clarify: choose when the message could mean resume OR something else and context does not resolve the ambiguity. Populate clarify_question with a short, specific question.
- If intent is "qa": populate "answer" with a complete, concise response. Name files and functions explicitly.
- If intent is "clarify": populate "clarify_question". Set "answer" to null.
- If intent is "small_change", "large_change", or "resume": set both "answer" and "clarify_question" to null.
"""


class IntentClassifier:
    def __init__(self, *, transport: Any, model: str) -> None:
        self._transport = transport
        self._model = model

    async def classify(
        self,
        message: str,
        context: list[dict[str, Any]],
        history: list[dict[str, str]],
        recent_task: dict[str, Any] | None = None,
        graph_blast_radius: list[str] | None = None,
    ) -> IntentClassification:
        if message.strip().startswith("/plan"):
            logger.info("[classify] /plan prefix — forcing large_change")
            return IntentClassification(
                intent=IntentType.LARGE_CHANGE,
                rationale="/plan prefix — forced large_change routing",
            )
        logger.info(
            "[classify] calling LLM: model=%s msg_chars=%d context_entries=%d history_turns=%d recent_task=%s",
            self._model, len(message), len(context), len(history),
            recent_task["task_id"] if recent_task else None,
        )
        try:
            result = await self._transport.generate_json(
                model=self._model,
                schema_name="intent_classification",
                schema=_CLASSIFY_SCHEMA,
                system_instructions=_SYSTEM_PROMPT,
                user_payload={
                    "message": message,
                    "conversation_history": history[-10:],
                    "explore_context": context,
                    "graph_blast_radius": graph_blast_radius or [],
                    "recent_task": recent_task,
                },
            )
            classification = IntentClassification(
                intent=IntentType(result["intent"]),
                rationale=result.get("rationale", ""),
                likely_targets=result.get("likely_targets", []),
                answer=result.get("answer") or None,
                clarify_question=result.get("clarify_question") or None,
            )
            logger.info(
                "[classify] result: intent=%s targets=%s has_answer=%s rationale=%.120s",
                classification.intent,
                classification.likely_targets,
                classification.answer is not None,
                classification.rationale or "",
            )
            return classification
        except Exception:
            logger.exception("Intent classification failed — defaulting to large_change")
            return IntentClassification(
                intent=IntentType.LARGE_CHANGE,
                rationale="classification error — safe default",
            )
