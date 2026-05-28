import pytest

from agentd.domain.models import TaskEvent, TaskRecord, TaskStatus
from agentd.domain.state_machine import can_transition, transition


def test_valid_transition_path() -> None:
    task = TaskRecord(task_id="t1", goal="goal", workspace_path=".")
    assert task.status == TaskStatus.QUEUED
    assert can_transition(TaskStatus.QUEUED, TaskStatus.CONTEXT_READY)
    assert can_transition(TaskStatus.READY_FOR_REVIEW, TaskStatus.PROMOTING)
    assert can_transition(TaskStatus.PLANNED, TaskStatus.EXECUTING)
    assert can_transition(TaskStatus.VALIDATING, TaskStatus.VALIDATED)


def test_invalid_direct_review_transitions() -> None:
    assert not can_transition(TaskStatus.PLANNED, TaskStatus.READY_FOR_REVIEW)
    assert not can_transition(TaskStatus.EXECUTING, TaskStatus.READY_FOR_REVIEW)
    assert not can_transition(TaskStatus.REPAIRING, TaskStatus.READY_FOR_REVIEW)


def test_task_record_normalizes_legacy_patched_status() -> None:
    task = TaskRecord.model_validate(
        {
            "task_id": "legacy",
            "goal": "goal",
            "workspace_path": ".",
            "status": "PATCHED",
        }
    )
    assert task.status == TaskStatus.EXECUTING


def test_task_event_normalizes_legacy_patched_statuses() -> None:
    event = TaskEvent.model_validate(
        {
            "at": "2026-03-20T00:00:00+00:00",
            "from_status": "PATCHED",
            "to_status": "PATCHED",
            "reason": "legacy",
        }
    )
    assert event.from_status == TaskStatus.EXECUTING
    assert event.to_status == TaskStatus.EXECUTING


def test_transition_requires_validated_before_review() -> None:
    task = TaskRecord(task_id="t2", goal="goal", workspace_path=".")
    task = transition(task, TaskStatus.CONTEXT_READY, "context")
    task = transition(task, TaskStatus.AWAITING_PLAN_APPROVAL, "awaiting")
    task = transition(task, TaskStatus.PLANNED, "planned")
    task = transition(task, TaskStatus.EXECUTING, "executing")
    task = transition(task, TaskStatus.VALIDATING, "validating")
    with pytest.raises(ValueError, match="Invalid transition"):
        transition(task, TaskStatus.READY_FOR_REVIEW, "invalid")


def test_executing_can_pause_for_scope_decision() -> None:
    task = TaskRecord(task_id="t1", goal="goal", workspace_path=".", status=TaskStatus.EXECUTING)
    paused = transition(task, TaskStatus.AWAITING_SCOPE_DECISION, "scope gate")
    assert paused.status == TaskStatus.AWAITING_SCOPE_DECISION


def test_scope_decision_can_resume_executing() -> None:
    task = TaskRecord(
        task_id="t1", goal="goal", workspace_path=".",
        status=TaskStatus.AWAITING_SCOPE_DECISION,
    )
    resumed = transition(task, TaskStatus.EXECUTING, "scope approved")
    assert resumed.status == TaskStatus.EXECUTING


def test_scope_decision_can_fail() -> None:
    task = TaskRecord(
        task_id="t1", goal="goal", workspace_path=".",
        status=TaskStatus.AWAITING_SCOPE_DECISION,
    )
    failed = transition(task, TaskStatus.FAILED, "scope timeout")
    assert failed.status == TaskStatus.FAILED


def test_invalid_transition_into_scope_decision_from_queued() -> None:
    task = TaskRecord(task_id="t1", goal="goal", workspace_path=".", status=TaskStatus.QUEUED)
    with pytest.raises(ValueError):
        transition(task, TaskStatus.AWAITING_SCOPE_DECISION, "wrong source")


def test_command_decision_edges() -> None:
    # Pause from EXECUTING and resume back, plus terminal edges.
    assert can_transition(TaskStatus.EXECUTING, TaskStatus.AWAITING_COMMAND_DECISION)
    assert can_transition(TaskStatus.AWAITING_COMMAND_DECISION, TaskStatus.EXECUTING)
    assert can_transition(TaskStatus.AWAITING_COMMAND_DECISION, TaskStatus.FAILED)
    assert can_transition(TaskStatus.AWAITING_COMMAND_DECISION, TaskStatus.ABORTED)
    # Not a terminal-success path.
    assert not can_transition(TaskStatus.AWAITING_COMMAND_DECISION, TaskStatus.SUCCEEDED)
