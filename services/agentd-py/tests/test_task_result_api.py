from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agentd.api.routes import build_router
from agentd.domain.models import (
    Diagnostic,
    PatchDocument,
    PatchDocumentV2,
    PlanDocument,
    StepExecutionTrace,
    TaskRecord,
    TaskStatus,
)
from agentd.storage.in_memory import InMemoryTaskStore
from agentd.workspace.shadow import ShadowWorkspaceManager


class DummyOrchestrator:
    def __init__(self) -> None:
        self.breadcrumbs: list[str] = []

    async def run_task(self, task_id: str) -> None:
        _ = task_id

    async def continue_task(self, task_id: str, feedback: str | None = None) -> None:
        _ = (task_id, feedback)

    def write_chat_breadcrumb(self, task: TaskRecord, text: str) -> None:
        _ = task
        self.breadcrumbs.append(text)


def _build_app(
    store: InMemoryTaskStore,
    workspace_manager: ShadowWorkspaceManager,
    orchestrator: DummyOrchestrator | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(
        build_router(store, orchestrator or DummyOrchestrator(), workspace_manager)
    )
    return app


def _sample_plan() -> PlanDocument:
    return PlanDocument.model_validate(
        {
            "analysis": "Need review",
            "steps": [
                {
                    "id": "S1",
                    "goal": "Update file",
                    "targets": [{"path": "src/main.py", "intent": "existing"}],
                    "risk": "low",
                }
            ],
            "expected_files": ["src/main.py"],
            "stop_conditions": ["validation passes"],
        }
    )


def _sample_patch() -> PatchDocument:
    return PatchDocument.model_validate(
        {
            "patch_ops": [
                {
                    "op": "replace_range",
                    "file": "src/main.py",
                    "anchor": {"start_line": 1, "end_line": 1},
                    "content": "print('shadow')",
                    "reason": "update",
                }
            ]
        }
    )


@pytest.mark.asyncio
async def test_get_task_result_returns_rich_task_payload(tmp_path: Path) -> None:
    store = InMemoryTaskStore()
    manager = ShadowWorkspaceManager(root_path=tmp_path / "shadows")

    real_workspace = tmp_path / "real"
    real_workspace.mkdir(parents=True)

    task = TaskRecord(
        task_id="task-0",
        goal="goal",
        workspace_path=str(real_workspace),
        shadow_workspace_path=str(tmp_path / "shadows/task-0"),
        status=TaskStatus.READY_FOR_REVIEW,
        plan=_sample_plan(),
        latest_patch=_sample_patch(),
        modified_files=["src/main.py"],
        completed_step_ids=["S1"],
        diagnostics=[Diagnostic(source="validator", message="warn", level="warning")],
        execution_trace=[
            StepExecutionTrace(
                step_id="S1",
                attempt=1,
                status="step_completed",
                message="ok",
            )
        ],
    )
    await store.create(task)

    app = _build_app(store, manager)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/tasks/task-0/result")

    assert response.status_code == 200
    payload = response.json()
    assert payload["task_id"] == "task-0"
    assert payload["status"] == "READY_FOR_REVIEW"
    assert payload["plan"]["analysis"] == "Need review"
    assert payload["patch"]["patch_ops"][0]["op"] == "replace_range"
    assert payload["modified_files"] == ["src/main.py"]
    assert payload["diagnostics"][0]["source"] == "validator"
    assert payload["shadow_workspace_path"] is not None
    assert payload["step_progress"]["total_steps"] == 1
    assert payload["step_progress"]["completed_steps"] == 1
    assert payload["execution_trace"][0]["step_id"] == "S1"
    assert payload["plan_markdown"] is None


@pytest.mark.asyncio
async def test_get_task_result_returns_404_for_unknown_task(tmp_path: Path) -> None:
    store = InMemoryTaskStore()
    manager = ShadowWorkspaceManager(root_path=tmp_path / "shadows")
    app = _build_app(store, manager)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/tasks/missing/result")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_accept_promotes_and_returns_task_result(tmp_path: Path) -> None:
    store = InMemoryTaskStore()
    manager = ShadowWorkspaceManager(root_path=tmp_path / "shadows")

    real_workspace = tmp_path / "real"
    (real_workspace / "src").mkdir(parents=True)
    (real_workspace / "src/main.py").write_text("print('real')\n", encoding="utf-8")

    shadow = await manager.prepare("task-1", str(real_workspace))
    (shadow.shadow_path / "src/main.py").write_text("print('shadow')\n", encoding="utf-8")

    task = TaskRecord(
        task_id="task-1",
        goal="goal",
        workspace_path=str(real_workspace),
        shadow_workspace_path=str(shadow.shadow_path),
        status=TaskStatus.READY_FOR_REVIEW,
        plan=_sample_plan(),
        latest_patch=_sample_patch(),
        modified_files=["src/main.py"],
    )
    await store.create(task)

    orch = DummyOrchestrator()
    app = _build_app(store, manager, orch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/tasks/task-1/accept")

    assert response.status_code == 200
    payload = response.json()
    assert payload["task_id"] == "task-1"
    assert payload["status"] == "SUCCEEDED"
    assert payload["plan"]["analysis"] == "Need review"
    assert payload["patch"]["patch_ops"][0]["op"] == "replace_range"
    assert payload["promoted_at"] is not None

    assert (real_workspace / "src/main.py").read_text(encoding="utf-8") == "print('shadow')\n"
    assert task.shadow_workspace_path is None
    # The final decision leaves a durable transcript breadcrumb (ReviewCard is
    # live-slot only — without this the run summary vanishes from chat history).
    assert orch.breadcrumbs == ["✓ Task finished — 1 file(s) applied to the workspace."]


@pytest.mark.asyncio
async def test_reject_returns_task_result_and_aborts(tmp_path: Path) -> None:
    store = InMemoryTaskStore()
    manager = ShadowWorkspaceManager(root_path=tmp_path / "shadows")

    real_workspace = tmp_path / "real"
    (real_workspace / "src").mkdir(parents=True)
    (real_workspace / "src/main.py").write_text("print('real')\n", encoding="utf-8")

    shadow = await manager.prepare("task-2", str(real_workspace))

    task = TaskRecord(
        task_id="task-2",
        goal="goal",
        workspace_path=str(real_workspace),
        shadow_workspace_path=str(shadow.shadow_path),
        status=TaskStatus.READY_FOR_REVIEW,
        plan=_sample_plan(),
        latest_patch=_sample_patch(),
        modified_files=["src/main.py"],
    )
    await store.create(task)

    orch = DummyOrchestrator()
    app = _build_app(store, manager, orch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/tasks/task-2/reject", json={"reason": "nope"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["task_id"] == "task-2"
    assert payload["status"] == "ABORTED"
    assert payload["plan"]["analysis"] == "Need review"
    assert payload["patch"]["patch_ops"][0]["op"] == "replace_range"
    assert payload["shadow_workspace_path"] is None

    assert not shadow.shadow_path.exists()
    assert orch.breadcrumbs == [
        "✗ Task closed without finishing — applied changes kept; task marked aborted."
    ]


@pytest.mark.asyncio
async def test_get_task_result_returns_selected_patch_candidate(tmp_path: Path) -> None:
    store = InMemoryTaskStore()
    manager = ShadowWorkspaceManager(root_path=tmp_path / "shadows")

    real_workspace = tmp_path / "real"
    real_workspace.mkdir(parents=True)

    task = TaskRecord(
        task_id="task-v2",
        goal="goal",
        workspace_path=str(real_workspace),
        status=TaskStatus.READY_FOR_REVIEW,
        plan=_sample_plan(),
        latest_patch_v2=PatchDocumentV2.model_validate(
            {
                "candidates": [
                    {
                        "candidate_id": "c1",
                        "patch_ops": [
                            {
                                "op": "create_file",
                                "file": "src/a.py",
                                "content": "print('a')\n",
                                "reason": "create",
                            }
                        ],
                    },
                    {
                        "candidate_id": "c2",
                        "patch_ops": [
                            {
                                "op": "create_file",
                                "file": "src/b.py",
                                "content": "print('b')\n",
                                "reason": "create",
                            }
                        ],
                    },
                ]
            }
        ),
        selected_candidate_id="c2",
        modified_files=["src/b.py"],
    )
    await store.create(task)

    app = _build_app(store, manager)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/tasks/task-v2/result")

    assert response.status_code == 200
    payload = response.json()
    assert payload["selected_candidate_id"] == "c2"
    assert payload["patch"]["candidate_id"] == "c2"


@pytest.mark.asyncio
async def test_get_task_result_ignores_repair_steps_in_step_progress(tmp_path: Path) -> None:
    store = InMemoryTaskStore()
    manager = ShadowWorkspaceManager(root_path=tmp_path / "shadows")

    real_workspace = tmp_path / "real"
    real_workspace.mkdir(parents=True)

    task = TaskRecord(
        task_id="task-repair-progress",
        goal="goal",
        workspace_path=str(real_workspace),
        status=TaskStatus.READY_FOR_REVIEW,
        plan=_sample_plan(),
        completed_step_ids=["S1", "repair-full-validation"],
    )
    await store.create(task)

    app = _build_app(store, manager)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/tasks/task-repair-progress/result")

    assert response.status_code == 200
    payload = response.json()
    assert payload["step_progress"]["total_steps"] == 1
    assert payload["step_progress"]["completed_steps"] == 1
    assert payload["step_progress"]["remaining_steps"] == 0


@pytest.mark.asyncio
async def test_get_task_artifacts_lists_task_debug_files(tmp_path: Path) -> None:
    store = InMemoryTaskStore()
    manager = ShadowWorkspaceManager(root_path=tmp_path / "shadows")

    real_workspace = tmp_path / "real"
    real_workspace.mkdir(parents=True)

    artifacts_root = tmp_path / "artifacts" / "task-artifacts"
    (artifacts_root / "step-S1" / "attempt-1").mkdir(parents=True)
    (artifacts_root / "step-S1" / "attempt-1" / "ranking.json").write_text(
        "{}",
        encoding="utf-8",
    )
    (artifacts_root / "step-S1" / "attempt-1" / "preflight-c1.json").write_text(
        "{}",
        encoding="utf-8",
    )

    task = TaskRecord(
        task_id="task-artifacts",
        goal="goal",
        workspace_path=str(real_workspace),
        status=TaskStatus.READY_FOR_REVIEW,
        artifacts_root_path=str(artifacts_root),
    )
    await store.create(task)

    app = _build_app(store, manager)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/tasks/task-artifacts/artifacts")

    assert response.status_code == 200
    payload = response.json()
    assert payload["task_id"] == "task-artifacts"
    assert len(payload["entries"]) == 2
    assert payload["entries"][0]["step_id"] == "S1"
