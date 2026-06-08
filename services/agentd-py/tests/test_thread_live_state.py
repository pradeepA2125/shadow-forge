"""ThreadLiveState resolver + the pending_validation field it relies on."""
from __future__ import annotations

import pytest

from agentd.chat.live_state import resolve_live_state
from agentd.domain.models import (
    CommandApprovalRequest,
    ScopeExtensionRequest,
    StepReviewPayload,
    TaskExecutionState,
    TaskRecord,
    TaskStatus,
)


def test_execution_state_has_pending_validation_field() -> None:
    st = TaskExecutionState()
    assert st.pending_validation is None
    st.pending_validation = {"summary": "2 failed", "diagnostics": []}
    assert st.pending_validation["summary"] == "2 failed"


def _task(status: TaskStatus, *, es: TaskExecutionState | None = None, plan: str | None = None) -> TaskRecord:
    return TaskRecord(
        task_id="t1",
        goal="g",
        workspace_path="/w",
        status=status,
        execution_state=es or TaskExecutionState(),
        plan_markdown=plan,
    )


def _getter(task: TaskRecord):
    def get(task_id: str) -> TaskRecord:
        if task_id != task.task_id:
            raise KeyError(task_id)
        return task
    return get


def test_no_active_task_returns_nulls() -> None:
    ls = resolve_live_state(None, _getter(_task(TaskStatus.EXECUTING)))
    assert ls.active_task_id is None and ls.status is None
    assert ls.pending_gate is None and ls.plan is None


def test_missing_task_returns_nulls() -> None:
    ls = resolve_live_state("ghost", _getter(_task(TaskStatus.EXECUTING)))
    assert ls.active_task_id is None and ls.pending_gate is None


def test_command_gate() -> None:
    es = TaskExecutionState(
        pending_command_request=CommandApprovalRequest(
            decision_id="d1", command="pytest", args=["-x"], cwd=".", step_id="s1"
        )
    )
    t = _task(TaskStatus.AWAITING_COMMAND_DECISION, es=es)
    ls = resolve_live_state("t1", _getter(t))
    assert ls.status == "AWAITING_COMMAND_DECISION"
    assert ls.pending_gate is not None
    assert ls.pending_gate.kind == "command"
    assert ls.pending_gate.payload["command"] == "pytest"
    assert ls.pending_gate.payload["args"] == ["-x"]


def test_step_gate() -> None:
    es = TaskExecutionState(
        pending_step_review=StepReviewPayload(step_id="s2", step_title="Add edges", diff_entries=[])
    )
    t = _task(TaskStatus.AWAITING_STEP_REVIEW, es=es)
    ls = resolve_live_state("t1", _getter(t))
    assert ls.pending_gate is not None
    assert ls.pending_gate.kind == "step"
    assert ls.pending_gate.payload["step_title"] == "Add edges"


def test_step_gate_carries_diff_entries_with_temp_path() -> None:
    # The chat live-slot step card renders an inline diff from these fields, so the
    # /live payload must carry per-file path + temp_path (shadow path for vscode.diff).
    es = TaskExecutionState(
        pending_step_review=StepReviewPayload(
            step_id="s3",
            step_title="Touch auth",
            diff_entries=[{"path": "auth.py", "additions": 3, "deletions": 1, "temp_path": "/shadow/auth.py"}],
        )
    )
    t = _task(TaskStatus.AWAITING_STEP_REVIEW, es=es)
    ls = resolve_live_state("t1", _getter(t))
    entries = ls.pending_gate.payload["diff_entries"]
    assert entries[0]["path"] == "auth.py"
    assert entries[0]["temp_path"] == "/shadow/auth.py"


def test_scope_gate() -> None:
    es = TaskExecutionState(
        pending_scope_request=ScopeExtensionRequest(
            decision_id="d2", files=["a.py"], reason="needed", step_id="s1"
        )
    )
    t = _task(TaskStatus.AWAITING_SCOPE_DECISION, es=es)
    ls = resolve_live_state("t1", _getter(t))
    assert ls.pending_gate is not None
    assert ls.pending_gate.kind == "scope"
    assert ls.pending_gate.payload["files"] == ["a.py"]


def test_validation_gate() -> None:
    es = TaskExecutionState(pending_validation={"summary": "1 failed", "diagnostics": ["x"]})
    t = _task(TaskStatus.AWAITING_VALIDATION_DECISION, es=es)
    ls = resolve_live_state("t1", _getter(t))
    assert ls.pending_gate is not None
    assert ls.pending_gate.kind == "validation"
    assert ls.pending_gate.payload["summary"] == "1 failed"


def test_plan_surfaced_only_on_awaiting_plan_approval() -> None:
    approving = _task(TaskStatus.AWAITING_PLAN_APPROVAL, plan="# Plan\n- step")
    ls = resolve_live_state("t1", _getter(approving))
    assert ls.plan is not None and ls.plan["plan_markdown"] == "# Plan\n- step"
    assert ls.pending_gate is None

    executing = _task(TaskStatus.EXECUTING, plan="# Plan\n- step")
    ls2 = resolve_live_state("t1", _getter(executing))
    assert ls2.plan is None  # plan is only the current ACTIONABLE plan
