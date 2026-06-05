from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agentd.api.routes import build_router
from agentd.domain.models import Diagnostic, TaskRecord, TaskStatus, ValidationResult
from agentd.orchestrator.engine import AgentOrchestrator
from agentd.patch.engine import PatchEngine
from agentd.storage.in_memory import InMemoryTaskStore
from agentd.workspace.shadow import ShadowWorkspaceManager


def _extract_feedback_from_history(history: list[dict[str, object]]) -> str | None:
    """Recover the raw feedback text from the appended user turn the orchestrator adds.

    Mirrors AgentOrchestrator._format_feedback_turn — the contract is that plan
    feedback travels as the final conversation turn, not as a payload field.
    """
    marker = "gave this feedback:\n\n"
    for msg in reversed(history):
        if msg.get("role") != "user":
            continue
        content = str(msg.get("content", ""))
        if marker in content:
            # Feedback runs from the marker to the next blank line; the current-plan
            # block (when embedded) follows that, so stop at the first "\n\n".
            return content.split(marker, 1)[1].split("\n\n", 1)[0].strip()
    return None


class SpecFirstReasoner:
    def __init__(self) -> None:
        self.markdown_feedbacks: list[str | None] = []

    async def create_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        retrieval_context: dict[str, object],
        on_thinking: object = None,
    ) -> object:
        _ = (task, workspace_path, retrieval_context, on_thinking)
        return {
            "analysis": "Create a generated file for verification.",
            "steps": [
                {
                    "id": "S1",
                    "goal": "Create generated file",
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
        _ = (task, workspace_path, diagnostics, retrieval_context, kwargs)
        return {
            "candidates": [
                {
                    "candidate_id": "c1",
                    "patch_ops": [
                        {
                            "op": "create_file",
                            "file": "generated.txt",
                            "content": "ok\n",
                            "reason": "create generated file",
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
        return {
            "type": "emit_patch",
            "thought": "scripted",
            "patch_ops": [
                {
                    "op": "create_file",
                    "file": "generated.txt",
                    "content": "ok\n",
                    "reason": "create generated file",
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
        _ = (plan_context, tool_definitions)
        # Feedback now arrives as the final appended turn of the planning conversation
        # (not as initial_context["plan_feedback"]) — recover it from there.
        feedback = _extract_feedback_from_history(history)
        if isinstance(feedback, str) and feedback.strip():
            self.markdown_feedbacks.append(feedback)
            return {
                "type": "emit_plan",
                "thought": "revised with feedback",
                "plan_markdown": f"# Revised Plan\n\n- {feedback}",
                "files_examined": [],
                "confidence": "high",
            }
        self.markdown_feedbacks.append(None)
        return {
            "type": "emit_plan",
            "thought": "stub: planning agent bypassed",
            "plan_markdown": "# Initial Plan\n\n- Create generated file",
            "files_examined": [],
            "confidence": "high",
        }


class AutoCritiqueReasoner(SpecFirstReasoner):
    # The auto-critique loop (create_markdown_plan → critique_markdown_plan → revise)
    # is replaced by the PlanningAgent's explore-then-commit loop. The agent is
    # expected to produce the correct plan in one shot via create_planning_step().

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
            "thought": "planning agent explored workspace and identified correct file",
            "plan_markdown": "# Revised Plan\n\n- Update `services/agentd-py/agentd/api/routes.py`",
            "files_examined": ["services/agentd-py/agentd/api/routes.py"],
            "confidence": "high",
        }


class AlwaysPassValidator:
    async def run_touched(self, workspace_path: str, touched_files: list[str]) -> ValidationResult:
        _ = (workspace_path, touched_files)
        return ValidationResult(success=True, diagnostics=[], duration_ms=1)

    async def run(self, workspace_path: str) -> ValidationResult:
        _ = workspace_path
        return ValidationResult(success=True, diagnostics=[], duration_ms=1)


def _build_app(
    store: InMemoryTaskStore,
    orchestrator: AgentOrchestrator,
    workspace_manager: ShadowWorkspaceManager,
) -> FastAPI:
    app = FastAPI()
    app.include_router(build_router(store, orchestrator, workspace_manager))
    return app


async def _wait_for_status(
    client: AsyncClient,
    task_id: str,
    expected_status: str,
    *,
    timeout_sec: float = 2.0,
) -> dict[str, object]:
    deadline = asyncio.get_event_loop().time() + timeout_sec
    last_payload: dict[str, object] | None = None
    while asyncio.get_event_loop().time() < deadline:
        response = await client.get(f"/v1/tasks/{task_id}")
        assert response.status_code == 200
        payload = response.json()
        last_payload = payload
        if payload["status"] == expected_status:
            return payload
        if payload["status"] in {"FAILED", "ABORTED"}:
            pytest.fail(f"Task {task_id} terminated unexpectedly: {payload}")
        await asyncio.sleep(0.01)
    pytest.fail(f"Timed out waiting for status {expected_status}: last payload={last_payload}")


async def _wait_for_plan_markdown(
    client: AsyncClient,
    task_id: str,
    expected_fragment: str,
    *,
    timeout_sec: float = 2.0,
) -> dict[str, object]:
    deadline = asyncio.get_event_loop().time() + timeout_sec
    last_payload: dict[str, object] | None = None
    while asyncio.get_event_loop().time() < deadline:
        response = await client.get(f"/v1/tasks/{task_id}")
        assert response.status_code == 200
        payload = response.json()
        last_payload = payload
        if expected_fragment in str(payload.get("plan_markdown", "")):
            return payload
        if payload["status"] in {"FAILED", "ABORTED"}:
            pytest.fail(f"Task {task_id} terminated unexpectedly: {payload}")
        await asyncio.sleep(0.01)
    pytest.fail(f"Timed out waiting for plan fragment {expected_fragment}: last payload={last_payload}")


@pytest.mark.asyncio
async def test_create_task_reaches_plan_approval_and_returns_plan_markdown(tmp_path: Path) -> None:
    store = InMemoryTaskStore()
    workspace_manager = ShadowWorkspaceManager(root_path=tmp_path / "shadows")
    orchestrator = AgentOrchestrator(
        store=store,
        reasoning_engine=SpecFirstReasoner(),
        validator=AlwaysPassValidator(),
        patch_engine=PatchEngine(),
        workspace_manager=workspace_manager,
    )
    app = _build_app(store, orchestrator, workspace_manager)

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/tasks",
            json={
                "goal": "Implement GET /v1/tasks/{task_id}/events",
                "workspace_path": str(workspace),
                "mode": "project_edit",
            },
        )

        assert response.status_code == 200
        task_id = response.json()["task_id"]
        payload = await _wait_for_status(client, task_id, "AWAITING_PLAN_APPROVAL")

    assert payload["plan_markdown"] == "# Initial Plan\n\n- Create generated file"


@pytest.mark.asyncio
async def test_plan_feedback_regenerates_markdown_plan(tmp_path: Path) -> None:
    store = InMemoryTaskStore()
    reasoner = SpecFirstReasoner()
    workspace_manager = ShadowWorkspaceManager(root_path=tmp_path / "shadows")
    orchestrator = AgentOrchestrator(
        store=store,
        reasoning_engine=reasoner,
        validator=AlwaysPassValidator(),
        patch_engine=PatchEngine(),
        workspace_manager=workspace_manager,
    )
    app = _build_app(store, orchestrator, workspace_manager)

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        create_response = await client.post(
            "/v1/tasks",
            json={
                "goal": "Implement GET /v1/tasks/{task_id}/events",
                "workspace_path": str(workspace),
                "mode": "project_edit",
            },
        )
        task_id = create_response.json()["task_id"]
        await _wait_for_status(client, task_id, "AWAITING_PLAN_APPROVAL")

        feedback_response = await client.post(
            f"/v1/tasks/{task_id}/plan/feedback",
            json={"feedback": "Please add a response model instead of returning a raw list."},
        )
        assert feedback_response.status_code == 200

        payload = await _wait_for_plan_markdown(client, task_id, "# Revised Plan")

    assert "response model" in str(payload["plan_markdown"]).lower()
    assert reasoner.markdown_feedbacks[-1] == "Please add a response model instead of returning a raw list."


@pytest.mark.asyncio
async def test_create_task_auto_critiques_markdown_before_approval(tmp_path: Path) -> None:
    store = InMemoryTaskStore()
    reasoner = AutoCritiqueReasoner()
    workspace_manager = ShadowWorkspaceManager(root_path=tmp_path / "shadows")
    orchestrator = AgentOrchestrator(
        store=store,
        reasoning_engine=reasoner,
        validator=AlwaysPassValidator(),
        patch_engine=PatchEngine(),
        workspace_manager=workspace_manager,
    )
    app = _build_app(store, orchestrator, workspace_manager)

    workspace = tmp_path / "workspace"
    (workspace / "services/agentd-py/agentd/api").mkdir(parents=True)
    (workspace / "services/agentd-py/agentd/api/routes.py").write_text("router = object()\n", encoding="utf-8")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/tasks",
            json={
                "goal": "Implement GET /v1/tasks/{task_id}/events",
                "workspace_path": str(workspace),
                "mode": "project_edit",
            },
        )

        assert response.status_code == 200
        task_id = response.json()["task_id"]
        payload = await _wait_for_status(client, task_id, "AWAITING_PLAN_APPROVAL")

    assert payload["plan_markdown"] == "# Revised Plan\n\n- Update `services/agentd-py/agentd/api/routes.py`"


@pytest.mark.asyncio
async def test_plan_feedback_null_continues_task_to_ready_for_review(tmp_path: Path) -> None:
    store = InMemoryTaskStore()
    workspace_manager = ShadowWorkspaceManager(root_path=tmp_path / "shadows")
    orchestrator = AgentOrchestrator(
        store=store,
        reasoning_engine=SpecFirstReasoner(),
        validator=AlwaysPassValidator(),
        patch_engine=PatchEngine(),
        workspace_manager=workspace_manager,
    )
    app = _build_app(store, orchestrator, workspace_manager)

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        create_response = await client.post(
            "/v1/tasks",
            json={
                "goal": "Implement GET /v1/tasks/{task_id}/events",
                "workspace_path": str(workspace),
                "mode": "project_edit",
            },
        )
        task_id = create_response.json()["task_id"]
        await _wait_for_status(client, task_id, "AWAITING_PLAN_APPROVAL")

        approve_response = await client.post(
            f"/v1/tasks/{task_id}/plan/feedback",
            json={"feedback": None},
        )
        assert approve_response.status_code == 200

        task_payload = await _wait_for_status(client, task_id, "READY_FOR_REVIEW")
        result_response = await client.get(f"/v1/tasks/{task_id}/result")

    assert task_payload["status"] == "READY_FOR_REVIEW"
    assert result_response.status_code == 200
    result_payload = result_response.json()
    assert result_payload["plan_markdown"] == "# Initial Plan\n\n- Create generated file"
    assert result_payload["modified_files"] == ["generated.txt"]


@pytest.mark.asyncio
async def test_plan_feedback_returns_400_when_task_not_awaiting_approval(tmp_path: Path) -> None:
    store = InMemoryTaskStore()
    workspace_manager = ShadowWorkspaceManager(root_path=tmp_path / "shadows")
    orchestrator = AgentOrchestrator(
        store=store,
        reasoning_engine=SpecFirstReasoner(),
        validator=AlwaysPassValidator(),
        patch_engine=PatchEngine(),
        workspace_manager=workspace_manager,
    )
    app = _build_app(store, orchestrator, workspace_manager)

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    await store.create(
        TaskRecord(
            task_id="task-locked",
            goal="goal",
            workspace_path=str(workspace),
            status=TaskStatus.READY_FOR_REVIEW,
        )
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/tasks/task-locked/plan/feedback",
            json={"feedback": "Please regenerate"},
        )

    assert response.status_code == 409  # task not in AWAITING_PLAN_APPROVAL state
