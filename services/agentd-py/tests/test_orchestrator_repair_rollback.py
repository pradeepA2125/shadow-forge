from __future__ import annotations

from pathlib import Path

import pytest

from agentd.domain.models import (
    Diagnostic,
    TaskBudget,
    TaskRecord,
    TaskStatus,
    ValidationResult,
)
from agentd.orchestrator.engine import AgentOrchestrator
from agentd.patch.engine import PatchEngine
from agentd.storage.in_memory import InMemoryTaskStore
from agentd.workspace.shadow import ShadowWorkspaceManager


class RepairReasoningEngine:
    def __init__(self) -> None:
        self.patch_calls = 0

    async def create_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        retrieval_context: dict[str, object],
        on_thinking: object = None,
    ) -> object:
        _ = (task, workspace_path, retrieval_context, on_thinking)
        return {
            "analysis": "Insert a marker line after class declaration.",
            "steps": [
                {
                    "id": "S1",
                    "goal": "Insert marker",
                    "targets": [{"path": "src/example.py", "intent": "existing"}],
                    "risk": "low",
                }
            ],
            "expected_files": ["src/example.py"],
            "stop_conditions": ["validation passes"],
        }

    async def create_patch(
        self,
        task: TaskRecord,
        workspace_path: str,
        diagnostics: list[Diagnostic],
        retrieval_context: dict[str, object],
        **kwargs: object,
    ) -> object:
        _ = (task, workspace_path, diagnostics, retrieval_context, kwargs)
        self.patch_calls += 1
        return {
            "candidates": [
                {
                    "candidate_id": "c1",
                    "patch_ops": [
                        {
                            "op": "replace_node",
                            "file": "src/example.py",
                            "language": "python",
                            "selector": {"kind": "symbol", "value": "X", "match": "exact"},
                            "content": "class X:\n    pass\n    injected = True\n",
                            "reason": "repair rollback regression test",
                        }
                    ],
                }
            ]
        }

    async def create_tool_step(
        self,
        step_context: dict[str, object],
        history: list[dict[str, object]],
        tool_definitions: list[dict[str, object]],
        on_thinking: object = None,
        state_description: str = "",
        allowed_action_types: frozenset[str] | None = None,
    ) -> dict[str, object]:
        _ = (step_context, tool_definitions, on_thinking)
        in_verify = any(
            isinstance(msg.get("content"), str) and "Patch applied successfully" in msg["content"]
            for msg in history
        )
        if in_verify:
            return {"type": "verify_done", "thought": "scripted", "verified": True, "test_output": ""}
        self.patch_calls += 1
        return {
            "type": "emit_patch",
            "thought": "scripted",
            "patch_ops": [
                {
                    "op": "replace_node",
                    "file": "src/example.py",
                    "language": "python",
                    "selector": {"kind": "symbol", "value": "X", "match": "exact"},
                    "content": "class X:\n    pass\n    injected = True\n",
                    "reason": "repair rollback regression test",
                }
            ],
        }

    async def create_planning_step(
        self,
        plan_context: dict,
        history: list,
        tool_definitions: list,
        on_thinking: object = None,
        state_description: str = "",
        allowed_action_types: frozenset[str] | None = None,
    ) -> dict:
        _ = (plan_context, history, tool_definitions)
        return {
            "type": "emit_plan",
            "thought": "stub: planning agent bypassed",
            "plan_markdown": "# Stub Plan\n\n- Review generated changes",
            "files_examined": [],
            "confidence": "high",
        }


class FailOnceValidator:
    def __init__(self) -> None:
        self.calls = 0
        self.fast_calls = 0

    async def run_touched(self, workspace_path: str, touched_files: list[str]) -> ValidationResult:
        _ = (workspace_path, touched_files)
        self.fast_calls += 1
        if self.fast_calls == 1:
            return ValidationResult(
                success=False,
                diagnostics=[
                    Diagnostic(source="validator", message="intentional first fast failure", level="error")
                ],
                duration_ms=1,
            )
        return ValidationResult(success=True, diagnostics=[], duration_ms=1)

    async def run(self, workspace_path: str) -> ValidationResult:
        _ = workspace_path
        self.calls += 1
        return ValidationResult(success=True, diagnostics=[], duration_ms=1)


@pytest.mark.asyncio
async def test_orchestrator_rolls_back_failed_repair_iteration(tmp_path: Path) -> None:
    real_workspace = tmp_path / "real"
    (real_workspace / "src").mkdir(parents=True)
    target = real_workspace / "src/example.py"
    target.write_text("class X:\n    pass\n", encoding="utf-8")

    store = InMemoryTaskStore()
    task = TaskRecord(
        task_id="task-repair-rollback",
        goal="Insert marker",
        workspace_path=str(real_workspace),
        budget=TaskBudget(max_iterations=3),
    )
    await store.create(task)

    reasoner = RepairReasoningEngine()
    validator = FailOnceValidator()
    orchestrator = AgentOrchestrator(
        store=store,
        reasoning_engine=reasoner,
        validator=validator,
        patch_engine=PatchEngine(),
        workspace_manager=ShadowWorkspaceManager(root_path=tmp_path / "shadows"),
    )

    initialized = await orchestrator.run_task(task.task_id)
    assert initialized.status == TaskStatus.AWAITING_PLAN_APPROVAL

    result = await orchestrator.continue_task(task.task_id, feedback=None)

    assert result.status == TaskStatus.READY_FOR_REVIEW
    assert reasoner.patch_calls == 1
    # Tool-loop inline apply bypasses _evaluate_candidates — run_touched is no longer called
    # per step; verify phase owns validation now.
    assert validator.fast_calls == 0
    assert validator.calls == 2  # 1 baseline capture + 1 post-execution full validation
    assert result.shadow_workspace_path is not None
    assert result.completed_step_ids == ["S1"]

    shadow_target = Path(result.shadow_workspace_path) / "src/example.py"
    content = shadow_target.read_text(encoding="utf-8")
    assert content.count("injected = True") == 1
