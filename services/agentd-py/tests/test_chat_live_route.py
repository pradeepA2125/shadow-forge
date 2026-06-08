"""Task 5: GET /v1/chat/threads/{id}/live serves the thread's live state."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agentd.api.routes import build_router
from agentd.chat.agent import ChatAgent
from agentd.chat.storage import ChatThreadStore
from agentd.domain.models import (
    CommandApprovalRequest,
    TaskExecutionState,
    TaskRecord,
    TaskStatus,
    ValidationResult,
)
from agentd.orchestrator.engine import AgentOrchestrator
from agentd.patch.engine import PatchEngine
from agentd.storage.in_memory import InMemoryTaskStore
from agentd.workspace.shadow import ShadowWorkspaceManager


class _NoopReasoning:
    async def create_plan(self, *a, **k): raise NotImplementedError
    async def create_patch(self, *a, **k): raise NotImplementedError
    async def create_tool_step(self, *a, **k): raise NotImplementedError
    async def create_planning_step(self, *a, **k): raise NotImplementedError


class _NullTransport:
    async def generate_text(self, **_) -> str:
        return "x"

    async def generate_json(self, *, schema_name, **_) -> dict:
        return {"intent": "qa", "rationale": "", "likely_targets": []}


class _Validator:
    async def run(self, workspace_path) -> ValidationResult:
        return ValidationResult(success=True, diagnostics=[], duration_ms=1)


def _build(tmp_path: Path):
    store = InMemoryTaskStore()
    ws_manager = ShadowWorkspaceManager(tmp_path / "shadows")
    chat_store = ChatThreadStore(tmp_path / "chat.db")
    orch = AgentOrchestrator(
        store=store,
        reasoning_engine=_NoopReasoning(),
        validator=_Validator(),
        patch_engine=PatchEngine(),
        workspace_manager=ws_manager,
        chat_store=chat_store,
    )
    agent = ChatAgent(
        workspace_path=str(tmp_path),
        transport=_NullTransport(),
        model="test-model",
        thread_store=chat_store,
        orchestrator=orch,
        broadcaster=orch.broadcaster,
    )
    app = FastAPI()
    app.include_router(build_router(store, orch, ws_manager, None, agent))
    return app, store, chat_store


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_unknown_thread_returns_404(tmp_path: Path) -> None:
    app, _store, _chat = _build(tmp_path)
    async with _client(app) as client:
        resp = await client.get("/v1/chat/threads/ghost/live")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_no_active_task_returns_nulls(tmp_path: Path) -> None:
    app, _store, chat_store = _build(tmp_path)
    thread = chat_store.create_thread(str(tmp_path))
    async with _client(app) as client:
        resp = await client.get(f"/v1/chat/threads/{thread.thread_id}/live")
    assert resp.status_code == 200
    body = resp.json()
    assert body["active_task_id"] is None
    assert body["status"] is None
    assert body["pending_gate"] is None
    assert body["plan"] is None


@pytest.mark.asyncio
async def test_pruned_active_task_returns_nulls(tmp_path: Path) -> None:
    app, _store, chat_store = _build(tmp_path)
    thread = chat_store.create_thread(str(tmp_path))
    chat_store.set_active_task(thread.thread_id, "task-gone")  # never created in the store
    async with _client(app) as client:
        resp = await client.get(f"/v1/chat/threads/{thread.thread_id}/live")
    assert resp.status_code == 200
    assert resp.json()["active_task_id"] is None


@pytest.mark.asyncio
async def test_command_gate_surfaced(tmp_path: Path) -> None:
    app, store, chat_store = _build(tmp_path)
    thread = chat_store.create_thread(str(tmp_path))
    task = TaskRecord(
        task_id="task-1",
        goal="g",
        workspace_path=str(tmp_path),
        status=TaskStatus.AWAITING_COMMAND_DECISION,
        execution_state=TaskExecutionState(
            pending_command_request=CommandApprovalRequest(
                decision_id="d1", command="pytest", args=["-x"], cwd=".", step_id="s1"
            )
        ),
    )
    await store.create(task)
    chat_store.set_active_task(thread.thread_id, task.task_id)

    async with _client(app) as client:
        resp = await client.get(f"/v1/chat/threads/{thread.thread_id}/live")

    assert resp.status_code == 200
    body = resp.json()
    assert body["active_task_id"] == "task-1"
    assert body["status"] == "AWAITING_COMMAND_DECISION"
    assert body["pending_gate"]["kind"] == "command"
    assert body["pending_gate"]["payload"]["command"] == "pytest"


@pytest.mark.asyncio
async def test_plan_surfaced_at_approval(tmp_path: Path) -> None:
    app, store, chat_store = _build(tmp_path)
    thread = chat_store.create_thread(str(tmp_path))
    task = TaskRecord(
        task_id="task-2",
        goal="g",
        workspace_path=str(tmp_path),
        status=TaskStatus.AWAITING_PLAN_APPROVAL,
        plan_markdown="# Plan\n- do it",
    )
    await store.create(task)
    chat_store.set_active_task(thread.thread_id, task.task_id)

    async with _client(app) as client:
        resp = await client.get(f"/v1/chat/threads/{thread.thread_id}/live")

    body = resp.json()
    assert body["pending_gate"] is None
    assert body["plan"]["plan_markdown"] == "# Plan\n- do it"
    assert body["plan"]["task_id"] == "task-2"
