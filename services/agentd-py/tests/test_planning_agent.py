# tests/test_planning_agent.py
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentd.domain.models import (
    AgentToolTrace,
    DeltaReplanRequest,
    PlanDocument,
    PlanRevisionResult,
    PlanStep,
    PlanningResult,
    TaskBudget,
    TaskExecutionState,
    TaskRecord,
    TaskStatus,
)
from agentd.orchestrator.broadcaster import PatchEventBroadcaster
from agentd.planning.agent import PlanningAgent
from agentd.planning.loop import (
    PlanningBudgetExceededError,
    PlanningLoop,
    _validate_no_duplicate_file_targets,
)
from agentd.planning.registry import PlanningToolRegistry


class ScriptedPlanningEngine:
    """Scripted engine that returns predetermined responses for the planning loop."""

    def __init__(self, responses: list[dict]) -> None:
        self._responses = responses
        self._index = 0

    async def create_planning_step(
        self,
        plan_context: dict,
        history: list,
        tool_definitions: list,
        on_thinking: object = None,
    ) -> dict:
        idx = min(self._index, len(self._responses) - 1)
        self._index += 1
        return self._responses[idx]

    async def create_plan(self, *a, **kw): return {}
    async def create_patch(self, *a, **kw): return {}
    async def create_tool_step(self, *a, **kw): return {"type": "emit_patch", "thought": "", "patch_ops": []}


def _make_task(tmp_path: Path) -> TaskRecord:
    return TaskRecord(task_id="t1", goal="add logging", workspace_path=str(tmp_path))


def _make_registry(tmp_path: Path) -> PlanningToolRegistry:
    return PlanningToolRegistry(real_path=tmp_path)


def _make_broadcaster() -> PatchEventBroadcaster:
    return PatchEventBroadcaster()


# --- _validate_no_duplicate_file_targets ---

def test_no_duplicates_passes():
    steps = [
        {"id": "s1", "targets": [{"path": "a.py"}, {"path": "b.py"}]},
        {"id": "s2", "targets": [{"path": "c.py"}]},
    ]
    assert _validate_no_duplicate_file_targets(steps) == []


def test_duplicate_across_steps_detected():
    steps = [
        {"id": "s1", "targets": [{"path": "a.py"}]},
        {"id": "s2", "targets": [{"path": "a.py"}]},
    ]
    errors = _validate_no_duplicate_file_targets(steps)
    assert len(errors) == 1
    assert "a.py" in errors[0]
    assert "s1" in errors[0]
    assert "s2" in errors[0]


def test_same_file_within_one_step_not_caught():
    steps = [{"id": "s1", "targets": [{"path": "a.py"}, {"path": "b.py"}]}]
    assert _validate_no_duplicate_file_targets(steps) == []


# --- PlanningLoop.run() ---

@pytest.mark.asyncio
async def test_planning_loop_emit_plan(tmp_path: Path):
    engine = ScriptedPlanningEngine([
        {
            "type": "emit_plan",
            "thought": "Ready",
            "plan_markdown": "# Plan\n- step 1",
            "files_examined": ["src/auth.py"],
            "confidence": "high",
        }
    ])
    loop = PlanningLoop(
        reasoning_engine=engine,
        registry=_make_registry(tmp_path),
        broadcaster=_make_broadcaster(),
        task_id="t1",
    )
    result = await loop.run({"goal": "add logging", "workspace_path": str(tmp_path)}, TaskBudget())
    assert isinstance(result, PlanningResult)
    assert result.plan_markdown == "# Plan\n- step 1"
    assert result.files_examined == ["src/auth.py"]
    assert result.confidence == "high"


@pytest.mark.asyncio
async def test_planning_loop_tool_call_then_emit_plan(tmp_path: Path):
    engine = ScriptedPlanningEngine([
        {"type": "tool_call", "thought": "Searching", "tool": "list_directory", "args": {}},
        {
            "type": "emit_plan",
            "thought": "Done",
            "plan_markdown": "# Plan",
            "files_examined": [],
            "confidence": "medium",
        },
    ])
    loop = PlanningLoop(
        reasoning_engine=engine,
        registry=_make_registry(tmp_path),
        broadcaster=_make_broadcaster(),
        task_id="t1",
    )
    result = await loop.run({"goal": "test", "workspace_path": str(tmp_path)}, TaskBudget())
    assert isinstance(result, PlanningResult)
    assert result.confidence == "medium"


@pytest.mark.asyncio
async def test_planning_loop_recovers_from_malformed_response(tmp_path: Path):
    """An empty/typeless response (weak-model failure mode) is corrected and retried,
    not fatal — a subsequent emit_plan still succeeds."""
    engine = ScriptedPlanningEngine([
        {},  # empty object: type == "" → malformed
        {"thought": "still confused"},  # missing type → malformed
        {
            "type": "emit_plan",
            "thought": "Recovered",
            "plan_markdown": "# Plan\n- step 1",
            "files_examined": [],
            "confidence": "medium",
        },
    ])
    loop = PlanningLoop(
        reasoning_engine=engine,
        registry=_make_registry(tmp_path),
        broadcaster=_make_broadcaster(),
        task_id="t1",
    )
    result = await loop.run({"goal": "test", "workspace_path": str(tmp_path)}, TaskBudget())
    assert isinstance(result, PlanningResult)
    assert result.plan_markdown == "# Plan\n- step 1"


