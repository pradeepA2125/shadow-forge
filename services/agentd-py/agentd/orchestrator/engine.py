from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from pydantic import ValidationError

from agentd.domain.models import (
    AgentToolTrace,
    CandidateScoreBreakdown,
    CheckpointManifest,
    Diagnostic,
    PatchCandidateV2,
    PatchDocumentV2,
    PatchFailureCode,
    PatchPreflightIssue,
    PlanCritiqueIssue,
    PlanCritiqueResult,
    PlanEvidencePack,
    PlanStep,
    PlanTargetIntent,
    PlanDocument,
    StepExecutionTrace,
    StepRunResult,
    TaskMilestoneSnapshot,
    TaskRecord,
    TaskStatus,
    ValidationResult,
)
from agentd.domain.state_machine import assert_budget, bump_usage, transition
from agentd.orchestrator.broadcaster import PatchEventBroadcaster
from agentd.planning.agent import PlanningAgent
from agentd.planning.registry import PlanningToolRegistry
from agentd.tools.loop import PlanHandoff
from agentd.patch.engine import PatchEngine
from agentd.reasoning.contracts import ReasoningEngine
from agentd.retrieval.artifact_client import RetrievalContext
from agentd.retrieval.chunker import ScoredChunk
from agentd.runtime.adapters import GenericPlanningAdapter, PlanningAdapter
from agentd.runtime.artifacts import task_artifacts_root
from agentd.storage.base import TaskStore
from agentd.workspace.shadow import ShadowWorkspace, ShadowWorkspaceManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _CandidateEvaluation:
    candidate: PatchCandidateV2
    score: float
    breakdown: CandidateScoreBreakdown
    preflight_issues: list[PatchPreflightIssue]
    validation: ValidationResult | None
    touched_files: list[str]
    changed_lines: int
    new_file_count: int
    preflight_report_path: str | None
    validation_report_path: str | None

    @property
    def preflight_pass(self) -> bool:
        return self.breakdown.preflight_pass

    @property
    def validation_pass(self) -> bool:
        return self.breakdown.validation_pass


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
        planning_adapter: PlanningAdapter | None = None,
        max_attempts_per_step: int = 3,
        step_scoped_mode: bool = True,
        patch_candidate_count: int = 3,
    ) -> None:
        self._store = store
        self._reasoning_engine = reasoning_engine
        self._validator = validator
        self._patch_engine = patch_engine
        self._workspace_manager = workspace_manager
        self._retrieval_client = retrieval_client or NullRetrievalClient()
        self._planning_adapter = planning_adapter or GenericPlanningAdapter()
        self._max_attempts_per_step = max(1, max_attempts_per_step)
        self._step_scoped_mode = step_scoped_mode
        self._patch_candidate_count = max(1, patch_candidate_count)
        self.broadcaster = PatchEventBroadcaster()
        self._running_tasks: set[str] = set()
        import os
        self._tool_loop_enabled: bool = os.environ.get("AI_EDITOR_TOOL_LOOP_ENABLED", "true") not in ("0", "false", "False")

    async def run_task(self, task_id: str) -> TaskRecord:
        task = await self._store.get(task_id)
        self._running_tasks.add(task_id)
        started_at_ms = int(time.time() * 1000)
        retrieval_context = RetrievalContext.empty()
        persistent_diagnostics: list[Diagnostic] = []
        task.artifacts_root_path = str(self._artifacts_root(task.task_id, task.workspace_path))

        try:
            task = transition(task, TaskStatus.CONTEXT_READY, "context assembled")
            await self._store.save(task)

            shadow_workspace = await self._workspace_manager.prepare(task.task_id, task.workspace_path)
            task.shadow_workspace_path = str(shadow_workspace.shadow_path)
            await self._store.save(task)

            retrieval_context, retrieval_warnings = self._retrieval_client.load_context(
                task.workspace_path,
                task.goal,
            )
            workspace_files_index = self._collect_workspace_file_index(
                Path(shadow_workspace.shadow_path)
            )
            workspace_files_set = set(workspace_files_index)
            plan_context_payload = retrieval_context.as_prompt_payload()
            persistent_diagnostics = retrieval_warnings
            task.diagnostics = [*persistent_diagnostics]
            await self._store.save(task)

            self._write_debug_artifact(
                task.task_id,
                "plan-evidence",
                {
                    "planner_evidence": plan_context_payload.get("planner_evidence"),
                    "diagnostics_excerpt": plan_context_payload.get("diagnostics_excerpt"),
                },
                artifacts_root_path=task.artifacts_root_path,
            )
            print("\n[PLAN] PlanningAgent exploring workspace...")
            planning_agent = self._build_planning_agent(task.task_id, task.workspace_path)
            planning_result = await planning_agent.generate_plan(
                task=task,
                initial_context=plan_context_payload,
                budget=task.budget,
            )
            self._write_debug_artifact(
                task.task_id,
                "planning-trace",
                planning_result.tool_trace.model_dump(mode="json"),
                artifacts_root_path=task.artifacts_root_path,
            )
            print(
                f"[PLAN] Plan created. Examined {len(planning_result.files_examined)} files. "
                f"Confidence: {planning_result.confidence}"
            )
            task.plan_markdown = planning_result.plan_markdown
            confidence_diagnostics: list[Diagnostic] = []
            if planning_result.confidence == "low":
                confidence_diagnostics = [Diagnostic(
                    source="planning_agent",
                    message=(
                        f"Planning confidence: low. Agent examined "
                        f"{len(planning_result.files_examined)} files — review plan carefully."
                    ),
                    level="warning",
                )]
            task.diagnostics = [*persistent_diagnostics, *confidence_diagnostics]
            task.plan_approval_snapshot = TaskMilestoneSnapshot(
                captured_at=datetime.now(timezone.utc),
                task_state=task.model_dump(mode="json"),
            )
            task = transition(task, TaskStatus.AWAITING_PLAN_APPROVAL, "plan generated; awaiting approval")
            await self._store.save(task)

            # In Spec-First mode, we pause here and wait for the user to approve/revise.
            # Do NOT broadcast "done" — the execution stream hasn't started yet.
            # The replay buffer is cleared so that the post-approval stream starts fresh.
            self._running_tasks.discard(task_id)
            self.broadcaster.clear_replay(task_id)
            return task

        except Exception as exc:
            logger.error(f"Task {task_id} failed during initialization", exc_info=True)
            task.diagnostics.append(
                Diagnostic(source="orchestrator", message=str(exc), level="error")
            )
            try:
                task = transition(task, TaskStatus.FAILED, "initialization failed")
            except ValueError:
                pass
            await self._store.save(task)
            self._running_tasks.discard(task_id)
            self.broadcaster.broadcast(task_id, {"type": "done"})
            return task

    async def continue_task(self, task_id: str, feedback: str | None = None) -> TaskRecord:
        task = await self._store.get(task_id)
        if task.status != TaskStatus.AWAITING_PLAN_APPROVAL:
            raise ValueError(f"Task {task_id} is not awaiting plan approval")

        self._running_tasks.add(task_id)
        started_at_ms = int(time.time() * 1000)
        task.artifacts_root_path = str(self._artifacts_root(task.task_id, task.workspace_path))

        try:
            shadow_workspace = await self._workspace_manager.prepare(task.task_id, task.workspace_path)
            task.shadow_workspace_path = str(shadow_workspace.shadow_path)
            
            retrieval_context, retrieval_warnings = self._retrieval_client.load_context(
                task.workspace_path,
                task.goal,
            )
            workspace_files_index = self._collect_workspace_file_index(
                Path(shadow_workspace.shadow_path)
            )
            workspace_files_set = set(workspace_files_index)
            plan_context_payload = retrieval_context.as_prompt_payload()
            self._write_debug_artifact(
                task.task_id,
                "plan-evidence",
                {
                    "planner_evidence": plan_context_payload.get("planner_evidence"),
                    "diagnostics_excerpt": plan_context_payload.get("diagnostics_excerpt"),
                },
                artifacts_root_path=task.artifacts_root_path,
            )

            if feedback:
                # User provided feedback, regenerate markdown plan
                task = transition(task, TaskStatus.CONTEXT_READY, "regenerating plan with feedback")
                await self._store.save(task)

                planning_agent = self._build_planning_agent(task.task_id, task.workspace_path)
                planning_result = await planning_agent.generate_plan(
                    task=task,
                    initial_context={**plan_context_payload, "plan_feedback": feedback},
                    budget=task.budget,
                )
                self._write_debug_artifact(
                    task.task_id,
                    "planning-trace-feedback",
                    planning_result.tool_trace.model_dump(mode="json"),
                    artifacts_root_path=task.artifacts_root_path,
                )
                task.plan_markdown = planning_result.plan_markdown
                confidence_diagnostics_fb: list[Diagnostic] = [Diagnostic(
                    source="planning_agent",
                    message="Planning confidence: low. Review plan carefully.",
                    level="warning",
                )] if planning_result.confidence == "low" else []
                task.diagnostics = [*retrieval_warnings, *confidence_diagnostics_fb]
                task.plan_approval_snapshot = TaskMilestoneSnapshot(
                    captured_at=datetime.now(timezone.utc),
                    task_state=task.model_dump(mode="json"),
                )
                task = transition(task, TaskStatus.AWAITING_PLAN_APPROVAL, "plan regenerated; awaiting approval")
                await self._store.save(task)
                self._running_tasks.discard(task_id)
                self.broadcaster.clear_replay(task_id)
                return task

            # Approved! Generate JSON plan from Markdown
            print("\n[PLAN] Plan Approved. Generating executable JSON plan...")
            task = transition(task, TaskStatus.PLANNED, "plan approved; starting execution")
            await self._store.save(task)

            plan_raw = await self._reasoning_engine.create_plan(
                task,
                str(shadow_workspace.shadow_path),
                plan_context_payload,
            )
            self._write_debug_artifact(
                task.task_id,
                "json-plan-draft",
                {"plan": plan_raw},
                artifacts_root_path=task.artifacts_root_path,
            )
            try:
                candidate_plan = PlanDocument.model_validate(plan_raw)
            except ValidationError as exc:
                task.diagnostics.append(Diagnostic(
                    source="orchestrator",
                    message=f"JSON plan schema validation failed: {exc}",
                    level="error",
                ))
                task = transition(task, TaskStatus.FAILED, "JSON plan schema invalid")
                await self._store.save(task)
                return task

            task.plan = candidate_plan
            self._write_debug_artifact(
                task.task_id,
                "plan",
                {"plan": plan_raw},
                artifacts_root_path=task.artifacts_root_path,
            )

            return await self._execute_plan(
                task,
                shadow_workspace,
                retrieval_context,
                retrieval_warnings,
                started_at_ms,
            )

        except Exception as exc:
            logger.error(f"Task {task_id} failed during continuation", exc_info=True)
            task.diagnostics.append(
                Diagnostic(source="orchestrator", message=str(exc), level="error")
            )
            task = transition(task, TaskStatus.FAILED, "continuation failed")
            await self._store.save(task)
            self._running_tasks.discard(task_id)
            self.broadcaster.broadcast(task_id, {"type": "done"})
            return task

    async def resume_task(self, task_id: str) -> TaskRecord:
        """Resume execution of a child task that was created from a failed/aborted parent.

        The child task must already be in PLANNED state with shadow_workspace_path set
        (cloned from the parent by the route handler).  Skips plan generation entirely
        and calls _execute_plan() directly, relying on the existing completed_step_ids
        skip logic to continue from the first incomplete step.
        """
        task = await self._store.get(task_id)
        self._running_tasks.add(task_id)
        task.artifacts_root_path = str(self._artifacts_root(task.task_id, task.workspace_path))
        started_at_ms = int(time.time() * 1000)
        try:
            retrieval_context, retrieval_warnings = self._retrieval_client.load_context(
                task.workspace_path, task.goal
            )
            shadow_workspace = ShadowWorkspace(
                task_id=task.task_id,
                real_path=Path(task.workspace_path).resolve(),
                shadow_path=Path(task.shadow_workspace_path),  # type: ignore[arg-type]
            )
            return await self._execute_plan(
                task, shadow_workspace, retrieval_context, retrieval_warnings, started_at_ms
            )
        except Exception as exc:
            logger.error(f"Task {task_id} failed during resume", exc_info=True)
            task.diagnostics.append(Diagnostic(source="orchestrator", message=str(exc), level="error"))
            task = transition(task, TaskStatus.FAILED, "resume failed")
            await self._store.save(task)
            self._running_tasks.discard(task_id)
            self.broadcaster.broadcast(task_id, {"type": "done"})
            return task

    async def _execute_plan(
        self,
        task: TaskRecord,
        shadow_workspace: ShadowWorkspace,
        retrieval_context: RetrievalContext,
        persistent_diagnostics: list[Diagnostic],
        started_at_ms: int,
    ) -> TaskRecord:
        try:
            shadow_path = Path(shadow_workspace.shadow_path)
            if task.plan is None:
                task = transition(task, TaskStatus.FAILED, "plan missing")
                await self._store.save(task)
                return task

            task = transition(task, TaskStatus.EXECUTING, "execution started")
            await self._store.save(task)

            baseline_errors = await self._collect_baseline_errors(
                shadow_path,
                task_id=task.task_id,
                artifacts_root_path=task.artifacts_root_path,
            )
            if baseline_errors:
                logger.info(
                    "Baseline validation captured pre-existing errors",
                    extra={"task_id": task.task_id, "baseline_error_count": len(baseline_errors)},
                )

            while (step := self._next_incomplete_step(task)) is not None:
                step_result = await self._run_step_with_retries(
                    task,
                    step,
                    shadow_path,
                    retrieval_context,
                    persistent_diagnostics,
                    started_at_ms,
                )

                if isinstance(step_result, PlanHandoff):
                    # Delta replan: planning agent dispatch added in Task 12.
                    # For now, fail cleanly so existing tests see a predictable error.
                    task.diagnostics.append(Diagnostic(
                        source="orchestrator",
                        message=f"Delta replan requested by step {step_result.step_id}: {step_result.reason}",
                        level="error",
                    ))
                    task = transition(task, TaskStatus.FAILED, "delta replan not yet wired")
                    await self._store.save(task)
                    return task

                self._merge_step_result(task, step_result, persistent_diagnostics)
                await self._store.save(task)
                if step_result.outcome != "step_completed":
                    task = transition(task, TaskStatus.FAILED, "step execution exhausted")
                    await self._store.save(task)
                    return task

            task = transition(task, TaskStatus.VALIDATING, "full validation started")
            await self._store.save(task)
            validation = await self._validator.run(str(shadow_workspace.shadow_path))
            validation = self._filter_baseline_errors(validation, baseline_errors)
            self._write_debug_artifact(
                task.task_id,
                "full-validation",
                validation.model_dump(mode="json"),
                artifacts_root_path=task.artifacts_root_path,
            )
            if validation.success:
                task.diagnostics = [*persistent_diagnostics]
                task = transition(task, TaskStatus.VALIDATED, "full validation passed")
                await self._store.save(task)
                task = transition(
                    task,
                    TaskStatus.READY_FOR_REVIEW,
                    "validation passed; ready for review",
                )
                await self._store.save(task)
                return task

            task.diagnostics = [*persistent_diagnostics, *validation.diagnostics]
            task = transition(task, TaskStatus.REPAIRING, "full validation failed")
            await self._store.save(task)

            repair_targets = task.modified_files or task.plan.expected_files
            repair_step = PlanStep(
                id="repair-full-validation",
                goal="Repair files failing full validation",
                targets=[
                    {
                        "path": path,
                        "intent": PlanTargetIntent.EXISTING.value,
                    }
                    for path in repair_targets
                ],
                risk="med",
            )
            task = transition(task, TaskStatus.EXECUTING, "repair execution started")
            await self._store.save(task)
            repair_result = await self._run_step_with_retries(
                task,
                repair_step,
                shadow_path,
                retrieval_context,
                persistent_diagnostics,
                started_at_ms,
                last_failure={
                    "failure_code": PatchFailureCode.APPLY_ERROR.value,
                    "file": None,
                    "op_id": None,
                    "excerpt": "\n".join(d.message for d in validation.diagnostics[:10]),
                },
            )
            self._merge_step_result(task, repair_result, persistent_diagnostics)
            await self._store.save(task)
            if repair_result.outcome != "step_completed":
                task = transition(task, TaskStatus.FAILED, "repair budget exhausted")
                await self._store.save(task)
                return task

            task = transition(task, TaskStatus.VALIDATING, "full validation started after repair")
            await self._store.save(task)
            repair_validation = await self._validator.run(str(shadow_workspace.shadow_path))
            repair_validation = self._filter_baseline_errors(repair_validation, baseline_errors)
            self._write_debug_artifact(
                task.task_id,
                "full-validation",
                repair_validation.model_dump(mode="json"),
                artifacts_root_path=task.artifacts_root_path,
            )
            if not repair_validation.success:
                task.diagnostics = [*persistent_diagnostics, *repair_validation.diagnostics]
                task = transition(task, TaskStatus.FAILED, "post-repair validation failed")
                await self._store.save(task)
                return task

            task.diagnostics = [*persistent_diagnostics]
            task = transition(task, TaskStatus.VALIDATED, "full validation passed after repair")
            await self._store.save(task)
            task = transition(task, TaskStatus.READY_FOR_REVIEW, "repair successful; ready for review")
            await self._store.save(task)
            return task
        except Exception as exc:
            logger.error(f"Task {task.task_id} failed during execution", exc_info=True)
            task.diagnostics.append(
                Diagnostic(source="orchestrator", message=str(exc), level="error")
            )
            task = transition(task, TaskStatus.FAILED, "execution failed")
            await self._store.save(task)
            return task
        finally:
            self._running_tasks.discard(task.task_id)
            self.broadcaster.broadcast(task.task_id, {"type": "done"})
            if task.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.ABORTED}:
                try:
                    await self._workspace_manager.prune_checkpoints()
                except Exception:
                    logger.exception(
                        "Checkpoint pruning failed",
                        extra={"task_id": task.task_id},
                    )

    async def _generate_repo_grounded_markdown_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        plan_context_payload: dict[str, object],
    ) -> tuple[str, list[Diagnostic]]:
        draft_rounds: list[dict[str, object]] = []
        critique_rounds: list[dict[str, object]] = []
        working_context = dict(plan_context_payload)
        final_markdown = task.plan_markdown or ""
        unresolved_issues: list[PlanCritiqueIssue] = []

        for attempt in range(3):
            final_markdown = await self._reasoning_engine.create_markdown_plan(
                task,
                workspace_path,
                working_context,
            )
            draft_rounds.append(
                {
                    "round": attempt + 1,
                    "plan_markdown": final_markdown,
                }
            )
            critique_raw = await self._reasoning_engine.critique_markdown_plan(
                task,
                workspace_path,
                working_context,
                final_markdown,
            )
            critique = PlanCritiqueResult.model_validate(critique_raw)
            critique_rounds.append(
                {
                    "round": attempt + 1,
                    "critique": critique.model_dump(mode="json"),
                }
            )
            unresolved_issues = critique.issues if critique.verdict == "revise" else []
            if not unresolved_issues:
                break
            working_context["plan_critique_feedback"] = critique.model_dump(mode="json")

        self._write_debug_artifact(
            task.task_id,
            "markdown-plan-draft",
            {"rounds": draft_rounds},
            artifacts_root_path=task.artifacts_root_path,
        )
        self._write_debug_artifact(
            task.task_id,
            "markdown-plan-critique",
            {"rounds": critique_rounds},
            artifacts_root_path=task.artifacts_root_path,
        )
        self._write_debug_artifact(
            task.task_id,
            "markdown-plan-final",
            {"plan_markdown": final_markdown},
            artifacts_root_path=task.artifacts_root_path,
        )
        return final_markdown, self._critique_diagnostics(
            unresolved_issues,
            source="markdown_plan_critique",
        )

    def _merge_step_result(
        self,
        task: TaskRecord,
        result: StepRunResult,
        persistent_diagnostics: list[Diagnostic],
    ) -> None:
        if result.selected_candidate_id is not None:
            task.selected_candidate_id = result.selected_candidate_id
        task.modified_files = sorted({*task.modified_files, *result.touched_files})
        task.execution_trace.extend(result.trace_entries)
        task.checkpoints.extend(result.checkpoint_manifests)
        task.diagnostics = [*result.diagnostics] if result.diagnostics else [*persistent_diagnostics]
        plan_step_ids = {step.id for step in task.plan.steps} if task.plan else set()
        if (
            result.outcome == "step_completed"
            and result.step_id in plan_step_ids
            and result.step_id not in task.completed_step_ids
        ):
            task.completed_step_ids.append(result.step_id)

    def _build_planning_agent(self, task_id: str, workspace_path: str) -> PlanningAgent:
        """Construct a PlanningAgent reading from the real (unmodified) workspace."""
        planning_registry = PlanningToolRegistry(
            real_path=Path(workspace_path).resolve(),
            semantic_index=getattr(self._retrieval_client, "_semantic_index", None),
        )
        return PlanningAgent(
            reasoning_engine=self._reasoning_engine,
            registry=planning_registry,
            broadcaster=self.broadcaster,
            task_id=task_id,
        )

    def _next_incomplete_step(self, task: TaskRecord) -> PlanStep | None:
        """Return the first step in the plan that hasn't been completed."""
        if task.plan is None:
            return None
        completed = set(task.completed_step_ids)
        return next((s for s in task.plan.steps if s.id not in completed), None)

    async def _run_step_with_retries(
        self,
        task: TaskRecord,
        step: PlanStep,
        shadow_path: Path,
        retrieval_context: RetrievalContext,
        persistent_diagnostics: list[Diagnostic],
        started_at_ms: int,
        *,
        last_failure: dict[str, object] | None = None,
    ) -> "StepRunResult | PlanHandoff":
        allowed_files = sorted(set(step.target_paths()))
        if not allowed_files:
            allowed_files = [*task.modified_files] or [*task.plan.expected_files]
        max_files = max(1, min(task.budget.max_files_touched, len(allowed_files)))
        max_ops = max(1, min(12, max_files * 3))
        allowed_files_set = set(allowed_files)
        trace_entries: list[StepExecutionTrace] = []
        checkpoints: list[CheckpointManifest] = []
        last_result_diagnostics: list[Diagnostic] = [*persistent_diagnostics]
        last_selected_candidate_id: str | None = None
        touched_files_result: list[str] = []

        for attempt in range(1, self._max_attempts_per_step + 1):
            print(f"\n[STEP] Running step {step.id} (Attempt {attempt}/{self._max_attempts_per_step})")
            print(f"[GOAL] {step.goal}")
            
            logger.info(
                "Step attempt started",
                extra={
                    "task_id": task.task_id,
                    "step_id": step.id,
                    "attempt": attempt,
                    "max_attempts_per_step": self._max_attempts_per_step,
                },
            )
            assert_budget(task, started_at_ms, int(time.time() * 1000))
            task = bump_usage(task)

            checkpoint = self._create_shadow_checkpoint(
                task,
                step,
                attempt,
                shadow_path,
                tracked_files=allowed_files,
            )
            previous_modified_files = list(task.modified_files)
            try:
                # Combine step targets and expected files for patching context
                context_files = sorted(set(allowed_files) | set(task.plan.expected_files if task.plan else []))
                
                # Build patch context using semantic chunk-scoped contents when available,
                # falling back to full file content. Either way original line numbers are
                # preserved so replace_range/apply_diff ops remain precise.
                patch_retrieval_context = RetrievalContext(
                    repository_structure=retrieval_context.repository_structure,
                    related_files=retrieval_context.related_files,
                    related_symbols=retrieval_context.related_symbols,
                    graph_neighbors=retrieval_context.graph_neighbors,
                    file_outlines=retrieval_context.file_outlines,
                    diagnostics_excerpt=retrieval_context.diagnostics_excerpt,
                    snapshot_age_sec=retrieval_context.snapshot_age_sec,
                    snapshot_stats=retrieval_context.snapshot_stats,
                    file_contents=self._collect_chunk_scoped_contents(
                        shadow_path, context_files, step.goal, retrieval_context
                    ),
                    planner_evidence=retrieval_context.planner_evidence,
                )
                retrieval_payload = patch_retrieval_context.as_prompt_payload()

                patch_request_context = {
                    "current_step": step.model_dump(mode="json"),
                    "allowed_files": allowed_files,
                    "max_ops": max_ops,
                    "max_files": max_files,
                    "last_failure": last_failure,
                    "diagnostics": [item.model_dump(mode="json") for item in task.diagnostics],
                    "retrieval_context": retrieval_payload,
                }
                self._write_debug_artifact(
                    task.task_id,
                    "patch-context",
                    patch_request_context,
                    step_id=step.id,
                    attempt=attempt,
                    artifacts_root_path=task.artifacts_root_path,
                )

                if self._tool_loop_enabled:
                    print("\n[PATCH] Entering Tool-Use Loop (ReAct)...")
                    from agentd.tools.loop import PatchResult, PlanHandoff, ToolLoop, build_tool_registry
                    registry = build_tool_registry(shadow_path, self._retrieval_client)
                    tool_loop = ToolLoop(
                        self._reasoning_engine,
                        registry,
                        self.broadcaster,
                        task.task_id,
                    )
                    step_outcome = await tool_loop.run(
                        step,
                        {**patch_request_context, "plan_markdown": task.plan_markdown},
                        task.budget,
                        task.usage,
                    )

                    if isinstance(step_outcome, PlanHandoff):
                        self._restore_shadow_checkpoint(shadow_path, checkpoint.checkpoint_path)
                        task.modified_files = previous_modified_files
                        return step_outcome

                    patch_raw = step_outcome.patch_document
                    tool_trace = step_outcome.tool_trace
                    self._write_debug_artifact(
                        task.task_id,
                        "tool-trace",
                        tool_trace.model_dump(mode="json"),
                        step_id=step.id,
                        attempt=attempt,
                        artifacts_root_path=task.artifacts_root_path,
                    )
                    print(f"[PATCH] Tool loop complete ({len(tool_trace.calls)} tool calls)")
                else:
                    print("\n[PATCH] Entering Patching Node...")
                    print(f"[PATCH] Generating {self._patch_candidate_count} candidates for {len(allowed_files)} target files...")
                    patch_raw = await self._create_patch_document(
                        task,
                        str(shadow_path),
                        task.diagnostics,
                        retrieval_payload,
                        current_step=step,
                        allowed_files=allowed_files,
                        max_ops=max_ops,
                        max_files=max_files,
                        candidate_count=self._patch_candidate_count,
                        last_failure=last_failure,
                    )
                self._write_debug_artifact(
                    task.task_id,
                    "patch",
                    patch_raw,
                    step_id=step.id,
                    attempt=attempt,
                    artifacts_root_path=task.artifacts_root_path,
                )
                patch_document = PatchDocumentV2.model_validate(patch_raw)
                task.latest_patch_v2 = patch_document
                task.latest_patch = None

                print(f"[PATCH] Evaluating {len(patch_document.candidates)} candidates...")
                evaluations, ranking_path = await self._evaluate_candidates(
                    task=task,
                    step=step,
                    attempt=attempt,
                    patch_document=patch_document,
                    shadow_path=shadow_path,
                    checkpoint=checkpoint,
                    allowed_files=allowed_files_set,
                    max_ops=max_ops,
                    max_files=max_files,
                )

                selected = self._select_best_candidate(evaluations)
                if selected is None:
                    print("[ERROR] No valid patch candidates were generated")
                    issue = PatchPreflightIssue(
                        code=PatchFailureCode.APPLY_ERROR,
                        file=None,
                        message="No patch candidates were generated",
                    )
                    last_failure = self._last_failure_from_issues([issue])
                    trace_entries.append(
                        StepExecutionTrace(
                            step_id=step.id,
                            attempt=attempt,
                            status="preflight_failed",
                            issues=[issue],
                            message="no patch candidates",
                            checkpoint_id=checkpoint.checkpoint_id,
                        )
                    )
                    last_result_diagnostics = [
                        *persistent_diagnostics,
                        *self._issues_to_diagnostics([issue]),
                    ]
                    self._restore_shadow_checkpoint(shadow_path, checkpoint.checkpoint_path)
                    task.modified_files = previous_modified_files
                    checkpoints.append(checkpoint)
                    continue

                checkpoint.ranking_report_path = ranking_path
                checkpoint.candidate_id = selected.candidate.candidate_id
                checkpoint.preflight_report_path = selected.preflight_report_path
                checkpoint.validation_report_path = selected.validation_report_path
                last_selected_candidate_id = selected.candidate.candidate_id

                print(f"[PATCH] Selected candidate {selected.candidate.candidate_id} (Score: {selected.score:.2f})")
                self._restore_shadow_checkpoint(shadow_path, checkpoint.checkpoint_path)
                final_preflight = await self._patch_engine.preflight_patch_candidate(
                    shadow_path,
                    selected.candidate,
                    allowed_files=allowed_files_set,
                )
                if not final_preflight.success:
                    failure_code = final_preflight.issues[0].code.value if final_preflight.issues else "unknown"
                    print(f"[ERROR] Preflight rejected: {final_preflight.issues[0].message if final_preflight.issues else 'Unknown preflight error'}")
                    logger.warning(
                        "Step preflight rejected",
                        extra={
                            "task_id": task.task_id,
                            "step_id": step.id,
                            "attempt": attempt,
                            "result": "preflight_failed",
                            "failure_code": failure_code,
                        },
                    )
                    last_failure = self._last_failure_from_issues(final_preflight.issues)
                    trace_entries.append(
                        StepExecutionTrace(
                            step_id=step.id,
                            attempt=attempt,
                            status="preflight_failed",
                            candidate_id=selected.candidate.candidate_id,
                            checkpoint_id=checkpoint.checkpoint_id,
                            issues=final_preflight.issues,
                            score=selected.score,
                            message="selected candidate preflight failed",
                        )
                    )
                    last_result_diagnostics = [
                        *persistent_diagnostics,
                        *self._issues_to_diagnostics(final_preflight.issues),
                    ]
                    task.modified_files = previous_modified_files
                    checkpoints.append(checkpoint)
                    continue

                print(f"[PATCH] Applying patch {selected.candidate.candidate_id}...")

                async def _incremental_check(files: list[str]) -> ValidationResult:
                    return await self._run_fast_validation(str(shadow_path), files)

                patch_result = await self._patch_engine.apply_patch_candidate(
                    shadow_path,
                    selected.candidate,
                    allowed_files=allowed_files_set,
                    on_patch_event=lambda ev: self.broadcaster.broadcast(task.task_id, ev),
                    incremental_validator=_incremental_check,
                )
                touched = patch_result.touched_files
                touched_files_result = touched
                trace_entries.append(
                    StepExecutionTrace(
                        step_id=step.id,
                        attempt=attempt,
                        status="patch_applied",
                        candidate_id=selected.candidate.candidate_id,
                        checkpoint_id=checkpoint.checkpoint_id,
                        score=selected.score,
                        preflight_summary={"success": True},
                        message="selected candidate applied",
                    )
                )
                
                print(f"[VALIDATE] Running fast validation on {len(touched)} files...")
                validation = await self._run_fast_validation(str(shadow_path), touched)
                validation_path = self._write_debug_artifact(
                    task.task_id,
                    "validation-selected",
                    validation.model_dump(mode="json"),
                    step_id=step.id,
                    attempt=attempt,
                    artifacts_root_path=task.artifacts_root_path,
                )
                checkpoint.validation_report_path = validation_path or checkpoint.validation_report_path
                checkpoint.file_hashes_after = self._hash_files(
                    shadow_path,
                    tracked_files=allowed_files,
                )
                checkpoints.append(checkpoint)

                if validation.success:
                    print(f"[SUCCESS] Step {step.id} completed successfully")
                    logger.info(
                        "Step completed",
                        extra={
                            "task_id": task.task_id,
                            "step_id": step.id,
                            "attempt": attempt,
                            "result": "step_completed",
                        },
                    )
                    trace_entries.append(
                        StepExecutionTrace(
                            step_id=step.id,
                            attempt=attempt,
                            status="step_completed",
                            candidate_id=selected.candidate.candidate_id,
                            checkpoint_id=checkpoint.checkpoint_id,
                            score=selected.score,
                            preflight_summary=selected.breakdown.model_dump(mode="json"),
                            validation_summary=validation.model_dump(mode="json"),
                            message="step validation passed",
                            artifacts={
                                "ranking": ranking_path or "",
                                "preflight": selected.preflight_report_path or "",
                                "validation": checkpoint.validation_report_path or "",
                            },
                        )
                    )
                    return StepRunResult(
                        step_id=step.id,
                        outcome="step_completed",
                        validation_result="validation_passed",
                        attempts_used=attempt,
                        selected_candidate_id=selected.candidate.candidate_id,
                        touched_files=touched,
                        diagnostics=[*persistent_diagnostics],
                        trace_entries=trace_entries,
                        checkpoint_manifests=checkpoints,
                        last_failure=last_failure,
                    )

                print(f"[ERROR] Validation failed for step {step.id}")
                if validation.diagnostics:
                    for diag in validation.diagnostics[:3]:
                        print(f"  - {diag.message}")

                last_failure = {
                    "failure_code": PatchFailureCode.APPLY_ERROR.value,
                    "file": None,
                    "op_id": None,
                    "excerpt": "\n".join(d.message for d in validation.diagnostics[:10]),
                }
                logger.warning(
                    "Step validation failed",
                    extra={
                        "task_id": task.task_id,
                        "step_id": step.id,
                        "attempt": attempt,
                        "result": "validation_failed",
                        "failure_code": PatchFailureCode.APPLY_ERROR.value,
                    },
                )
                trace_entries.append(
                    StepExecutionTrace(
                        step_id=step.id,
                        attempt=attempt,
                        status="validation_failed",
                        candidate_id=selected.candidate.candidate_id,
                        checkpoint_id=checkpoint.checkpoint_id,
                        score=selected.score,
                        validation_summary=validation.model_dump(mode="json"),
                        message="selected candidate validation failed",
                    )
                )
                last_result_diagnostics = [*persistent_diagnostics, *validation.diagnostics]
                self._restore_shadow_checkpoint(shadow_path, checkpoint.checkpoint_path)
                task.modified_files = previous_modified_files
                checkpoints.append(checkpoint)
            except Exception as exc:
                logger.exception(
                    "Iteration failed while applying/validating patch",
                    extra={
                        "task_id": task.task_id,
                        "step_id": step.id,
                        "attempt": attempt,
                        "result": "validation_failed",
                        "failure_code": PatchFailureCode.APPLY_ERROR.value,
                    },
                )
                issue = PatchPreflightIssue(
                    code=PatchFailureCode.APPLY_ERROR,
                    file=None,
                    message=str(exc),
                )
                last_failure = self._last_failure_from_issues([issue])
                trace_entries.append(
                    StepExecutionTrace(
                        step_id=step.id,
                        attempt=attempt,
                        status="validation_failed",
                        issues=[issue],
                        checkpoint_id=checkpoint.checkpoint_id,
                        message="internal apply/validation error",
                    )
                )
                last_result_diagnostics = [
                    *persistent_diagnostics,
                    *self._issues_to_diagnostics([issue]),
                ]
                self._restore_shadow_checkpoint(shadow_path, checkpoint.checkpoint_path)
                task.modified_files = previous_modified_files
                checkpoints.append(checkpoint)

        trace_entries.append(
            StepExecutionTrace(
                step_id=step.id,
                attempt=self._max_attempts_per_step,
                status="step_exhausted",
                message="step attempts exhausted",
            )
        )
        logger.error(
            "Step attempts exhausted",
            extra={
                "task_id": task.task_id,
                "step_id": step.id,
                "attempt": self._max_attempts_per_step,
                "result": "step_exhausted",
            },
        )
        return StepRunResult(
            step_id=step.id,
            outcome="attempts_exhausted",
            validation_result="validation_failed",
            attempts_used=self._max_attempts_per_step,
            selected_candidate_id=last_selected_candidate_id,
            touched_files=touched_files_result,
            diagnostics=last_result_diagnostics,
            trace_entries=trace_entries,
            checkpoint_manifests=checkpoints,
            last_failure=last_failure,
        )

    async def _collect_baseline_errors(
        self,
        shadow_path: Path,
        task_id: str | None = None,
        artifacts_root_path: str | None = None,
    ) -> frozenset[str]:
        """Run the full validator before any patches to record pre-existing errors.

        Returns the set of error messages already present so they can be filtered
        from post-patch validation results.  Failures here are non-fatal — if the
        baseline run itself errors we just return an empty set and proceed normally.
        """
        try:
            result = await self._validator.run(str(shadow_path))
            errors = frozenset(
                self._normalize_error_message(d.message)
                for d in result.diagnostics
                if d.level == "error"
            )
            if task_id:
                self._write_debug_artifact(
                    task_id,
                    "baseline-validation",
                    {
                        "success": result.success,
                        "baseline_error_count": len(errors),
                        "diagnostics": result.model_dump(mode="json")["diagnostics"],
                    },
                    artifacts_root_path=artifacts_root_path,
                )
            return errors
        except Exception:
            logger.warning("Baseline validation run failed; proceeding without baseline filtering")
            return frozenset()

    @staticmethod
    def _normalize_error_message(msg: str) -> str:
        """Produce a stable fingerprint for an error message across repeated runs.

        pytest embeds volatile data throughout its output (tmp dir run numbers,
        inner task UUIDs, elapsed times) making the full string comparison fail
        even when the same tests fail. For pytest output we extract only the
        FAILED test IDs from the short summary section — these are deterministic.
        For other validators (mypy, ruff) we apply light normalization.
        """
        import re
        # Detect pytest output by its short summary sentinel
        if "short test summary info" in msg:
            failed_lines = re.findall(r"^FAILED\s+(\S+)", msg, re.MULTILINE)
            if failed_lines:
                return "pytest:FAILED:" + ",".join(sorted(failed_lines))
            # No FAILED lines means zero failures; treat as empty (shouldn't reach here)
            return "pytest:FAILED:"
        # Strip pytest/cargo timing: "N error(s) in X.XXs" at end of output
        msg = re.sub(r"\d+ errors? in \d+\.\d+s\s*$", "", msg, flags=re.MULTILINE).rstrip()
        # Strip compiler/linter line:col numbers so shifted lines still match
        msg = re.sub(r"(?m)(:\d+){1,2}(?=:|\s|$)", "", msg)
        return msg

    def _filter_baseline_errors(
        self, result: ValidationResult, baseline: frozenset[str]
    ) -> ValidationResult:
        """Remove errors that were already present before patching started."""
        if not baseline:
            return result
        filtered = [
            d for d in result.diagnostics
            if not (d.level == "error" and self._normalize_error_message(d.message) in baseline)
        ]
        return ValidationResult(
            success=not any(d.level == "error" for d in filtered),
            diagnostics=filtered,
            duration_ms=result.duration_ms,
        )

    async def _run_fast_validation(
        self,
        workspace_path: str,
        touched_files: list[str],
    ) -> ValidationResult:
        run_touched = getattr(self._validator, "run_touched", None)
        if callable(run_touched):
            return await run_touched(workspace_path, touched_files)

        diagnostics: list[Diagnostic] = []
        root = Path(workspace_path)
        for rel in touched_files:
            candidate = root / rel
            if candidate.suffix != ".py" or not candidate.exists():
                continue
            try:
                source = candidate.read_text(encoding="utf-8")
                compile(source, str(candidate), "exec")
            except Exception as exc:
                diagnostics.append(
                    Diagnostic(
                        source="validator:fast-python-compile",
                        message=f"{candidate}: {exc}",
                        level="error",
                    )
                )
        return ValidationResult(
            success=not diagnostics,
            diagnostics=diagnostics,
            duration_ms=0,
        )

    def _collect_file_contents(self, shadow_path: Path, allowed_files: list[str]) -> dict[str, str]:
        contents: dict[str, str] = {}
        for rel_path in allowed_files:
            abs_path = shadow_path / rel_path
            if not abs_path.exists() or not abs_path.is_file():
                continue
            try:
                lines = abs_path.read_text(encoding="utf-8").splitlines()
                contents[rel_path] = "\n".join(f"{i+1:4d}: {line}" for i, line in enumerate(lines))
            except OSError:
                continue
        return contents

    def _collect_chunk_scoped_contents(
        self,
        shadow_path: Path,
        allowed_files: list[str],
        step_goal: str,
        retrieval_context: RetrievalContext,
    ) -> dict[str, str]:
        """Return line-numbered file contents scoped to semantically relevant chunks.

        For each allowed file:
        - If semantic chunks exist for that file: include only the chunk line ranges
          (+ 4 context lines each side) with omission markers between gaps.
        - If no chunks: fall back to full file content.

        Original line numbers are preserved so replace_range/apply_diff patch ops remain precise.
        """
        if not retrieval_context.semantic_chunks:
            return self._collect_file_contents(shadow_path, allowed_files)

        allowed_set = set(allowed_files)
        chunks_by_path: dict[str, list[ScoredChunk]] = {}
        for sc in retrieval_context.semantic_chunks:
            if sc.chunk.path in allowed_set:
                chunks_by_path.setdefault(sc.chunk.path, []).append(sc)

        contents: dict[str, str] = {}
        _CONTEXT = 4

        for rel_path in allowed_files:
            abs_path = shadow_path / rel_path
            if not abs_path.exists() or not abs_path.is_file():
                continue
            try:
                lines = abs_path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue

            file_chunks = chunks_by_path.get(rel_path)
            if not file_chunks:
                contents[rel_path] = "\n".join(
                    f"{i+1:4d}: {line}" for i, line in enumerate(lines)
                )
                continue

            ranges: list[tuple[int, int]] = []
            for sc in file_chunks:
                start0 = max(0, sc.chunk.line_start - 1 - _CONTEXT)
                end0 = min(len(lines), sc.chunk.line_end + _CONTEXT)
                ranges.append((start0, end0))

            ranges.sort()
            merged: list[tuple[int, int]] = []
            for s, e in ranges:
                if merged and s <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], e))
                else:
                    merged.append((s, e))

            parts: list[str] = []
            prev_end = 0
            for start0, end0 in merged:
                if start0 > prev_end:
                    parts.append(f"... (lines {prev_end + 1}–{start0} omitted) ...")
                numbered = "\n".join(
                    f"{start0 + j + 1:4d}: {line}"
                    for j, line in enumerate(lines[start0:end0])
                )
                parts.append(numbered)
                prev_end = end0

            if prev_end < len(lines):
                parts.append(f"... (lines {prev_end + 1}–{len(lines)} omitted) ...")

            contents[rel_path] = "\n".join(parts)

        return contents

    async def _create_patch_document(
        self,
        task: TaskRecord,
        workspace_path: str,
        diagnostics: list[Diagnostic],
        retrieval_context: dict[str, object],
        *,
        current_step: PlanStep | None,
        allowed_files: list[str],
        max_ops: int,
        max_files: int,
        candidate_count: int,
        last_failure: dict[str, object] | None,
    ) -> object:
        return await self._reasoning_engine.create_patch(
            task,
            workspace_path,
            diagnostics,
            retrieval_context,
            current_step=current_step,
            allowed_files=allowed_files,
            max_ops=max_ops,
            max_files=max_files,
            candidate_count=candidate_count,
            last_failure=last_failure,
        )

    async def _evaluate_candidates(
        self,
        *,
        task: TaskRecord,
        step: PlanStep,
        attempt: int,
        patch_document: PatchDocumentV2,
        shadow_path: Path,
        checkpoint: CheckpointManifest,
        allowed_files: set[str],
        max_ops: int,
        max_files: int,
    ) -> tuple[list[_CandidateEvaluation], str | None]:
        evaluations: list[_CandidateEvaluation] = []
        for candidate in patch_document.candidates:
            self._restore_shadow_checkpoint(shadow_path, checkpoint.checkpoint_path)
            evaluation = await self._evaluate_single_candidate(
                task=task,
                step=step,
                attempt=attempt,
                candidate=candidate,
                shadow_path=shadow_path,
                checkpoint=checkpoint,
                allowed_files=allowed_files,
                max_ops=max_ops,
                max_files=max_files,
            )
            evaluations.append(evaluation)

        selected = self._select_best_candidate(evaluations)
        selected_id = selected.candidate.candidate_id if selected else None
        ranking_payload = {
            "step_id": step.id,
            "attempt": attempt,
            "selected_candidate_id": selected_id,
            "candidates": [
                {
                    "candidate_id": item.candidate.candidate_id,
                    "score": item.score,
                    "preflight_pass": item.preflight_pass,
                    "validation_pass": item.validation_pass,
                    "changed_lines": item.changed_lines,
                    "touched_files": item.touched_files,
                    "new_file_count": item.new_file_count,
                    "issues": [issue.model_dump(mode="json") for issue in item.preflight_issues],
                    "selected": item.candidate.candidate_id == selected_id,
                }
                for item in evaluations
            ],
        }
        ranking_path = self._write_debug_artifact(
            task.task_id,
            "ranking",
            ranking_payload,
            step_id=step.id,
            attempt=attempt,
            artifacts_root_path=task.artifacts_root_path,
        )
        return evaluations, ranking_path

    async def _evaluate_single_candidate(
        self,
        *,
        task: TaskRecord,
        step: PlanStep,
        attempt: int,
        candidate: PatchCandidateV2,
        shadow_path: Path,
        checkpoint: CheckpointManifest,
        allowed_files: set[str],
        max_ops: int,
        max_files: int,
    ) -> _CandidateEvaluation:
        op_count = len(candidate.patch_ops)
        candidate_files = sorted({op.file for op in candidate.patch_ops})
        if op_count > max_ops or len(candidate_files) > max_files:
            issue = PatchPreflightIssue(
                code=PatchFailureCode.SCOPE_VIOLATION,
                file=candidate_files[0] if candidate_files else None,
                message=(
                    f"Candidate '{candidate.candidate_id}' exceeds limits: "
                    f"ops={op_count}/{max_ops}, files={len(candidate_files)}/{max_files}"
                ),
            )
            breakdown = self._score_candidate(
                preflight_pass=False,
                validation_pass=False,
                changed_lines=0,
                op_count=op_count,
                new_file_count=0,
            )
            return _CandidateEvaluation(
                candidate=candidate,
                score=breakdown.score,
                breakdown=breakdown,
                preflight_issues=[issue],
                validation=None,
                touched_files=[],
                changed_lines=0,
                new_file_count=0,
                preflight_report_path=None,
                validation_report_path=None,
            )

        preflight = await self._patch_engine.preflight_patch_candidate(
            shadow_path,
            candidate,
            allowed_files=allowed_files,
        )
        preflight_path = self._write_debug_artifact(
            task.task_id,
            f"preflight-{candidate.candidate_id}",
            preflight.model_dump(mode="json"),
            step_id=step.id,
            attempt=attempt,
            artifacts_root_path=task.artifacts_root_path,
        )
        if not preflight.success:
            breakdown = self._score_candidate(
                preflight_pass=False,
                validation_pass=False,
                changed_lines=0,
                op_count=op_count,
                new_file_count=0,
            )
            return _CandidateEvaluation(
                candidate=candidate,
                score=breakdown.score,
                breakdown=breakdown,
                preflight_issues=preflight.issues,
                validation=None,
                touched_files=[],
                changed_lines=0,
                new_file_count=0,
                preflight_report_path=preflight_path,
                validation_report_path=None,
            )

        try:
            patch_result = await self._patch_engine.apply_patch_candidate(
                shadow_path,
                candidate,
                allowed_files=allowed_files,
            )
        except Exception as exc:
            validation = ValidationResult(
                success=False,
                diagnostics=[
                    Diagnostic(
                        source="patch_apply",
                        message=str(exc),
                        level="error",
                    )
                ],
                duration_ms=0,
            )
            validation_path = self._write_debug_artifact(
                task.task_id,
                f"validation-{candidate.candidate_id}",
                validation.model_dump(mode="json"),
                step_id=step.id,
                attempt=attempt,
                artifacts_root_path=task.artifacts_root_path,
            )
            breakdown = self._score_candidate(
                preflight_pass=True,
                validation_pass=False,
                changed_lines=0,
                op_count=op_count,
                new_file_count=0,
            )
            return _CandidateEvaluation(
                candidate=candidate,
                score=breakdown.score,
                breakdown=breakdown,
                preflight_issues=[],
                validation=validation,
                touched_files=[],
                changed_lines=0,
                new_file_count=0,
                preflight_report_path=preflight_path,
                validation_report_path=validation_path,
            )

        touched_files = patch_result.touched_files
        validation = await self._run_fast_validation(str(shadow_path), touched_files)
        validation_path = self._write_debug_artifact(
            task.task_id,
            f"validation-{candidate.candidate_id}",
            validation.model_dump(mode="json"),
            step_id=step.id,
            attempt=attempt,
            artifacts_root_path=task.artifacts_root_path,
        )

        checkpoint_snapshot = Path(checkpoint.checkpoint_path)
        changed_lines = self._count_changed_lines(
            checkpoint_snapshot,
            shadow_path,
            touched_files,
        )
        new_file_count = self._count_new_files(
            checkpoint_snapshot,
            shadow_path,
            touched_files,
        )
        breakdown = self._score_candidate(
            preflight_pass=True,
            validation_pass=validation.success,
            changed_lines=changed_lines,
            op_count=op_count,
            new_file_count=new_file_count,
        )
        return _CandidateEvaluation(
            candidate=candidate,
            score=breakdown.score,
            breakdown=breakdown,
            preflight_issues=[],
            validation=validation,
            touched_files=touched_files,
            changed_lines=changed_lines,
            new_file_count=new_file_count,
            preflight_report_path=preflight_path,
            validation_report_path=validation_path,
        )

    def _select_best_candidate(
        self,
        evaluations: list[_CandidateEvaluation],
    ) -> _CandidateEvaluation | None:
        if not evaluations:
            return None

        # Filter candidates that pass preflight
        passing_candidates = [e for e in evaluations if e.preflight_pass]
        
        if not passing_candidates:
            # If none pass preflight, select the one with highest score (fallback)
            passing_candidates = evaluations
        
        def sort_key(item: _CandidateEvaluation) -> tuple[float, int, int, str]:
            touched_files_count = len(item.touched_files) if item.touched_files else len(
                {op.file for op in item.candidate.patch_ops}
            )
            return (-item.score, touched_files_count, item.changed_lines, item.candidate.candidate_id)

        return sorted(passing_candidates, key=sort_key)[0]

    def _score_candidate(
        self,
        *,
        preflight_pass: bool,
        validation_pass: bool,
        changed_lines: int,
        op_count: int,
        new_file_count: int,
    ) -> CandidateScoreBreakdown:
        score = 0.0
        if preflight_pass:
            score += 100.0
        if validation_pass:
            score += 60.0
        score -= 0.05 * float(changed_lines)
        score -= 2.0 * float(op_count)
        score -= 5.0 * float(new_file_count)
        return CandidateScoreBreakdown(
            preflight_pass=preflight_pass,
            validation_pass=validation_pass,
            changed_lines=changed_lines,
            op_count=op_count,
            new_file_count=new_file_count,
            score=score,
        )

    def _count_changed_lines(
        self,
        checkpoint_snapshot: Path,
        shadow_path: Path,
        touched_files: list[str],
    ) -> int:
        changed = 0
        for rel in touched_files:
            before_path = checkpoint_snapshot / rel
            after_path = shadow_path / rel
            before = before_path.read_text(encoding="utf-8").splitlines() if before_path.exists() else []
            after = after_path.read_text(encoding="utf-8").splitlines() if after_path.exists() else []
            for line in difflib.ndiff(before, after):
                if line.startswith("+ ") or line.startswith("- "):
                    changed += 1
        return changed

    def _count_new_files(
        self,
        checkpoint_snapshot: Path,
        shadow_path: Path,
        touched_files: list[str],
    ) -> int:
        count = 0
        for rel in touched_files:
            if not (checkpoint_snapshot / rel).exists() and (shadow_path / rel).exists():
                count += 1
        return count

    def _issues_to_diagnostics(self, issues: list[PatchPreflightIssue]) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []
        for issue in issues:
            diagnostics.append(
                Diagnostic(
                    source=f"patch_preflight:{issue.code.value}",
                    message=issue.message,
                    level="error",
                    file=issue.file,
                )
            )
        return diagnostics

    def _last_failure_from_issues(self, issues: list[PatchPreflightIssue]) -> dict[str, object] | None:
        if not issues:
            return None
        issue = issues[0]
        
        # Add specific guidance based on error type
        guidance = self._get_error_guidance(issue.code, issue.message, issue.file)
        
        return {
            "failure_code": issue.code.value,
            "file": issue.file,
            "op_id": issue.op_index,
            "excerpt": issue.message,
            "guidance": guidance,
            "suggested_fix": self._get_suggested_fix(issue.code, issue.message, issue.file),
        }
    
    def _get_error_guidance(self, failure_code: PatchFailureCode, message: str, file: str | None) -> str:
        """Provide specific guidance for different error types."""
        if failure_code == PatchFailureCode.ANCHOR_MISSING:
            return "The search text or symbol selector was not found. Check if the file content has changed since patch generation."
        
        elif failure_code == PatchFailureCode.ANCHOR_AMBIGUOUS:
            return "The search text appears multiple times. Use more specific context or a symbol selector for unique matching."
        
        elif failure_code == PatchFailureCode.PARSER_UNAVAILABLE:
            return "Tree-sitter parser not installed. AST operations require language-specific parsers. Consider using search_replace instead."
        
        elif failure_code == PatchFailureCode.APPLY_ERROR and "Hunk context mismatch" in message:
            return "Diff header line count doesn't match actual context. This is a model generation error - try search_replace instead."
        
        elif failure_code == PatchFailureCode.RANGE_INVALID:
            return "Line numbers in patch are out of range. The file may be shorter than expected."
        
        elif failure_code == PatchFailureCode.SCOPE_VIOLATION:
            return "Patch operation targets file outside current step scope. Check allowed_files constraint."
        
        else:
            return "Patch validation failed. Review the specific error message for details."
    
    def _get_suggested_fix(self, failure_code: PatchFailureCode, message: str, file: str | None) -> str:
        """Suggest specific fixes for different error types."""
        if failure_code == PatchFailureCode.ANCHOR_MISSING:
            return "Use search_replace with more context or verify the exact text exists in the file."
        
        elif failure_code == PatchFailureCode.ANCHOR_AMBIGUOUS:
            return "Include more surrounding context in search text to make it unique."
        
        elif failure_code == PatchFailureCode.PARSER_UNAVAILABLE:
            return "Switch to search_replace operation for text-based replacement."
        
        elif failure_code == PatchFailureCode.APPLY_ERROR and "Hunk context mismatch" in message:
            return "Prefer search_replace over apply_diff for this type of change."
        
        elif failure_code == PatchFailureCode.RANGE_INVALID:
            return "Check the actual file line count and adjust patch accordingly."
        
        elif failure_code == PatchFailureCode.SCOPE_VIOLATION:
            return "Ensure all modified files are in the allowed_files list."
        
        else:
            return "Review patch operation and try a different approach or operation type."

    def _artifacts_root(
        self,
        task_id: str,
        workspace_path: str | None = None,
        artifacts_root_path: str | None = None,
    ) -> Path:
        if artifacts_root_path:
            return Path(artifacts_root_path)
        return task_artifacts_root(task_id, workspace_path)

    def _write_debug_artifact(
        self,
        task_id: str,
        name: str,
        payload: object,
        *,
        step_id: str | None = None,
        attempt: int | None = None,
        workspace_path: str | None = None,
        artifacts_root_path: str | None = None,
    ) -> str | None:
        try:
            root = self._artifacts_root(
                task_id,
                workspace_path=workspace_path,
                artifacts_root_path=artifacts_root_path,
            )
            if step_id:
                root = root / f"step-{step_id}"
            if attempt is not None:
                root = root / f"attempt-{attempt}"
            root.mkdir(parents=True, exist_ok=True)
            output_path = root / f"{name}.json"
            output_path.write_text(
                json.dumps(payload, indent=2, default=str),
                encoding="utf-8",
            )
            return str(output_path)
        except Exception:
            logger.debug("failed to write debug artifact", exc_info=True)
            return None

    def _create_shadow_checkpoint(
        self,
        task: TaskRecord,
        step: PlanStep,
        attempt: int,
        shadow_path: Path,
        *,
        tracked_files: list[str],
    ) -> CheckpointManifest:
        checkpoint_id = f"{step.id}-{attempt}-{uuid4().hex[:8]}"
        checkpoint_root = shadow_path.parent / "_checkpoints" / task.task_id / f"step-{step.id}"
        attempt_root = checkpoint_root / f"attempt-{attempt}"
        snapshot_path = attempt_root / checkpoint_id / "shadow"
        if attempt_root.exists():
            shutil.rmtree(attempt_root)
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(shadow_path, snapshot_path)

        return CheckpointManifest(
            task_id=task.task_id,
            step_id=step.id,
            attempt=attempt,
            checkpoint_id=checkpoint_id,
            checkpoint_path=str(snapshot_path),
            shadow_path=str(shadow_path),
            file_hashes_before=self._hash_files(shadow_path, tracked_files=tracked_files),
        )

    def _restore_shadow_checkpoint(self, shadow_path: Path, checkpoint_path: str) -> None:
        snapshot_path = Path(checkpoint_path)
        if shadow_path.exists():
            shutil.rmtree(shadow_path)
        shutil.copytree(snapshot_path, shadow_path)

    def _hash_files(
        self,
        root: Path,
        *,
        tracked_files: list[str],
    ) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for rel in sorted(set(tracked_files)):
            path = root / rel
            if not path.exists() or not path.is_file():
                hashes[rel] = "__missing__"
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            hashes[rel] = digest
        return hashes

    def _append_checkpoint(self, task: TaskRecord, checkpoint: CheckpointManifest) -> None:
        for item in task.checkpoints:
            if item.checkpoint_id == checkpoint.checkpoint_id:
                return
        task.checkpoints.append(checkpoint)

    def _collect_workspace_file_index(self, workspace_path: Path) -> list[str]:
        skip_dirs = {
            ".git",
            ".venv",
            "node_modules",
            "target",
            "dist",
            "__pycache__",
            ".mypy_cache",
            ".pytest_cache",
            ".tox",
            ".idea",
            ".vscode",
            "build",
            "tmp",
            "out",
            "coverage",
            ".agentd",
            ".ai-editor",
        }
        indexed: list[str] = []
        for root, dirs, files in os.walk(workspace_path):
            dirs[:] = sorted(d for d in dirs if d not in skip_dirs)
            for file_name in sorted(files):
                relative = str((Path(root) / file_name).relative_to(workspace_path))
                indexed.append(relative)
                if len(indexed) >= 15000:
                    return indexed
        return indexed

    def _find_unresolved_plan_targets(
        self,
        plan: PlanDocument,
        workspace_files_set: set[str],
        workspace_files_index: list[str],
    ) -> list[dict[str, str | None]]:
        unresolved: list[dict[str, str | None]] = []
        for step in plan.steps:
            for target in step.target_paths():
                if target in workspace_files_set:
                    continue
                target_intent = step.target_intent_for(target)
                if self._allow_missing_plan_target(step, target):
                    continue
                unresolved.append(
                    {
                        "step_id": step.id,
                        "target": target,
                        "suggestion": self._suggest_workspace_path(target, workspace_files_index),
                        "reason": (
                            "missing_target_intent"
                            if target_intent is None
                            else "missing_existing_target"
                        ),
                    }
                )
        return unresolved

    def _allow_missing_plan_target(self, step: PlanStep, target: str) -> bool:
        return step.target_intent_for(target) == PlanTargetIntent.NEW

    def _suggest_workspace_path(
        self,
        target: str,
        workspace_files_index: list[str],
    ) -> str | None:
        target_name = Path(target).name
        by_name = [candidate for candidate in workspace_files_index if Path(candidate).name == target_name]
        if len(by_name) == 1:
            return by_name[0]
        close = difflib.get_close_matches(target, workspace_files_index, n=1, cutoff=0.6)
        return close[0] if close else None

    def _plan_target_diagnostics(
        self,
        unresolved_targets: list[dict[str, str | None]],
    ) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []
        for item in unresolved_targets[:8]:
            target = item["target"] or "<unknown>"
            step_id = item["step_id"] or "<unknown>"
            suggestion = item["suggestion"]
            reason = item.get("reason")
            if reason == "missing_target_intent":
                message = (
                    f"Plan step {step_id} references missing target without explicit intent: {target}. "
                    "Add target intent in step.targets as {'path': ..., 'intent': 'existing'|'new'}."
                )
            elif reason == "missing_existing_target":
                message = f"Plan step {step_id} references missing target marked existing: {target}"
            else:
                message = f"Plan step {step_id} references missing target: {target}"
            if suggestion:
                message += f" (did you mean: {suggestion})"
            diagnostics.append(
                Diagnostic(
                    source="plan_target_validation",
                    message=message,
                    level="error",
                    file=target,
                )
            )
        return diagnostics

    def _critique_diagnostics(
        self,
        issues: list[PlanCritiqueIssue],
        *,
        source: str,
    ) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []
        for issue in issues[:10]:
            diagnostics.append(
                Diagnostic(
                    source=f"{source}:{issue.code.value}",
                    message=issue.message,
                    level="warning",
                    file=issue.file,
                )
            )
        return diagnostics

    def _validate_plan_grounding(
        self,
        plan: PlanDocument,
        approved_markdown: str,
        workspace_files_set: set[str],
        workspace_files_index: list[str],
        planner_evidence: PlanEvidencePack,
    ) -> tuple[list[dict[str, str | None]], list[PlanCritiqueIssue]]:
        unresolved_targets: list[dict[str, str | None]] = []
        issues: list[PlanCritiqueIssue] = []
        approved_existing_files = set(
            self._extract_markdown_file_mentions(approved_markdown, workspace_files_index)
        )
        step_target_files = {target for step in plan.steps for target in step.target_paths()}
        created_files: set[str] = set()

        for step in plan.steps:
            for target in step.target_paths():
                target_intent = step.target_intent_for(target)
                if target in workspace_files_set or target in created_files:
                    if target_intent == PlanTargetIntent.NEW and target in workspace_files_set:
                        issues.append(
                            PlanCritiqueIssue(
                                code="schema_mismatch",
                                message=(
                                    f"Target '{target}' is marked as new, but it already exists in the workspace."
                                ),
                                file=target,
                            )
                        )
                elif self._allow_missing_plan_target(step, target):
                    created_files.add(target)
                else:
                    unresolved_targets.append(
                        {
                            "step_id": step.id,
                            "target": target,
                            "suggestion": self._suggest_workspace_path(target, workspace_files_index),
                            "reason": (
                                "missing_target_intent"
                                if target_intent is None
                                else "missing_existing_target"
                            ),
                        }
                    )

                if (
                    approved_existing_files
                    and target in workspace_files_set
                    and target not in approved_existing_files
                ):
                    issues.append(
                        PlanCritiqueIssue(
                            code="path_prefix_mismatch",
                            message=(
                                f"JSON plan target '{target}' is not part of the approved markdown blueprint."
                            ),
                            file=target,
                            evidence=", ".join(sorted(approved_existing_files)[:6]) or None,
                        )
                    )

        allowed_expected_missing = {
            target
            for step in plan.steps
            for target in step.target_paths()
            if step.target_intent_for(target) == PlanTargetIntent.NEW
        }
        for expected_file in plan.expected_files:
            if expected_file in workspace_files_set or expected_file in allowed_expected_missing:
                continue
            if expected_file in step_target_files:
                continue
            issues.append(
                PlanCritiqueIssue(
                    code="invented_file",
                    message=(
                        f"expected_files includes '{expected_file}' without evidence that it exists or will be created."
                    ),
                    file=expected_file,
                    evidence=self._suggest_workspace_path(expected_file, workspace_files_index),
                )
            )

        mentioned_verification_files = self._extract_markdown_file_mentions(
            "\n".join(plan.stop_conditions),
            workspace_files_index,
        )
        for verification_file in mentioned_verification_files:
            if verification_file not in workspace_files_set:
                issues.append(
                    PlanCritiqueIssue(
                        code="verification_mismatch",
                        message=f"Stop condition references missing verification file '{verification_file}'.",
                        file=verification_file,
                    )
                )

        issues.extend(
            self._planning_adapter.additional_grounding_issues(
                plan=plan,
                approved_markdown=approved_markdown,
                workspace_files_index=workspace_files_index,
                planner_evidence=planner_evidence,
            )
        )

        return unresolved_targets, self._dedupe_critique_issues(issues)

    def _extract_markdown_file_mentions(
        self,
        text: str,
        workspace_files_index: list[str],
    ) -> list[str]:
        if not text:
            return []
        mentions: list[str] = []
        seen: set[str] = set()
        for candidate in workspace_files_index:
            if candidate in text and candidate not in seen:
                seen.add(candidate)
                mentions.append(candidate)
        return mentions

    def _dedupe_critique_issues(
        self,
        issues: list[PlanCritiqueIssue],
    ) -> list[PlanCritiqueIssue]:
        deduped: list[PlanCritiqueIssue] = []
        seen: set[tuple[str, str | None, str]] = set()
        for issue in issues:
            key = (issue.code.value, issue.file, issue.message)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(issue)
        return deduped

    def _truncate_text(self, text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[:limit] + "\n...[truncated]"
