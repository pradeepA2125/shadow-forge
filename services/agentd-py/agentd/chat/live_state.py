"""Resolve a chat thread's current actionable state from its active task.

The chat thread is the durable anchor; its active task id churns (resume creates
a new child id). This pure resolver maps the active task's status to the single
gate it's waiting on plus the current actionable plan, so the UI can render
entirely from state (no in-memory session, no reliance on transient SSE).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel

from agentd.chat.models import PendingGate, ThreadLiveState
from agentd.domain.models import TaskRecord

logger = logging.getLogger(__name__)

_GateKind = Literal["command", "step", "scope", "validation"]

# status -> (gate kind, the execution_state field holding its payload)
_GATE_FIELD: dict[str, tuple[_GateKind, str]] = {
    "AWAITING_COMMAND_DECISION": ("command", "pending_command_request"),
    "AWAITING_STEP_REVIEW": ("step", "pending_step_review"),
    "AWAITING_SCOPE_DECISION": ("scope", "pending_scope_request"),
    "AWAITING_VALIDATION_DECISION": ("validation", "pending_validation"),
}


def _payload(raw: object) -> dict:
    """Normalize a pending_* field (Pydantic model or dict) to a plain dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, BaseModel):
        return raw.model_dump(mode="json")
    return {}


# Task-status -> history-list chip. Mirrors the mockup's Running/Review/Done
# chips (chat-ui-hifi.html frame 1); failed/aborted get a "failed" chip the
# mockup omits.
_CHIP_RUNNING = frozenset({
    "QUEUED", "CONTEXT_READY", "PLANNED", "EXECUTING",
    "VALIDATING", "REPAIRING", "VALIDATED", "PROMOTING",
})
_CHIP_REVIEW = frozenset({
    "AWAITING_PLAN_APPROVAL", "AWAITING_STEP_REVIEW",
    "AWAITING_COMMAND_DECISION", "AWAITING_SCOPE_DECISION",
    "AWAITING_VALIDATION_DECISION", "READY_FOR_REVIEW",
})


def thread_status_chip(status: str | None) -> str | None:
    """Map a task status to the history-list chip, or None for no chip."""
    if status in _CHIP_RUNNING:
        return "running"
    if status in _CHIP_REVIEW:
        return "review"
    if status == "SUCCEEDED":
        return "done"
    if status in ("FAILED", "ABORTED"):
        return "failed"
    return None


def resolve_live_state(
    active_task_id: str | None,
    get_task: Callable[[str], TaskRecord],
) -> ThreadLiveState:
    """Build the ThreadLiveState for a thread given its active task id.

    `get_task` raises KeyError for an unknown/missing task; that is treated as
    "no active task" (the task was pruned) rather than an error.
    """
    if not active_task_id:
        return ThreadLiveState()
    try:
        task = get_task(active_task_id)
    except KeyError:
        return ThreadLiveState()

    status = str(task.status)
    gate: PendingGate | None = None
    if status in _GATE_FIELD:
        kind, field = _GATE_FIELD[status]
        payload = _payload(getattr(task.execution_state, field, None))
        if payload:
            gate = PendingGate(kind=kind, payload=payload)
        else:
            # Tripwire + defense: status says we're at a gate but its payload is
            # missing/empty — a persistence inconsistency (e.g. a stale save clobbered
            # pending_X). Do NOT render a broken/empty card; surface no gate so the UI
            # stays clean and the next poll reconciles once state is consistent.
            # Logged so the clobber can be caught and root-caused in the wild.
            logger.warning(
                "live_state inconsistency: task=%s status=%s but %s payload is empty "
                "— suppressing %s card",
                task.task_id, status, field, kind,
            )

    plan: dict | None = None
    if status == "AWAITING_PLAN_APPROVAL" and task.plan_markdown:
        plan = {"task_id": task.task_id, "plan_markdown": task.plan_markdown}

    return ThreadLiveState(
        active_task_id=task.task_id,
        status=status,
        pending_gate=gate,
        plan=plan,
        # failure_summary only makes sense once the task has failed/aborted; run_summary
        # surfaces whenever the engine has finalized it (terminal states).
        failure_summary=task.failure_summary if status in ("FAILED", "ABORTED") else None,
        run_summary=task.run_summary,
        task_narrative=task.task_narrative,
    )
