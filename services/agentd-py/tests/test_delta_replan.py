# tests/test_delta_replan.py
from __future__ import annotations

from pathlib import Path

import pytest

from agentd.domain.models import (
    PlanStep,
    TaskBudget,
    TaskRecord,
    TaskStatus,
    TaskUsage,
    ValidationResult,
)
from agentd.orchestrator.broadcaster import PatchEventBroadcaster
from agentd.orchestrator.engine import AgentOrchestrator
from agentd.patch.engine import PatchEngine
from agentd.storage.in_memory import InMemoryTaskStore
from agentd.tools.loop import PlanHandoff, ToolLoop
from agentd.tools.registry import ToolRegistry
from agentd.workspace.shadow import ShadowWorkspaceManager


# ---------------------------------------------------------------------------
# Shared stubs
# ---------------------------------------------------------------------------


class AlwaysPassValidator:
    async def run_touched(self, workspace_path: str, touched_files: list[str]) -> ValidationResult:
        _ = (workspace_path, touched_files)
        return ValidationResult(success=True, diagnostics=[], duration_ms=1)

    async def run(self, workspace_path: str) -> ValidationResult:
        _ = workspace_path
        return ValidationResult(success=True, diagnostics=[], duration_ms=1)


# ---------------------------------------------------------------------------
# ToolLoop unit test (Task 8 coverage — PlanHandoff returned on revision_needed)
# ---------------------------------------------------------------------------


class RevisionNeededEngine:
    """Engine that immediately emits revision_needed."""

    async def create_tool_step(self, step_context, history, tool_definitions, on_thinking=None, state_description="", allowed_action_types=None):
        return {
            "type": "revision_needed",
            "thought": "Target file is wrong",
            "reason": "function not in planned file",
            "evidence": "grep found it in other.py",
            "affected_steps": ["s2"],
        }

    async def create_planning_step(self, *a, **kw): return {}
    async def create_plan(self, *a, **kw): return {}
    async def create_patch(self, *a, **kw): return {}


@pytest.mark.asyncio
async def test_tool_loop_returns_plan_handoff_on_revision_needed(tmp_path: Path):
    step = PlanStep(
        id="s1",
        goal="add logging",
        targets=[{"path": "src/api.py", "intent": "existing"}],
        risk="low",
    )
    loop = ToolLoop(
        reasoning_engine=RevisionNeededEngine(),
        registry=ToolRegistry(shadow_root=tmp_path, real_workspace_path=tmp_path),
        broadcaster=PatchEventBroadcaster(),
        task_id="t1",
    )
    outcome = await loop.run(step, {}, TaskBudget(), TaskUsage())
    assert isinstance(outcome, PlanHandoff)
    assert outcome.step_id == "s1"
    assert outcome.reason == "function not in planned file"
    assert outcome.evidence == "grep found it in other.py"
    assert outcome.hinted_affected_steps == ["s2"]


# ---------------------------------------------------------------------------
# Integration: delta replan corrects step target (happy path)
# ---------------------------------------------------------------------------


