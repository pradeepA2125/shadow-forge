from __future__ import annotations

import time
from pathlib import Path
from typing import Protocol

from agentd.domain.models import (
    Diagnostic,
    PatchDocument,
    PlanDocument,
    TaskRecord,
    TaskStatus,
    ValidationResult,
)
from agentd.domain.state_machine import assert_budget, bump_usage, transition
from agentd.patch.engine import PatchEngine
from agentd.reasoning.contracts import ReasoningEngine
from agentd.retrieval.artifact_client import RetrievalContext
from agentd.storage.base import TaskStore
from agentd.workspace.shadow import ShadowWorkspaceManager


class Validator(Protocol):
    async def run(self, workspace_path: str) -> ValidationResult: ...


class RetrievalClient(Protocol):
    def load_context(
        self,
        workspace_path: str,
        goal: str,
    ) -> tuple[RetrievalContext, list[Diagnostic]]: ...


class NullRetrievalClient:
    def load_context(
        self,
        workspace_path: str,
        goal: str,
    ) -> tuple[RetrievalContext, list[Diagnostic]]:
        _ = (workspace_path, goal)
        return RetrievalContext.empty(), []


class AgentOrchestrator:
    def __init__(
        self,
        store: TaskStore,
        reasoning_engine: ReasoningEngine,
        validator: Validator,
        patch_engine: PatchEngine,
        workspace_manager: ShadowWorkspaceManager,
        retrieval_client: RetrievalClient | None = None,
    ) -> None:
        self._store = store
        self._reasoning_engine = reasoning_engine
        self._validator = validator
        self._patch_engine = patch_engine
        self._workspace_manager = workspace_manager
        self._retrieval_client = retrieval_client or NullRetrievalClient()

    async def run_task(self, task_id: str) -> TaskRecord:
        task = await self._store.get(task_id)
        started_at_ms = int(time.time() * 1000)
        retrieval_context = RetrievalContext.empty()
        persistent_diagnostics: list[Diagnostic] = []

        try:
            task = transition(task, TaskStatus.CONTEXT_READY, "context assembled")
            await self._store.save(task)

            shadow_workspace = await self._workspace_manager.prepare(
                task.task_id,
                task.workspace_path,
            )
            task.shadow_workspace_path = str(shadow_workspace.shadow_path)
            await self._store.save(task)

            retrieval_context, retrieval_warnings = self._retrieval_client.load_context(
                task.workspace_path,
                task.goal,
            )
            persistent_diagnostics = retrieval_warnings
            task.diagnostics = [*persistent_diagnostics]
            await self._store.save(task)

            plan_raw = await self._reasoning_engine.create_plan(
                task,
                str(shadow_workspace.shadow_path),
                retrieval_context.as_prompt_payload(),
            )
            task.plan = PlanDocument.model_validate(plan_raw)
            task = transition(task, TaskStatus.PLANNED, "plan accepted")
            await self._store.save(task)

            while task.status in {TaskStatus.PLANNED, TaskStatus.REPAIRING}:
                assert_budget(task, started_at_ms, int(time.time() * 1000))
                task = bump_usage(task)
                await self._store.save(task)

                patch_raw = await self._reasoning_engine.create_patch(
                    task,
                    str(shadow_workspace.shadow_path),
                    task.diagnostics,
                    retrieval_context.as_prompt_payload(),
                )
                patch = PatchDocument.model_validate(patch_raw)
                task.latest_patch = patch

                patch_result = await self._patch_engine.apply_patch_document(
                    Path(shadow_workspace.shadow_path),
                    patch,
                )
                touched = patch_result.touched_files
                task.modified_files = sorted({*task.modified_files, *touched})
                task.diagnostics = [*persistent_diagnostics]

                task = transition(task, TaskStatus.PATCHED, "patch applied in shadow workspace")
                await self._store.save(task)

                task = transition(task, TaskStatus.VALIDATING, "validation started")
                await self._store.save(task)

                validation = await self._validator.run(str(shadow_workspace.shadow_path))
                if validation.success:
                    task.diagnostics = [*persistent_diagnostics]
                    task = transition(
                        task,
                        TaskStatus.READY_FOR_REVIEW,
                        "validation passed; ready for review",
                    )
                    await self._store.save(task)
                    return task

                task.diagnostics = [*persistent_diagnostics, *validation.diagnostics]
                if task.usage.iterations >= task.budget.max_iterations:
                    task = transition(task, TaskStatus.FAILED, "repair budget exhausted")
                    await self._store.save(task)
                    return task

                task = transition(task, TaskStatus.REPAIRING, "validation failed")
                await self._store.save(task)

            return task
        except Exception as exc:
            if task.status not in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.ABORTED}:
                task.diagnostics.append(
                    Diagnostic(source="orchestrator", message=str(exc), level="error")
                )
                try:
                    task = transition(task, TaskStatus.FAILED, "unhandled orchestrator error")
                except ValueError:
                    pass
            await self._store.save(task)
            return task
