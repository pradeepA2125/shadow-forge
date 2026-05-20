from __future__ import annotations

from pathlib import Path

import pytest

from agentd.domain.models import Diagnostic, TaskRecord, TaskStatus, ValidationResult
from agentd.orchestrator.engine import AgentOrchestrator
from agentd.patch.engine import PatchEngine
from agentd.retrieval.artifact_client import RetrievalContext
from agentd.storage.in_memory import InMemoryTaskStore
from agentd.workspace.shadow import ShadowWorkspaceManager


class RecordingReasoningEngine:
    def __init__(self) -> None:
        self.planning_step_calls: list[dict[str, object]] = []
        self.plan_calls: list[dict[str, object]] = []
        self.patch_calls: list[dict[str, object]] = []

    async def create_planning_step(
        self,
        plan_context: dict[str, object],
        history: list[dict[str, object]],
        tool_definitions: list[dict[str, object]],
        on_thinking: object = None,
        state_description: str = "",
        allowed_action_types: frozenset[str] | None = None,
    ) -> dict[str, object]:
        _ = (history, tool_definitions)
        self.planning_step_calls.append({"plan_context": plan_context})
        return {
            "type": "emit_plan",
            "thought": "stub",
            "plan_markdown": "# Plan\n\n- Create helper",
            "files_examined": [],
            "confidence": "high",
        }

    async def create_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        retrieval_context: dict[str, object],
        on_thinking: object = None,
    ) -> object:
        _ = on_thinking
        self.plan_calls.append(
            {
                "task_id": task.task_id,
                "workspace_path": workspace_path,
                "retrieval_context": retrieval_context,
            }
        )
        return {
            "analysis": "Plan with retrieval",
            "steps": [
                {
                    "id": "S1",
                    "goal": "Create file",
                    "targets": [{"path": "generated.txt", "intent": "new"}],
                    "risk": "low",
                }
            ],
            "expected_files": ["generated.txt"],
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
        _ = kwargs
        self.patch_calls.append(
            {
                "task_id": task.task_id,
                "workspace_path": workspace_path,
                "diagnostics": diagnostics,
                "retrieval_context": retrieval_context,
            }
        )
        return {
            "candidates": [
                {
                    "candidate_id": "c1",
                    "patch_ops": [
                        {
                            "op": "create_file",
                            "file": "generated.txt",
                            "content": "ok",
                            "reason": "retrieval-driven edit",
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
        _ = (tool_definitions, on_thinking)
        in_verify = any(
            isinstance(msg.get("content"), str) and "Patch applied successfully" in msg["content"]
            for msg in history
        )
        if in_verify:
            return {"type": "verify_done", "thought": "scripted", "verified": True, "test_output": ""}
        self.patch_calls.append(
            {
                "task_id": None,
                "workspace_path": None,
                "diagnostics": [],
                "retrieval_context": step_context,
            }
        )
        return {
            "type": "emit_patch",
            "thought": "stub",
            "patch_ops": [
                {
                    "op": "create_file",
                    "file": "generated.txt",
                    "content": "ok",
                    "reason": "retrieval-driven edit",
                }
            ],
        }



class AlwaysPassValidator:
    async def run(self, workspace_path: str) -> ValidationResult:
        _ = workspace_path
        return ValidationResult(success=True, diagnostics=[], duration_ms=1)


class StubRetrievalClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def load_context(
        self,
        workspace_path: str,
        goal: str,
    ) -> tuple[RetrievalContext, list[Diagnostic]]:
        self.calls.append((workspace_path, goal))
        return (
            RetrievalContext(
                related_files=["src/auth.py"],
                related_symbols=["build_auth"],
                graph_neighbors=["function:file:src/auth.py:validate"],
                diagnostics_excerpt=["src/auth.py:12: unresolved name"],
                snapshot_age_sec=12.0,
                snapshot_stats={"node_count": 3, "edge_count": 2, "diagnostic_count": 1},
                planner_evidence={
                    "workspace_files_index": ["src/auth.py"],
                    "evidence_files": [
                        {
                            "path": "src/auth.py",
                            "excerpt": "def build_auth(token):\n    return validate(token)",
                        }
                    ],
                    "evidence_symbols": [],
                    "evidence_routes_models_storage": {"routes": [], "models": [], "storage": []},
                    "diagnostics_excerpt": ["src/auth.py:12: unresolved name"],
                    "confidence_notes": [],
                },
            ),
            [Diagnostic(source="retrieval", message="Snapshot is stale", level="warning")],
        )


@pytest.mark.asyncio
async def test_orchestrator_passes_retrieval_context_to_plan_and_patch(tmp_path: Path) -> None:
    real_workspace = tmp_path / "real"
    real_workspace.mkdir(parents=True)
    (real_workspace / "README.md").write_text("hello\n", encoding="utf-8")

    store = InMemoryTaskStore()
    task = TaskRecord(task_id="task-retrieval-1", goal="Generate helper", workspace_path=str(real_workspace))
    await store.create(task)

    reasoner = RecordingReasoningEngine()
    retrieval_client = StubRetrievalClient()
    orchestrator = AgentOrchestrator(
        store=store,
        reasoning_engine=reasoner,
        validator=AlwaysPassValidator(),
        patch_engine=PatchEngine(),
        workspace_manager=ShadowWorkspaceManager(root_path=tmp_path / "shadows"),
        retrieval_client=retrieval_client,
    )

    initialized = await orchestrator.run_task(task.task_id)
    assert initialized.status == TaskStatus.AWAITING_PLAN_APPROVAL

    result = await orchestrator.continue_task(task.task_id, feedback=None)

    assert result.status == TaskStatus.READY_FOR_REVIEW
    assert retrieval_client.calls == [
        (str(real_workspace), "Generate helper"),
        (str(real_workspace), "Generate helper"),
    ]
    assert len(reasoner.planning_step_calls) == 1
    assert len(reasoner.plan_calls) == 1
    assert len(reasoner.patch_calls) == 1

    # Retrieval context reaches create_planning_step via initial_context
    planning_initial = reasoner.planning_step_calls[0]["plan_context"].get("initial_context", {})
    plan_context = reasoner.plan_calls[0]["retrieval_context"]
    patch_context = reasoner.patch_calls[0]["retrieval_context"]
    assert isinstance(planning_initial, dict)
    assert isinstance(plan_context, dict)
    assert isinstance(patch_context, dict)
    assert "repository_structure" in planning_initial
    assert "file_outlines" in plan_context
    assert "file_contents" in patch_context
    assert "planner_evidence" in planning_initial

    assert result.diagnostics
    assert result.diagnostics[0].source == "retrieval"
    assert result.shadow_workspace_path is not None
    assert Path(result.shadow_workspace_path, "generated.txt").exists()