class DeltaReplanReasoner:
    """Emits revision_needed on first S1 (wrong_file.py); succeeds on revised S1 (correct_file.py)."""

    def __init__(self) -> None:
        self.planning_step_calls = 0
        self.tool_step_calls = 0

    async def create_planning_step(
        self,
        plan_context: dict,
        history: list,
        tool_definitions: list,
        on_thinking: object = None,
        state_description: str = "",
        allowed_action_types: frozenset[str] | None = None,
    ) -> dict:
        _ = (history, tool_definitions)
        self.planning_step_calls += 1
        if "revision_request" in plan_context:
            # Called from PlanningAgent.revise()
            return {
                "type": "emit_revision",
                "thought": "Correct target is correct_file.py",
                "revised_steps": [
                    {
                        "step_id": "S1",
                        "goal": "Add helper to correct file",
                        "targets": [{"path": "correct_file.py", "intent": "existing"}],
                        "implementation_details": "add helper function",
                        "edge_cases": "",
                        "testing_strategy": "",
                        "risk": "low",
                    }
                ],
                "reverted_step_ids": [],
                "revision_summary": "Switched target from wrong_file.py to correct_file.py",
            }
        # Called from PlanningAgent.generate_plan()
        return {
            "type": "emit_plan",
            "thought": "planning",
            "plan_markdown": "# Plan\n\n- Add helper",
            "files_examined": [],
            "confidence": "high",
        }

    async def create_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        retrieval_context: dict,
        on_thinking: object = None,
    ) -> object:
        _ = (task, workspace_path, retrieval_context, on_thinking)
        return {
            "analysis": "initial plan — targets wrong file",
            "steps": [
                {
                    "id": "S1",
                    "goal": "Add helper",
                    "targets": [{"path": "wrong_file.py", "intent": "existing"}],
                    "risk": "low",
                }
            ],
            "expected_files": ["wrong_file.py"],
            "stop_conditions": ["validation passes"],
        }

    async def create_tool_step(
        self,
        step_context: dict,
        history: list,
        tool_definitions: list,
        on_thinking: object = None,
        state_description: str = "",
        allowed_action_types: frozenset[str] | None = None,
    ) -> dict:
        _ = (tool_definitions, on_thinking)
        self.tool_step_calls += 1
        allowed_files = step_context.get("allowed_files", [])
        if "correct_file.py" in allowed_files:
            # In verify phase (patch already applied) — signal completion
            in_verify = any(
                isinstance(msg.get("content"), str) and "Patch applied successfully" in msg["content"]
                for msg in history
            )
            if in_verify:
                return {"type": "verify_done", "thought": "patch applied", "verified": True, "test_output": ""}
            return {
                "type": "emit_patch",
                "thought": "patching correct file",
                "patch_ops": [
                    {
                        "op": "search_replace",
                        "file": "correct_file.py",
                        "search": "# target",
                        "replace": "# target\n# added by delta replan",
                        "reason": "add helper",
                    }
                ],
            }
        return {
            "type": "revision_needed",
            "thought": "wrong file targeted",
            "reason": "helper not in wrong_file.py",
            "evidence": "searched wrong_file.py, function not found",
            "affected_steps": [],
        }

    async def create_patch(self, *a, **kw): return {}


@pytest.mark.asyncio
async def test_orchestrator_delta_replan_corrects_step_target(tmp_path: Path) -> None:
    """ToolLoop revision_needed → engine revises plan → step retries with correct target → READY_FOR_REVIEW."""
    real_workspace = tmp_path / "real"
    real_workspace.mkdir(parents=True)
    (real_workspace / "wrong_file.py").write_text("# wrong\n", encoding="utf-8")
    (real_workspace / "correct_file.py").write_text("# target\n", encoding="utf-8")

    store = InMemoryTaskStore()
    task = TaskRecord(
        task_id="task-delta-replan",
        goal="Add helper",
        workspace_path=str(real_workspace),
    )
    await store.create(task)

    reasoner = DeltaReplanReasoner()
    orchestrator = AgentOrchestrator(
        store=store,
        reasoning_engine=reasoner,
        validator=AlwaysPassValidator(),
        patch_engine=PatchEngine(),
        workspace_manager=ShadowWorkspaceManager(root_path=tmp_path / "shadows"),
    )

    initialized = await orchestrator.run_task(task.task_id)
    assert initialized.status == TaskStatus.AWAITING_PLAN_APPROVAL

    result = await orchestrator.continue_task(task.task_id, feedback=None)

    assert result.status == TaskStatus.READY_FOR_REVIEW
    # generate_plan() + revise() = 2 planning_step calls
    assert reasoner.planning_step_calls == 2
    # revision_needed on wrong_file + emit_patch on correct_file + verify_done = 3 tool_step calls
    assert reasoner.tool_step_calls == 3
    # The revised step patched correct_file.py
    assert "correct_file.py" in result.modified_files
    # Execution state reflects exactly one delta replan
    assert result.execution_state.delta_replans_used == 1
    assert len(result.execution_state.delta_replan_requests) == 1
    assert result.execution_state.delta_replan_requests[0].requested_by_step_id == "S1"
    assert result.execution_state.delta_replan_requests[0].reason == "helper not in wrong_file.py"
    # Shadow workspace has the patched content
    assert result.shadow_workspace_path is not None
    content = Path(result.shadow_workspace_path, "correct_file.py").read_text(encoding="utf-8")
    assert "# added by delta replan" in content


# ---------------------------------------------------------------------------
# Integration: delta replan budget exhaustion
# ---------------------------------------------------------------------------


