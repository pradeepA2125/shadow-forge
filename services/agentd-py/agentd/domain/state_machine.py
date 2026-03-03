from __future__ import annotations

from datetime import datetime, timezone

from .models import TaskEvent, TaskRecord, TaskStatus


_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.QUEUED: {TaskStatus.CONTEXT_READY, TaskStatus.FAILED, TaskStatus.ABORTED},
    TaskStatus.CONTEXT_READY: {TaskStatus.PLANNED, TaskStatus.FAILED, TaskStatus.ABORTED},
    TaskStatus.PLANNED: {TaskStatus.PATCHED, TaskStatus.FAILED, TaskStatus.ABORTED},
    TaskStatus.PATCHED: {TaskStatus.VALIDATING, TaskStatus.FAILED, TaskStatus.ABORTED},
    TaskStatus.VALIDATING: {
        TaskStatus.READY_FOR_REVIEW,
        TaskStatus.REPAIRING,
        TaskStatus.FAILED,
        TaskStatus.ABORTED,
    },
    TaskStatus.REPAIRING: {TaskStatus.PATCHED, TaskStatus.FAILED, TaskStatus.ABORTED},
    TaskStatus.READY_FOR_REVIEW: {TaskStatus.PROMOTING, TaskStatus.ABORTED, TaskStatus.FAILED},
    TaskStatus.PROMOTING: {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.ABORTED},
    TaskStatus.SUCCEEDED: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.ABORTED: set(),
}


def can_transition(from_status: TaskStatus, to_status: TaskStatus) -> bool:
    return to_status in _TRANSITIONS[from_status]


def transition(task: TaskRecord, to_status: TaskStatus, reason: str) -> TaskRecord:
    if not can_transition(task.status, to_status):
        msg = f"Invalid transition: {task.status} -> {to_status}"
        raise ValueError(msg)

    from_status = task.status
    now = datetime.now(timezone.utc)
    task.status = to_status
    task.updated_at = now
    task.events.append(
        TaskEvent(
            at=now,
            from_status=from_status,
            to_status=to_status,
            reason=reason,
        )
    )
    return task


def bump_usage(task: TaskRecord, tokens_used: int = 0) -> TaskRecord:
    task.usage.iterations += 1
    task.usage.tokens_used += tokens_used
    task.updated_at = datetime.now(timezone.utc)
    return task


def assert_budget(task: TaskRecord, started_at_ms: int, now_ms: int) -> None:
    if task.usage.iterations > task.budget.max_iterations:
        raise RuntimeError("Iteration budget exceeded")

    if task.usage.tokens_used > task.budget.max_tokens:
        raise RuntimeError("Token budget exceeded")

    if now_ms - started_at_ms > task.budget.max_runtime_ms:
        raise RuntimeError("Runtime budget exceeded")

    if len(task.modified_files) > task.budget.max_files_touched:
        raise RuntimeError("Modified file budget exceeded")
