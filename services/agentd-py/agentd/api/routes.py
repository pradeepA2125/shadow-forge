from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from agentd.domain.models import (
    Diagnostic,
    PlanFeedbackRequest,
    RejectPatchRequest,
    ResumeTaskRequest,
    ResumeTaskResponse,
    ScopeDecisionRequest,
    ScopeDecisionResponse,
    CommandDecision,
    CommandDecisionResponse,
    ValidationDecisionRequest,
    ValidationDecisionResponse,
    StepDecisionRequest,
    StepProgress,
    TaskArtifactEntry,
    TaskArtifactsResponse,
    TaskBudget,
    TaskCreateRequest,
    TaskCreateResponse,
    TaskEvent,
    TaskRecord,
    TaskResult,
    TaskStatus,
    TaskView,
)
from agentd.domain.state_machine import transition
from agentd.orchestrator.engine import AgentOrchestrator
from agentd.retrieval.artifact_client import RetrievalArtifactClient
from agentd.storage.base import TaskStore
from agentd.workspace.shadow import ShadowWorkspaceManager


def _to_task_result(task: TaskRecord) -> TaskResult:
    selected_patch = None
    patch_candidates = []
    if task.latest_patch_v2:
        patch_candidates = [*task.latest_patch_v2.candidates]
        if task.selected_candidate_id:
            selected_patch = next(
                (
                    item
                    for item in patch_candidates
                    if item.candidate_id == task.selected_candidate_id
                ),
                None,
            )
        if selected_patch is None and patch_candidates:
            selected_patch = patch_candidates[0]
    elif task.latest_patch:
        selected_patch = task.latest_patch

    plan_step_ids = [step.id for step in task.plan.steps] if task.plan else []
    completed_plan_step_ids = [
        step_id for step_id in task.completed_step_ids if step_id in plan_step_ids
    ]
    total_steps = len(plan_step_ids)
    completed_steps = len(completed_plan_step_ids)
    current_step_id: str | None = None
    if task.plan:
        for step in task.plan.steps:
            if step.id not in completed_plan_step_ids:
                current_step_id = step.id
                break

    step_progress = (
        StepProgress(
            total_steps=total_steps,
            completed_steps=completed_steps,
            remaining_steps=max(total_steps - completed_steps, 0),
            current_step_id=current_step_id,
        )
        if task.plan
        else None
    )

    return TaskResult(
        task_id=task.task_id,
        goal=task.goal,
        status=task.status,
        plan=task.plan,
        patch=selected_patch,
        patch_candidates=patch_candidates,
        selected_candidate_id=task.selected_candidate_id,
        modified_files=task.modified_files,
        diagnostics=task.diagnostics,
        promoted_at=task.promoted_at,
        shadow_workspace_path=task.shadow_workspace_path,
        step_progress=step_progress,
        execution_trace=task.execution_trace[-50:],
        artifacts_root_path=task.artifacts_root_path,
        plan_markdown=task.plan_markdown,
        resume_of_task_id=task.resume_of_task_id,
    )


def _list_task_artifacts(task: TaskRecord) -> list[TaskArtifactEntry]:
    root_value = task.artifacts_root_path
    if not root_value:
        return []

    root = Path(root_value)
    if not root.exists() or not root.is_dir():
        return []

    entries: list[TaskArtifactEntry] = []
    for file_path in sorted(path for path in root.rglob("*") if path.is_file()):
        relative = file_path.relative_to(root).as_posix()
        parts = relative.split("/")
        step_id: str | None = None
        attempt: int | None = None
        for part in parts:
            if part.startswith("step-"):
                step_id = part.removeprefix("step-")
            elif part.startswith("attempt-"):
                try:
                    attempt = int(part.removeprefix("attempt-"))
                except ValueError:
                    attempt = None

        name = file_path.name.lower()
        kind = "other"
        if "checkpoint" in name:
            kind = "checkpoint"
        elif "preflight" in name:
            kind = "preflight"
        elif "validation" in name:
            kind = "validation"
        elif "ranking" in name:
            kind = "ranking"
        elif "plan" in name:
            kind = "plan"
        elif "patch" in name:
            kind = "patch"

        candidate_id: str | None = None
        if "preflight-" in name:
            candidate_id = name.split("preflight-", maxsplit=1)[1].removesuffix(".json")
        elif "validation-" in name:
            candidate_id = name.split("validation-", maxsplit=1)[1].removesuffix(".json")

        entries.append(
            TaskArtifactEntry(
                relative_path=relative,
                kind=kind,  # type: ignore[arg-type]
                step_id=step_id,
                attempt=attempt,
                candidate_id=candidate_id,
            )
        )
    return entries


