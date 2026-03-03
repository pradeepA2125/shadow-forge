from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException

from agentd.domain.models import (
    Diagnostic,
    RejectPatchRequest,
    TaskCreateRequest,
    TaskCreateResponse,
    TaskRecord,
    TaskResult,
    TaskStatus,
    TaskView,
)
from agentd.domain.state_machine import transition
from agentd.orchestrator.engine import AgentOrchestrator
from agentd.storage.base import TaskStore
from agentd.workspace.shadow import ShadowWorkspaceManager


def _to_task_result(task: TaskRecord) -> TaskResult:
    return TaskResult(
        task_id=task.task_id,
        goal=task.goal,
        status=task.status,
        plan=task.plan,
        patch=task.latest_patch,
        modified_files=task.modified_files,
        diagnostics=task.diagnostics,
        promoted_at=task.promoted_at,
        shadow_workspace_path=task.shadow_workspace_path,
    )


def build_router(
    store: TaskStore,
    orchestrator: AgentOrchestrator,
    workspace_manager: ShadowWorkspaceManager,
) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["tasks"])

    @router.post("/tasks", response_model=TaskCreateResponse)
    async def create_task(request: TaskCreateRequest, background_tasks: BackgroundTasks) -> TaskCreateResponse:
        task_id = f"task-{uuid4()}"
        task = TaskRecord(
            task_id=task_id,
            goal=request.goal,
            workspace_path=request.workspace_path,
            mode=request.mode,
            budget=request.budget,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        await store.create(task)
        background_tasks.add_task(orchestrator.run_task, task_id)
        return TaskCreateResponse(task_id=task_id)

    @router.get("/tasks/{task_id}", response_model=TaskView)
    async def get_task(task_id: str) -> TaskView:
        try:
            task = await store.get(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        return TaskView(
            task_id=task.task_id,
            goal=task.goal,
            status=task.status,
            modified_files=task.modified_files,
            diagnostics=task.diagnostics,
        )

    @router.get("/tasks/{task_id}/result", response_model=TaskResult)
    async def get_task_result(task_id: str) -> TaskResult:
        try:
            task = await store.get(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        return _to_task_result(task)

    @router.post("/tasks/{task_id}/cancel", response_model=TaskView)
    async def cancel_task(task_id: str) -> TaskView:
        try:
            task = await store.get(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        if task.status not in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.ABORTED}:
            task = transition(task, TaskStatus.ABORTED, "cancelled by user")
        await workspace_manager.cleanup(task)
        task.shadow_workspace_path = None
        await store.save(task)

        return TaskView(
            task_id=task.task_id,
            goal=task.goal,
            status=task.status,
            modified_files=task.modified_files,
            diagnostics=task.diagnostics,
        )

    @router.post("/tasks/{task_id}/accept", response_model=TaskResult)
    async def accept_patch(task_id: str) -> TaskResult:
        try:
            task = await store.get(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if task.status != TaskStatus.READY_FOR_REVIEW:
            msg = f"Task {task_id} is not in READY_FOR_REVIEW state"
            raise HTTPException(status_code=409, detail=msg)

        task = transition(task, TaskStatus.PROMOTING, "promotion started")
        await store.save(task)

        try:
            await workspace_manager.promote(task)
            await workspace_manager.cleanup(task)
        except Exception as exc:
            task.diagnostics.append(
                Diagnostic(source="promotion", message=str(exc), level="error")
            )
            task = transition(task, TaskStatus.FAILED, "promotion failed")
            await store.save(task)
            raise HTTPException(status_code=500, detail=f"Promotion failed: {exc}") from exc

        task.shadow_workspace_path = None
        task.promoted_at = datetime.now(timezone.utc)
        task = transition(task, TaskStatus.SUCCEEDED, "promotion completed")
        await store.save(task)

        return _to_task_result(task)

    @router.post("/tasks/{task_id}/reject", response_model=TaskResult)
    async def reject_patch(task_id: str, request: RejectPatchRequest) -> TaskResult:
        try:
            task = await store.get(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        if task.status != TaskStatus.READY_FOR_REVIEW:
            msg = f"Task {task_id} is not in READY_FOR_REVIEW state"
            raise HTTPException(status_code=409, detail=msg)

        await workspace_manager.cleanup(task)
        task.shadow_workspace_path = None
        task = transition(task, TaskStatus.ABORTED, f"patch rejected: {request.reason}")
        await store.save(task)

        return _to_task_result(task)

    return router