class AlwaysRevisionNeededReasoner:
    """Always returns revision_needed; each revise() returns emit_revision with same target."""

    def __init__(self) -> None:
        self.planning_step_calls = 0
        self.tool_step_calls = 0

    async def create_planning_step(
        self,
        plan_context: dict,
        history: list,
        tool_definitions: list,
        on_thinking: object = None,
        state_description: str = "",
        allowed_action_types: frozenset[str] | None = None,
    ) -> dict:
        _ = (history, tool_definitions)
        self.planning_step_calls += 1
        if "revision_request" in plan_context:
            return {
                "type": "emit_revision",
                "thought": "still trying",
                "revised_steps": [
                    {
                        "step_id": "S1",
                        "goal": "Still wrong",
                        "targets": [{"path": "some_file.py", "intent": "existing"}],
                        "implementation_details": "noop",
                        "edge_cases": "",
                        "testing_strategy": "",
                        "risk": "low",
                    }
                ],
                "reverted_step_ids": [],
                "revision_summary": "same target, keeps failing",
            }
        return {
            "type": "emit_plan",
            "thought": "planning",
            "plan_markdown": "# Plan\n\n- Do something",
            "files_examined": [],
            "confidence": "high",
        }

    async def create_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        retrieval_context: dict,
        on_thinking: object = None,
    ) -> object:
        _ = (task, workspace_path, retrieval_context, on_thinking)
        return {
            "analysis": "always-bad plan",
            "steps": [
                {
                    "id": "S1",
                    "goal": "Do something",
                    "targets": [{"path": "some_file.py", "intent": "existing"}],
                    "risk": "low",
                }
            ],
            "expected_files": ["some_file.py"],
            "stop_conditions": ["validation passes"],
        }

    async def create_tool_step(
        self,
        step_context: dict,
        history: list,
        tool_definitions: list,
        on_thinking: object = None,
        state_description: str = "",
        allowed_action_types: frozenset[str] | None = None,
    ) -> dict:
        _ = (step_context, history, tool_definitions, on_thinking)
        self.tool_step_calls += 1
        return {
            "type": "revision_needed",
            "thought": "always needs revision",
            "reason": "something is always wrong",
            "evidence": "evidence",
            "affected_steps": [],
        }

    async def create_markdown_plan(self, *a, **kw): return ""
    async def critique_markdown_plan(self, *a, **kw): return {"verdict": "pass", "issues": []}
    async def critique_json_plan(self, *a, **kw): return {"verdict": "pass", "issues": []}
    async def create_patch(self, *a, **kw): return {}


@pytest.mark.asyncio
async def test_orchestrator_delta_replan_budget_exhausted(tmp_path: Path) -> None:
    """After max_delta_replans PlanHandoffs, engine fails with budget exhausted diagnostic."""
    real_workspace = tmp_path / "real"
    real_workspace.mkdir(parents=True)
    (real_workspace / "some_file.py").write_text("# target\n", encoding="utf-8")

    store = InMemoryTaskStore()
    task = TaskRecord(
        task_id="task-delta-budget",
        goal="Do something",
        workspace_path=str(real_workspace),
        budget=TaskBudget(max_delta_replans=2),
    )
    await store.create(task)

    reasoner = AlwaysRevisionNeededReasoner()
    orchestrator = AgentOrchestrator(
        store=store,
        reasoning_engine=reasoner,
        validator=AlwaysPassValidator(),
        patch_engine=PatchEngine(),
        workspace_manager=ShadowWorkspaceManager(root_path=tmp_path / "shadows"),
    )

    initialized = await orchestrator.run_task(task.task_id)
    assert initialized.status == TaskStatus.AWAITING_PLAN_APPROVAL

    result = await orchestrator.continue_task(task.task_id, feedback=None)

    assert result.status == TaskStatus.FAILED
    assert any("delta replan budget exhausted" in d.message.lower() for d in result.diagnostics)
    # 2 successful revisions used, fail on 3rd PlanHandoff
    assert result.execution_state.delta_replans_used == 2
    # tool_step: 3 revision_needed (one per while-loop iteration before budget check)
    assert reasoner.tool_step_calls == 3
    # planning_step: 1 generate_plan + 2 revise() calls = 3
    assert reasoner.planning_step_calls == 3
