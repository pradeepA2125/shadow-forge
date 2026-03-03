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
        self.plan_calls: list[dict[str, object]] = []
        self.patch_calls: list[dict[str, object]] = []

    async def create_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        retrieval_context: dict[str, object],
    ) -> object:
        self.plan_calls.append(
            {
                "task_id": task.task_id,
                "workspace_path": workspace_path,
                "retrieval_context": retrieval_context,
            }
        )
        return {
            "analysis": "Plan with retrieval",
            "steps": [{"id": "S1", "goal": "Create file", "targets": ["generated.txt"], "risk": "low"}],
            "expected_files": ["generated.txt"],
            "stop_conditions": ["validation passes"],
        }

    async def create_patch(
        self,
        task: TaskRecord,
        workspace_path: str,
        diagnostics: list[Diagnostic],
        retrieval_context: dict[str, object],
    ) -> object:
        self.patch_calls.append(
            {
                "task_id": task.task_id,
                "workspace_path": workspace_path,
                "diagnostics": diagnostics,
                "retrieval_context": retrieval_context,
            }
        )
        return {
            "patch_ops": [
                {
                    "op": "create_file",
                    "file": "generated.txt",
                    "content": "ok",
                    "reason": "retrieval-driven edit",
                }
            ]
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

    result = await orchestrator.run_task(task.task_id)

    assert result.status == TaskStatus.READY_FOR_REVIEW
    assert retrieval_client.calls == [(str(real_workspace), "Generate helper")]
    assert len(reasoner.plan_calls) == 1
    assert len(reasoner.patch_calls) == 1

    plan_context = reasoner.plan_calls[0]["retrieval_context"]
    patch_context = reasoner.patch_calls[0]["retrieval_context"]
    assert isinstance(plan_context, dict)
    assert isinstance(patch_context, dict)
    assert plan_context["related_files"] == ["src/auth.py"]
    assert patch_context["related_symbols"] == ["build_auth"]

    assert result.diagnostics
    assert result.diagnostics[0].source == "retrieval"
    assert result.shadow_workspace_path is not None
    assert Path(result.shadow_workspace_path, "generated.txt").exists()