def build_router(
    store: TaskStore,
    orchestrator: AgentOrchestrator,
    workspace_manager: ShadowWorkspaceManager,
    retrieval_client: RetrievalArtifactClient | None = None,
    chat_agent: object | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["tasks"])

    # Guards against two concurrent plan-feedback calls for the same task.
    # Safe without a lock: asyncio is single-threaded; the check+add happens
    # with no await in between, so no other coroutine can interleave.
    _in_flight_feedback: set[str] = set()

    # Guards against concurrent resume calls for the same PARENT task_id.
    _in_flight_resume: set[str] = set()

    # Guards against concurrent scope-decision posts for the same task_id.
    _in_flight_scope: set[str] = set()
    _in_flight_validation: set[str] = set()
    _in_flight_command: set[str] = set()

    @router.get("/workspaces/env-profile")
    async def get_env_profile(workspace: str) -> dict:
        from agentd.env.profile_store import EnvProfileStore
        ws = Path(workspace)
        if not ws.is_dir():
            raise HTTPException(
                status_code=400, detail=f"workspace not a directory: {workspace}"
            )
        profile = EnvProfileStore().read(ws)
        if profile is None:
            raise HTTPException(status_code=404, detail="env profile not built")
        return json.loads(profile.model_dump_json())

    @router.post("/workspaces/env-profile")
    async def build_env_profile(workspace: str, channel_id: str | None = None) -> dict:
        """Force a rebuild of the workspace env profile.

        Uses the orchestrator's ensurer so SSE events (env_profile_building /
        env_profile_built) fire just like a task-triggered build. When
        channel_id is supplied, events route there; otherwise they land on the
        workspace path (no UI subscriber by default).
        """
        from agentd.env.profile_store import EnvProfileStore
        ws = Path(workspace)
        if not ws.is_dir():
            raise HTTPException(
                status_code=400, detail=f"workspace not a directory: {workspace}"
            )
        # Wipe the existing profile so ensure()'s is_stale check doesn't short-circuit.
        store = EnvProfileStore()
        profile_path = store.path_for(ws)
        if profile_path.is_file():
            profile_path.unlink()
        await orchestrator._env_ensurer.ensure(ws, channel_id=channel_id)
        profile = store.read(ws)
        if profile is None:
            raise HTTPException(status_code=500, detail="env profile build failed")
        return json.loads(profile.model_dump_json())

    @router.post("/tasks", response_model=TaskCreateResponse)
    async def create_task(request: TaskCreateRequest) -> TaskCreateResponse:
        import asyncio
        task_id = f"task-{uuid4()}"
        # Per-task override > AI_EDITOR_STEP_REVIEW_AUTO_ACCEPT env > default True.
        _env_step_review_default = os.environ.get(
            "AI_EDITOR_STEP_REVIEW_AUTO_ACCEPT", "true",
        ).strip().lower() not in ("0", "false", "no", "off")
        step_review = (
            request.step_review_auto_accept
            if request.step_review_auto_accept is not None
            else _env_step_review_default
        )
        task = TaskRecord(
            task_id=task_id,
            goal=request.goal,
            workspace_path=request.workspace_path,
            mode=request.mode,
            budget=request.budget,
            step_review_auto_accept=step_review,
            shell_policy=request.shell_policy,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        await store.create(task)
        asyncio.create_task(orchestrator.run_task(task_id))
        return TaskCreateResponse(task_id=task_id)

    @router.post("/tasks/{task_id}/plan/feedback", response_model=TaskView)
    async def provide_plan_feedback(
        task_id: str,
        request: PlanFeedbackRequest,
    ) -> TaskView:
        import asyncio

        # Reject duplicate concurrent calls before any await — safe because there
        # is no await between the check and the add in asyncio's cooperative model.
        if task_id in _in_flight_feedback:
            raise HTTPException(
                status_code=409,
                detail=f"Task {task_id} already has a plan-feedback call in progress",
            )
        _in_flight_feedback.add(task_id)

        try:
            task = await store.get(task_id)
        except KeyError as exc:
            _in_flight_feedback.discard(task_id)
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        if task.status != TaskStatus.AWAITING_PLAN_APPROVAL:
            _in_flight_feedback.discard(task_id)
            raise HTTPException(
                status_code=409,
                detail=f"Task {task_id} is not awaiting plan approval (status={task.status})",
            )

        # Mark running before create_task so SSE stream doesn't see the task as idle
        # during the race window between this return and continue_task's own add().
        orchestrator._running_tasks.add(task_id)

        async def _run_and_release() -> None:
            try:
                await orchestrator.continue_task(task_id, feedback=request.feedback)
            finally:
                orchestrator._running_tasks.discard(task_id)
                _in_flight_feedback.discard(task_id)

        asyncio.create_task(_run_and_release())

        return TaskView(
            task_id=task.task_id,
            goal=task.goal,
            status=task.status,
            modified_files=task.modified_files,
            diagnostics=task.diagnostics,
            plan_markdown=task.plan_markdown,
        )

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
            plan_markdown=task.plan_markdown,
            resume_of_task_id=task.resume_of_task_id,
        )

    @router.get("/tasks/{task_id}/result", response_model=TaskResult)
    async def get_task_result(task_id: str) -> TaskResult:
        try:
            task = await store.get(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        return _to_task_result(task)

    @router.get("/tasks/{task_id}/events", response_model=list[TaskEvent])
    async def get_task_events_route(task_id: str) -> list[TaskEvent]:
        try:
            return await store.get_task_events(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/channels/{channel_id}/stream")
    async def stream_channel(channel_id: str) -> StreamingResponse:
        """Permissive SSE: subscribe to any broadcaster channel without
        requiring it to be a task_id. Used by env-profile route callers
        and other admin/dev tooling that need to observe workspace-level
        SSE events (env_profile_*) before any task exists."""

        async def event_generator():
            queue = orchestrator.broadcaster.subscribe(channel_id)
            try:
                while not queue.empty():
                    event = queue.get_nowait()
                    yield f"data: {json.dumps(event)}\n\n"
                    if event.get("type") == "done":
                        return
                while True:
                    event = await queue.get()
                    yield f"data: {json.dumps(event)}\n\n"
                    if event.get("type") == "done":
                        return
            finally:
                orchestrator.broadcaster.unsubscribe(channel_id, queue)

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @router.get("/tasks/{task_id}/stream-patch")
    async def stream_task_patches(task_id: str) -> StreamingResponse:
        try:
            await store.get(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        # Statuses that will never produce more patch events — safe to close immediately.
        # AWAITING_PLAN_APPROVAL is intentionally excluded: right after plan approval the
        # task status is still AWAITING_PLAN_APPROVAL in the DB while continue_task is
        # starting in the background. Closing early here would miss all patch events.
        _DONE_STATUSES = {
            TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.ABORTED,
            TaskStatus.READY_FOR_REVIEW,
        }

        async def event_generator():
            import asyncio

            queue = orchestrator.broadcaster.subscribe(task_id)
            try:
                # Drain replay buffer first so late-connecting clients get history
                while not queue.empty():
                    event = queue.get_nowait()
                    yield f"data: {json.dumps(event)}\n\n"
                    if event.get("type") == "done":
                        return

                # Only close immediately if the task is definitively done or waiting for
                # user input. Do NOT close just because _running_tasks is empty — there is
                # a race window between the route returning and the background coroutine
                # calling _running_tasks.add(), e.g. right after plan approval.
                if task_id not in orchestrator._running_tasks:
                    task = await store.get(task_id)
                    if task.status in _DONE_STATUSES:
                        yield f"data: {json.dumps({'type': 'done'})}\n\n"
                        return

                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        # Keep-alive ping — prevents Node.js fetch (undici) from
                        # dropping the connection during the silent gap while the
                        # model generates a large plan (no task-channel events for
                        # minutes), which would otherwise lose the
                        # AWAITING_PLAN_APPROVAL/plan event on slow inference.
                        yield ": ping\n\n"
                        continue
                    yield f"data: {json.dumps(event)}\n\n"
                    if event.get("type") == "done":
                        break
            finally:
                orchestrator.broadcaster.unsubscribe(task_id, queue)

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @router.get("/tasks/{task_id}/artifacts", response_model=TaskArtifactsResponse)
    async def get_task_artifacts(task_id: str) -> TaskArtifactsResponse:
        try:
            task = await store.get(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        return TaskArtifactsResponse(
            task_id=task.task_id,
            artifacts_root_path=task.artifacts_root_path,
            entries=_list_task_artifacts(task),
        )

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
        await workspace_manager.prune_checkpoints()

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
        task.promoted_at = datetime.now(UTC)
        task = transition(task, TaskStatus.SUCCEEDED, "promotion completed")
        await store.save(task)
        await workspace_manager.prune_checkpoints()

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
        await workspace_manager.prune_checkpoints()

        return _to_task_result(task)

    @router.post("/tasks/{task_id}/scope-decision", response_model=ScopeDecisionResponse)
    async def post_scope_decision(
        task_id: str, request: ScopeDecisionRequest,
    ) -> ScopeDecisionResponse:
        from agentd.tools.loop import ScopeDecision

        if task_id in _in_flight_scope:
            raise HTTPException(
                status_code=409,
                detail=f"Scope decision already in progress for task {task_id}",
            )
        _in_flight_scope.add(task_id)

        try:
            try:
                task = await store.get(task_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

            if task.status != TaskStatus.AWAITING_SCOPE_DECISION:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Task {task_id} is not awaiting scope decision "
                        f"(status={task.status})"
                    ),
                )

            future = orchestrator._pending_scope_decisions.get(task_id)
            if future is None or future.done():
                raise HTTPException(
                    status_code=409, detail="No pending scope decision for this task",
                )

            pending = task.execution_state.pending_scope_request
            if pending is None:
                raise HTTPException(
                    status_code=409, detail="Task has no pending scope request payload",
                )

            if request.decision == "approve":
                approved_files = request.files or list(pending.files)
                extra = set(approved_files) - set(pending.files)
                if extra:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "Cannot approve files that were not in the original request: "
                            f"{sorted(extra)}"
                        ),
                    )
                decision = ScopeDecision(
                    approve=True,
                    extended_files=approved_files,
                    reason="user approved",
                    remember=request.remember,
                )
            else:
                decision = ScopeDecision(
                    approve=False, extended_files=[], reason="user rejected",
                )
            future.set_result(decision)

            return ScopeDecisionResponse(task_id=task_id, status=TaskStatus.EXECUTING)
        finally:
            _in_flight_scope.discard(task_id)

    @router.post("/tasks/{task_id}/validation-decision", response_model=ValidationDecisionResponse)
    async def post_validation_decision(
        task_id: str, request: ValidationDecisionRequest,
    ) -> ValidationDecisionResponse:
        if task_id in _in_flight_validation:
            raise HTTPException(
                status_code=409,
                detail=f"Validation decision already in progress for task {task_id}",
            )
        _in_flight_validation.add(task_id)
        try:
            try:
                task = await store.get(task_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

            if task.status != TaskStatus.AWAITING_VALIDATION_DECISION:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Task {task_id} is not awaiting a validation decision "
                        f"(status={task.status})"
                    ),
                )

            future = orchestrator._pending_validation_decisions.get(task_id)
            if future is None or future.done():
                raise HTTPException(
                    status_code=409, detail="No pending validation decision for this task",
                )

            future.set_result(request.decision == "accept")
            # The orchestrator coroutine drives the resulting transition (VALIDATED→
            # READY_FOR_REVIEW on accept, FAILED on reject); report the gate status here.
            return ValidationDecisionResponse(
                task_id=task_id, status=TaskStatus.AWAITING_VALIDATION_DECISION,
            )
        finally:
            _in_flight_validation.discard(task_id)

    @router.post("/tasks/{task_id}/command-decision", response_model=CommandDecisionResponse)
    async def post_command_decision(
        task_id: str, request: CommandDecision,
    ) -> CommandDecisionResponse:
        if task_id in _in_flight_command:
            raise HTTPException(
                status_code=409,
                detail=f"Command decision already in progress for task {task_id}",
            )
        _in_flight_command.add(task_id)
        try:
            try:
                task = await store.get(task_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

            if task.status != TaskStatus.AWAITING_COMMAND_DECISION:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Task {task_id} is not awaiting a command decision "
                        f"(status={task.status})"
                    ),
                )

            future = orchestrator._pending_command_decisions.get(task_id)
            if future is None or future.done():
                raise HTTPException(
                    status_code=409,
                    detail="No pending command decision for this task",
                )

            future.set_result(request)
            # The orchestrator coroutine drives the resulting transition back
            # to EXECUTING; report the post-decision status here.
            return CommandDecisionResponse(task_id=task_id, status=TaskStatus.EXECUTING)
        finally:
            _in_flight_command.discard(task_id)

    _in_flight_step_decision: set[str] = set()

    @router.post("/tasks/{task_id}/step-decision")
    async def post_step_decision(task_id: str, request: StepDecisionRequest) -> dict:
        if task_id in _in_flight_step_decision:
            raise HTTPException(
                status_code=409,
                detail=f"Step decision already in progress for task {task_id}",
            )
        _in_flight_step_decision.add(task_id)
        try:
            try:
                task = await store.get(task_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

            if task.status != TaskStatus.AWAITING_STEP_REVIEW:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Task {task_id} is not awaiting step review "
                        f"(status={task.status})"
                    ),
                )

            future = orchestrator._pending_step_decisions.get(task_id)
            if future is None or future.done():
                raise HTTPException(
                    status_code=409, detail="No pending step review for this task",
                )

            future.set_result(request.decision)
            return {"ok": True}
        finally:
            _in_flight_step_decision.discard(task_id)

    @router.post("/tasks/{task_id}/resume", response_model=ResumeTaskResponse)
    async def resume_task_route(task_id: str, request: ResumeTaskRequest) -> ResumeTaskResponse:
        import asyncio
        from uuid import uuid4

        # No-await check+add: safe in asyncio's cooperative concurrency model
        if task_id in _in_flight_resume:
            raise HTTPException(
                status_code=409,
                detail=f"Resume already in progress for task {task_id}",
            )
        _in_flight_resume.add(task_id)

        try:
            parent = await store.get(task_id)
        except KeyError as exc:
            _in_flight_resume.discard(task_id)
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        if parent.status not in {TaskStatus.FAILED, TaskStatus.ABORTED}:
            _in_flight_resume.discard(task_id)
            raise HTTPException(
                status_code=409,
                detail=f"Cannot resume task in {parent.status} state (must be FAILED or ABORTED)",
            )

        # Stage-specific eligibility — hard 409, no fallback reconstruction
        if request.stage == "feedback":
            if not parent.plan_approval_snapshot:
                _in_flight_resume.discard(task_id)
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "No plan approval snapshot available;"
                        " cannot restore exact feedback state"
                    ),
                )
        if request.stage == "execute":
            if not parent.plan:
                _in_flight_resume.discard(task_id)
                raise HTTPException(status_code=409, detail="No executable plan to resume from")
            if not parent.shadow_workspace_path or not Path(parent.shadow_workspace_path).exists():
                _in_flight_resume.discard(task_id)
                raise HTTPException(
                    status_code=409,
                    detail="Parent shadow workspace no longer exists; cannot resume execute stage",
                )
            # Allow resume even when all steps are done — re-runs full validation
            # on the existing shadow, which is useful when validation previously
            # failed for environmental reasons (quota, pre-existing test failures).

        if request.stage == "validate":
            if not parent.plan:
                _in_flight_resume.discard(task_id)
                raise HTTPException(status_code=409, detail="No plan to revalidate")
            if not parent.shadow_workspace_path or not Path(parent.shadow_workspace_path).exists():
                _in_flight_resume.discard(task_id)
                raise HTTPException(
                    status_code=409,
                    detail="Parent shadow workspace no longer exists; cannot revalidate",
                )
            plan_step_ids = {step.id for step in parent.plan.steps}
            if not plan_step_ids <= set(parent.completed_step_ids):
                _in_flight_resume.discard(task_id)
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Not all plan steps are complete;"
                        " use stage=execute to finish them before revalidating"
                    ),
                )

        # Merge budget: parent values unless overridden
        override = request.budget_override
        merged_budget = TaskBudget(
            max_iterations=(
                override.max_iterations if override and override.max_iterations is not None
                else parent.budget.max_iterations
            ),
            max_tokens=(
                override.max_tokens if override and override.max_tokens is not None
                else parent.budget.max_tokens
            ),
            max_files_touched=(
                override.max_files_touched if override and override.max_files_touched is not None
                else parent.budget.max_files_touched
            ),
            max_runtime_ms=(
                override.max_runtime_ms if override and override.max_runtime_ms is not None
                else parent.budget.max_runtime_ms
            ),
        )

        child_id = f"task-{uuid4()}"
        now = datetime.now(UTC)

        if request.stage == "plan":
            child = TaskRecord(
                task_id=child_id,
                goal=parent.goal,
                workspace_path=parent.workspace_path,
                mode=parent.mode,
                budget=merged_budget,
                status=TaskStatus.QUEUED,
                resume_of_task_id=parent.task_id,
                created_at=now,
                updated_at=now,
            )
        elif request.stage == "feedback":
            snapshot_state = parent.plan_approval_snapshot.task_state  # type: ignore[union-attr]
            snapshot_task = TaskRecord.model_validate(snapshot_state)
            child = TaskRecord(
                task_id=child_id,
                goal=snapshot_task.goal,
                workspace_path=snapshot_task.workspace_path,
                mode=snapshot_task.mode,
                budget=merged_budget,
                status=TaskStatus.AWAITING_PLAN_APPROVAL,
                plan_markdown=snapshot_task.plan_markdown,
                diagnostics=snapshot_task.diagnostics,
                plan_approval_snapshot=snapshot_task.plan_approval_snapshot,
                shadow_workspace_path=None,  # continue_task() calls prepare() fresh
                resume_of_task_id=parent.task_id,
                created_at=now,
                updated_at=now,
            )
        else:  # "execute" / "validate"
            # Current task state IS the correct starting point:
            #   plan/plan_markdown unchanged throughout lifecycle
            #   completed_step_ids: exactly reflects what finished (failed step excluded)
            #   modified_files: restored to pre-failed-step state by checkpoint logic
            #   shadow: restored to pre-failed-step state by _run_step_with_retries
            #   baseline_error_fingerprints: original pre-execution baseline, reused by validate
            child = TaskRecord(
                task_id=child_id,
                goal=parent.goal,
                workspace_path=parent.workspace_path,
                mode=parent.mode,
                budget=merged_budget,
                status=TaskStatus.PLANNED,
                plan=parent.plan,
                plan_markdown=parent.plan_markdown,
                completed_step_ids=list(parent.completed_step_ids),
                modified_files=list(parent.modified_files),
                baseline_error_fingerprints=list(parent.baseline_error_fingerprints),
                plan_approval_snapshot=parent.plan_approval_snapshot,
                resume_of_task_id=parent.task_id,
                created_at=now,
                updated_at=now,
            )

        await store.create(child)

        async def _run_and_release() -> None:
            try:
                if request.stage == "plan":
                    await orchestrator.run_task(child_id)
                elif request.stage in ("execute", "validate"):
                    shadow = await workspace_manager.clone(
                        parent.task_id,
                        child_id,
                        parent.workspace_path,
                        src_override=Path(parent.shadow_workspace_path) if parent.shadow_workspace_path else None,
                    )
                    child_record = await store.get(child_id)
                    child_record.shadow_workspace_path = str(shadow.shadow_path)
                    await store.save(child_record)
                    if request.stage == "execute":
                        await orchestrator.resume_task(child_id)
                    else:
                        await orchestrator.revalidate_task(child_id)
                # "feedback": no async work — user calls /plan/feedback on child_id
            finally:
                _in_flight_resume.discard(task_id)

        asyncio.create_task(_run_and_release())

        return ResumeTaskResponse(task_id=child_id, resume_of_task_id=parent.task_id)

    # ── Index routes ───────────────────────────────────────────────────────────
    # Allows callers (CI, indexer scripts, start-backend.sh) to pre-warm the
    # semantic index immediately after the Rust indexer writes a new snapshot,
    # eliminating the cold-start penalty on the first task.

    @router.get("/index/status")
    async def get_index_status() -> dict:  # type: ignore[type-arg]
        """Return the current state of the semantic index.

        GET /v1/index/status
        Response: { "semantic_enabled": bool, "building": bool, "last_indexed_snapshot_ms": int }
        Poll until building=false to know the pre-warm triggered by POST /v1/index/build is done.
        """
        if retrieval_client is None:
            return {"semantic_enabled": False, "building": False, "last_indexed_snapshot_ms": 0}
        return retrieval_client.index_status()

    @router.post("/index/build", status_code=202)
    async def build_index(request: dict) -> dict:  # type: ignore[type-arg]
        """Trigger an async semantic index build for a workspace.

        POST /v1/index/build
        Body: { "workspace_path": "/abs/path/to/workspace" }

        Returns 202 immediately; building runs in the background.
        Returns 503 if semantic retrieval is not enabled.
        Returns 400 if workspace_path is missing.
        """
        import asyncio as _asyncio

        workspace_path = request.get("workspace_path", "").strip() if isinstance(request, dict) else ""
        if not workspace_path:
            raise HTTPException(400, "workspace_path is required")
        if retrieval_client is None or not retrieval_client.semantic_enabled():
            raise HTTPException(503, "Semantic retrieval is not enabled (set AI_EDITOR_SEMANTIC_RETRIEVAL=true)")

        async def _build() -> None:
            loop = _asyncio.get_event_loop()
            await loop.run_in_executor(None, retrieval_client.trigger_index_build, workspace_path)

        _asyncio.create_task(_build())
        return {"status": "building", "workspace_path": workspace_path}

    # --- Inline change routes ---

    @router.post("/chat/inline-changes/{inline_task_id}/promote")
    async def promote_inline_change(inline_task_id: str) -> dict:
        try:
            await orchestrator.promote_inline_change(inline_task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if chat_agent is not None:
            chat_agent._store.resolve_diff_card(inline_task_id, "applied")  # type: ignore[union-attr]
        return {"status": "promoted", "inline_task_id": inline_task_id}

    @router.delete("/chat/inline-changes/{inline_task_id}")
    async def discard_inline_change(inline_task_id: str) -> dict:
        await orchestrator.discard_inline_change(inline_task_id)
        if chat_agent is not None:
            chat_agent._store.resolve_diff_card(inline_task_id, "discarded")  # type: ignore[union-attr]
        return {"status": "discarded", "inline_task_id": inline_task_id}

    # --- Chat routes ---

    if chat_agent is not None:
        from agentd.chat.agent import ChatAgent as _ChatAgent
        _chat_agent: _ChatAgent = chat_agent  # type: ignore[assignment]

        @router.get("/chat/threads")
        async def list_chat_threads(workspace: str) -> dict:
            threads = _chat_agent._store.list_threads(workspace)
            return {"threads": [t.model_dump(exclude={"messages"}) for t in threads]}

        @router.post("/chat/threads")
        async def create_chat_thread(request: dict) -> dict:
            workspace = request.get("workspace", "")
            title = request.get("title", "New Chat")
            thread = _chat_agent._store.create_thread(workspace, title=title)
            return thread.model_dump(exclude={"messages"})

        @router.get("/chat/threads/{thread_id}")
        async def get_chat_thread(thread_id: str) -> dict:
            thread = _chat_agent._store.get_thread(thread_id)
            if thread is None:
                raise HTTPException(status_code=404, detail="Thread not found")
            return thread.model_dump()

        @router.post("/chat/threads/{thread_id}/message")
        async def post_chat_message(thread_id: str, request: dict) -> StreamingResponse:
            import asyncio as _asyncio_chat
            import json as _json
            message = request.get("content") or request.get("message", "")
            channel_id = f"chat:{thread_id}"
            # Clear stale replay events from the previous message so a new subscriber
            # doesn't receive old events (including a stale chat_done).
            _chat_agent._broadcaster.clear_replay(channel_id)
            queue = _chat_agent._broadcaster.subscribe(channel_id)

            async def _run_agent() -> None:
                try:
                    await _chat_agent.handle_message(thread_id, message, channel_id=channel_id)
                except Exception:
                    import logging as _logging
                    _logging.getLogger(__name__).exception("ChatAgent.handle_message failed")
                    _chat_agent._broadcaster.broadcast(channel_id, {"type": "chat_done", "payload": {}})

            async def event_stream():
                # Start the agent task INSIDE the generator so it is scheduled
                # after Starlette has begun consuming the stream. This guarantees
                # the first `await queue.get()` suspends before the agent runs,
                # which produces proper per-event streaming instead of a bulk
                # dump when the model responds fast.
                agent_task = _asyncio_chat.create_task(_run_agent())
                try:
                    while True:
                        try:
                            event = await _asyncio_chat.wait_for(
                                queue.get(), timeout=15.0
                            )
                        except _asyncio_chat.TimeoutError:
                            # Keep-alive ping — prevents Node.js fetch from
                            # dropping the connection during slow LLM inference.
                            yield ": ping\n\n"
                            continue
                        yield f"data: {_json.dumps(event)}\n\n"
                        if event.get("type") in ("chat_done", "done"):
                            break
                finally:
                    _chat_agent._broadcaster.unsubscribe(channel_id, queue)
                    agent_task.cancel()

            return StreamingResponse(event_stream(), media_type="text/event-stream")

    return router