@pytest.mark.asyncio
async def test_planning_loop_bails_after_consecutive_malformed(tmp_path: Path):
    """A model that only ever returns empty responses fails gracefully after the cap
    rather than spinning to the full tool-call budget."""
    engine = ScriptedPlanningEngine([{}])  # always empty
    loop = PlanningLoop(
        reasoning_engine=engine,
        registry=_make_registry(tmp_path),
        broadcaster=_make_broadcaster(),
        task_id="t1",
    )
    with pytest.raises(PlanningBudgetExceededError, match="consecutive malformed"):
        await loop.run({"goal": "test", "workspace_path": str(tmp_path)}, TaskBudget())


@pytest.mark.asyncio
async def test_planning_loop_emit_revision(tmp_path: Path):
    engine = ScriptedPlanningEngine([
        {
            "type": "emit_revision",
            "thought": "Fixed",
            "revised_steps": [{
                "step_id": "s1",
                "goal": "Fixed goal",
                "targets": [{"path": "a.py", "intent": "existing"}],
                "implementation_details": "do it",
            }],
            "reverted_step_ids": [],
            "revision_summary": "s1 retargeted",
        }
    ])
    loop = PlanningLoop(
        reasoning_engine=engine,
        registry=_make_registry(tmp_path),
        broadcaster=_make_broadcaster(),
        task_id="t1",
    )
    result = await loop.run(
        {"goal": "fix", "workspace_path": str(tmp_path)},
        TaskBudget(),
        revision_mode=True,
    )
    assert isinstance(result, PlanRevisionResult)
    assert len(result.revised_steps) == 1
    assert result.revised_steps[0].step_id == "s1"
    assert result.revision_summary == "s1 retargeted"


# --- PlanningAgent ---

@pytest.mark.asyncio
async def test_planning_agent_generate_plan(tmp_path: Path):
    engine = ScriptedPlanningEngine([
        {
            "type": "emit_plan",
            "thought": "Ready",
            "plan_markdown": "# Plan",
            "files_examined": [],
            "confidence": "high",
        }
    ])
    agent = PlanningAgent(
        reasoning_engine=engine,
        registry=_make_registry(tmp_path),
        broadcaster=_make_broadcaster(),
        task_id="t1",
    )
    task = _make_task(tmp_path)
    result = await agent.generate_plan(task, initial_context={}, budget=TaskBudget())
    assert isinstance(result, PlanningResult)
    assert result.plan_markdown == "# Plan"


@pytest.mark.asyncio
async def test_planning_agent_revise(tmp_path: Path):
    engine = ScriptedPlanningEngine([
        {
            "type": "emit_revision",
            "thought": "Fixed",
            "revised_steps": [{
                "step_id": "s2",
                "goal": "Retargeted",
                "targets": [{"path": "correct.py", "intent": "existing"}],
                "implementation_details": "add log",
            }],
            "reverted_step_ids": [],
            "revision_summary": "s2 fixed",
        }
    ])
    task = _make_task(tmp_path)
    task.plan = PlanDocument(
        analysis="test",
        steps=[
            PlanStep(id="s1", goal="done", targets=[{"path": "a.py", "intent": "existing"}], risk="low"),
            PlanStep(id="s2", goal="failed", targets=[{"path": "b.py", "intent": "existing"}], risk="low"),
        ],
        expected_files=["a.py", "b.py"],
        stop_conditions=[],
    )
    task.completed_step_ids = ["s1"]
    task.execution_state.delta_replan_requests.append(
        DeltaReplanRequest(
            requested_by_step_id="s2",
            reason="wrong file",
            evidence="function in correct.py",
            hinted_affected_steps=[],
            requested_at=datetime.now(timezone.utc),
        )
    )
    task.execution_state.step_checkpoints["s1"] = "/tmp/checkpoint-s1"

    agent = PlanningAgent(
        reasoning_engine=engine,
        registry=_make_registry(tmp_path),
        broadcaster=_make_broadcaster(),
        task_id="t1",
    )
    result = await agent.revise(task, tmp_path)
    assert isinstance(result, PlanRevisionResult)
    assert result.revised_steps[0].step_id == "s2"
    assert result.revised_steps[0].goal == "Retargeted"


# --- _validate_no_duplicate_file_targets: multi-file scenarios ---

def test_duplicate_targets_across_steps_multi_file():
    steps = [
        {"id": "s1", "targets": [{"path": "src/auth.py"}, {"path": "src/models.py"}]},
        {"id": "s2", "targets": [{"path": "src/auth.py"}]},
    ]
    errors = _validate_no_duplicate_file_targets(steps)
    assert len(errors) == 1
    assert "src/auth.py" in errors[0]
    assert "s1" in errors[0]
    assert "s2" in errors[0]


def test_no_cross_step_duplicates_with_different_files():
    steps = [
        {"id": "s1", "targets": [{"path": "a.py"}, {"path": "b.py"}]},
        {"id": "s2", "targets": [{"path": "c.py"}, {"path": "d.py"}]},
    ]
    assert _validate_no_duplicate_file_targets(steps) == []
