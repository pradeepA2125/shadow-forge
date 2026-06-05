from __future__ import annotations

import asyncio
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

import dataclasses

from agentd.domain.models import (
    CandidateScoreBreakdown,
    CheckpointManifest,
    DeltaReplanRequest,
    DiffEntry,
    Diagnostic,
    InlineChangeResult,
    PatchCandidateV2,
    PatchDocumentV2,
    PatchFailureCode,
    PatchPreflightIssue,
    PlanDocument,
    PlanRevisionResult,
    PlanStep,
    PlanTarget,
    PlanTargetIntent,
    ScopeExtensionRequest,
    CommandApprovalRequest,
    CommandDecision,
    CommandRule,
    ScopePolicy,
    ScopeRemember,
    ShellPolicy,
    ScopeTrigger,
    StepExecutionTrace,
    StepReviewPayload,
    StepRunResult,
    TaskBudget,
    TaskMilestoneSnapshot,
    TaskRecord,
    TaskStatus,
    TaskUsage,
    ValidationResult,
)
from agentd.domain.state_machine import assert_budget, bump_usage, transition
from agentd.env.ensure import EnvProfileEnsurer
from agentd.orchestrator.broadcaster import PatchEventBroadcaster
from agentd.patch.engine import PatchEngine
from agentd.planning.agent import PlanningAgent
from agentd.planning.registry import PlanningToolRegistry
from agentd.reasoning.contracts import ReasoningEngine
from agentd.retrieval.artifact_client import RetrievalContext
from agentd.retrieval.chunker import ScoredChunk
from agentd.runtime.adapters import GenericPlanningAdapter, PlanningAdapter
from agentd.runtime.artifacts import task_artifacts_root
from agentd.storage.base import TaskStore
from agentd.tools.loop import PlanHandoff, ScopeDecision, ScopeExtensionCallback
from agentd.workspace.shadow import ShadowWorkspace, ShadowWorkspaceManager

logger = logging.getLogger(__name__)



_NEARBY_PATTERN_NAMES: frozenset[str] = frozenset({"__init__.py", "conftest.py"})


def _resolve_test_command(
    revised: object,
    reverted_steps: list[PlanStep],
) -> str | None:
    """Pick the test_command for a revised step.

    Prefer the model's explicit value. Fall back by scanning ALL reverted steps
    for a test_command that covers a file the revised step also targets. This
    handles the common delta-replan pattern where multiple steps are collapsed
    into one: the model writes the new step but omits the test_command that was
    on one of the steps being replaced.
    """
    from agentd.domain.models import RevisedStep
    assert isinstance(revised, RevisedStep)
    if revised.test_command:
        return revised.test_command
    revised_paths = {t["path"] for t in revised.targets if isinstance(t, dict)}
    for step in reverted_steps:
        if step.test_command:
            original_paths = {t.path for t in step.targets}
            if revised_paths & original_paths:
                return step.test_command
    return None


def _is_nearby_file(out_of_scope: str, allowed: list[str]) -> bool:
    """True iff `out_of_scope` is plausibly within the spirit of the step's scope:
      - is a conventional Python pattern (__init__.py, conftest.py), OR
      - shares a parent directory with one of the allowed targets, OR
      - lives inside a directory listed as an allowed target.
    """
    if not allowed:
        return False
    out_path = Path(out_of_scope)
    if out_path.name in _NEARBY_PATTERN_NAMES:
        return True
    for t in allowed:
        t_path = Path(t)
        if out_path.parent == t_path.parent:
            return True
        try:
            out_path.relative_to(t_path)
            return True
        except ValueError:
            continue
    return False


def _format_feedback_turn(feedback: str, *, current_plan: str | None = None) -> dict[str, object]:
    """Wrap user plan-feedback as the final turn of the planning conversation.

    Appended after the replayed history so the model reads it as the latest message
    without disturbing the cacheable prefix that precedes it. The current plan is
    embedded so the model can copy exact search snippets for emit_plan_patch and so
    later rounds see the post-patch version (append-only — never mutates an earlier
    history entry, keeping the KV prefix intact).
    """
    body = (
        "The user reviewed your plan and gave this feedback:\n\n"
        f"{feedback}\n\n"
    )
    if current_plan:
        body += (
            "Current plan (edit it with emit_plan_patch search_replace ops, copying exact "
            "unique snippets from below; or emit_plan for a large rewrite):\n\n"
            f"{current_plan}\n\n"
        )
    body += (
        "Revise the plan to address the feedback. Explore further only if the feedback "
        "raises something you have not already examined."
    )
    return {"role": "user", "content": body}


def _merge_validation_results(a: "ValidationResult", b: "ValidationResult") -> "ValidationResult":
    return ValidationResult(
        success=a.success and b.success,
        diagnostics=[*a.diagnostics, *b.diagnostics],
        duration_ms=max(a.duration_ms, b.duration_ms),
    )


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
        scope_policy: ScopePolicy = ScopePolicy.STRICT,
        scope_trigger: ScopeTrigger = ScopeTrigger.NEARBY,
        scope_remember: ScopeRemember = ScopeRemember.TASK,
        scope_timeout_sec: float = 600.0,
        shell_policy: ShellPolicy = ShellPolicy.ASK,
        command_decision_timeout_sec: float = 0.0,
        chat_store: object | None = None,
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
        self._env_ensurer = EnvProfileEnsurer(
            reasoner=self._reasoning_engine,
            broadcaster=self.broadcaster,
        )
        self._running_tasks: set[str] = set()
        self._scope_policy = scope_policy
        self._scope_trigger = scope_trigger
        self._scope_remember = scope_remember
        self._scope_timeout_sec = max(0.0, scope_timeout_sec)
        self._pending_scope_decisions: dict[str, asyncio.Future[ScopeDecision]] = {}
        self._pending_step_decisions: dict[str, asyncio.Future[str]] = {}
        # task_id → future resolved by POST /tasks/{id}/validation-decision (True=accept)
        self._pending_validation_decisions: dict[str, asyncio.Future[bool]] = {}
        self._shell_policy = shell_policy
        self._command_decision_timeout_sec = max(0.0, command_decision_timeout_sec)
        # task_id → future resolved by POST /tasks/{id}/command-decision
        self._pending_command_decisions: dict[str, asyncio.Future[CommandDecision]] = {}
        self._inline_shadows: dict[str, dict[str, object]] = {}  # inline_task_id → meta
        self._chat_store = chat_store
        import os
        self._tool_loop_enabled: bool = os.environ.get("AI_EDITOR_TOOL_LOOP_ENABLED", "true") not in ("0", "false", "False")
        # 0 = wait indefinitely for the user's accept/reject (a deliberate human gate).
        self._validation_decision_timeout_sec = float(
            os.environ.get("AI_EDITOR_VALIDATION_DECISION_TIMEOUT_SEC", "0") or 0
        )

    async def run_task(self, task_id: str) -> TaskRecord:
        task = await self._store.get(task_id)
        await self._env_ensurer.ensure(Path(task.workspace_path), channel_id=task.task_id)
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
                pre_explored_context=task.initial_explore_context or None,
                chat_channel_id=task.chat_channel_id,
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
            # Persist the verbatim planning conversation + the exact initial_context it
            # used, so a feedback round REPLAYS this history (with the feedback appended)
            # against a byte-identical prefix instead of reprefilling from scratch.
            task.planning_conversation_history = planning_result.conversation_history
            task.planning_initial_context = plan_context_payload
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
            if task.chat_channel_id:
                self.broadcaster.broadcast(task.chat_channel_id, {
                    "type": "task_status_changed",
                    "payload": {
                        "task_id": task_id,
                        "status": task.status.value,
                        "plan_markdown": task.plan_markdown,
                    },
                })
            self._write_chat_plan_card(task)
            return task

        except Exception as exc:
            logger.error(f"Task {task_id} failed during initialization", exc_info=True)
            from agentd.planning.loop import PlanningBudgetExceededError
            if isinstance(exc, PlanningBudgetExceededError) and exc.partial_trace is not None:
                self._write_debug_artifact(
                    task_id,
                    "planning-trace-partial",
                    exc.partial_trace.model_dump(mode="json"),
                    artifacts_root_path=task.artifacts_root_path,
                )
            task.diagnostics.append(
                Diagnostic(source="orchestrator", message=str(exc), level="error")
            )
            try:
                task = transition(task, TaskStatus.FAILED, "initialization failed")
            except ValueError:
                pass
            await self._store.save(task)
            self._running_tasks.discard(task_id)
            self.broadcaster.broadcast(task_id, {"type": "done", "payload": {}})
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

            # A feedback round with a pinned planning context reuses round 1's retrieval:
            # the workspace is unchanged since the plan, so a fresh load_context buys
            # nothing — and it can DIVERGE (background re-index / staleness auto-reindex
            # rewrites the snapshot) which would break the cached prompt prefix, while
            # paying for a reindex it then discards. Skip it on that path only; the
            # approval path (and feedback without a pin) still computes fresh retrieval.
            retrieval_context = None
            reuse_pinned_context = bool(feedback and task.planning_initial_context)
            if reuse_pinned_context:
                retrieval_warnings = []
                plan_context_payload: dict[str, object] = {}
            else:
                retrieval_context, retrieval_warnings = self._retrieval_client.load_context(
                    task.workspace_path,
                    task.goal,
                )
                workspace_files_index = self._collect_workspace_file_index(
                    Path(shadow_workspace.shadow_path)
                )
                plan_context_payload = retrieval_context.as_prompt_payload()
                plan_context_payload["workspace_files_index"] = workspace_files_index
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
                # Continue the SAME planning conversation: replay the persisted history
                # and append the feedback as the final turn. Everything before the history
                # (initial_context, pre_explored_context) is pinned to round 1's values so
                # the prompt prefix stays byte-identical and the KV cache is reused.
                pinned_initial_context = task.planning_initial_context or plan_context_payload
                seed_history = [
                    *(task.planning_conversation_history or []),
                    _format_feedback_turn(feedback, current_plan=task.plan_markdown),
                ]
                _plan_patch_scratch = str(Path(task.shadow_workspace_path or ".") / ".plan-patch")
                planning_result = await planning_agent.generate_plan(
                    task=task,
                    initial_context=pinned_initial_context,
                    budget=task.budget,
                    pre_explored_context=task.initial_explore_context or None,
                    chat_channel_id=task.chat_channel_id,
                    seed_history=seed_history,
                    current_plan_markdown=task.plan_markdown,
                    plan_patch_scratch_dir=_plan_patch_scratch,
                    allow_plan_patch=True,
                )
                self._write_debug_artifact(
                    task.task_id,
                    "planning-trace-feedback",
                    planning_result.tool_trace.model_dump(mode="json"),
                    artifacts_root_path=task.artifacts_root_path,
                )
                # Re-persist the grown conversation so the NEXT feedback round replays
                # this round's turns too (append-only across rounds).
                task.planning_conversation_history = planning_result.conversation_history
                task.plan_markdown = planning_result.plan_markdown
                self.broadcaster.broadcast(task.chat_channel_id or task_id, {
                    "type": "plan_diff",
                    "payload": {"task_id": task_id, "plan_markdown": task.plan_markdown},
                })
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
                if task.chat_channel_id:
                    self.broadcaster.broadcast(task.chat_channel_id, {
                        "type": "task_status_changed",
                        "payload": {
                            "task_id": task_id,
                            "status": task.status.value,
                            "plan_markdown": task.plan_markdown,
                        },
                    })
                self._write_chat_plan_card(task)
                return task

            # Approved! Generate JSON plan from Markdown
            print("\n[PLAN] Plan Approved. Generating executable JSON plan...")
            task = transition(task, TaskStatus.PLANNED, "plan approved; starting execution")
            await self._store.save(task)
            self.broadcaster.broadcast(task_id, {
                "type": "task_status_changed",
                "payload": {"task_id": task_id, "status": "PLANNED", "message": "Generating execution plan…"},
            })

            _plan_channel = task.chat_channel_id or task_id

            def _on_plan_thinking(chunk: str) -> None:
                for ch in (task_id, _plan_channel):
                    self.broadcaster.broadcast(ch, {
                        "type": "planning_thinking_chunk",
                        "payload": {"chunk": chunk, "iteration": 0},
                    })

            plan_raw = await self._reasoning_engine.create_plan(
                task,
                str(shadow_workspace.shadow_path),
                plan_context_payload,
                on_thinking=_on_plan_thinking,
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
            self.broadcaster.broadcast(task_id, {"type": "done", "payload": {}})
            return task

    async def resume_task(self, task_id: str, *, broadcast_key: str | None = None) -> TaskRecord:
        """Resume execution of a child task that was created from a failed/aborted parent.

        The child task must already be in PLANNED state with shadow_workspace_path set
        (cloned from the parent by the route handler).  Skips plan generation entirely
        and calls _execute_plan() directly, relying on the existing completed_step_ids
        skip logic to continue from the first incomplete step.

        broadcast_key: if set, ToolLoop events are routed to this channel instead of
        the task channel (used by the chat resume path to stream events to the chat SSE).
        """
        task = await self._store.get(task_id)
        await self._env_ensurer.ensure(Path(task.workspace_path), channel_id=task.task_id)
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
                task, shadow_workspace, retrieval_context, retrieval_warnings, started_at_ms,
                broadcast_key=broadcast_key,
            )
        except Exception as exc:
            logger.error(f"Task {task_id} failed during resume", exc_info=True)
            task.diagnostics.append(Diagnostic(source="orchestrator", message=str(exc), level="error"))
            task = transition(task, TaskStatus.FAILED, "resume failed")
            await self._store.save(task)
            self._running_tasks.discard(task_id)
            self.broadcaster.broadcast(task_id, {"type": "done", "payload": {}})
            return task

    async def revalidate_task(self, task_id: str) -> TaskRecord:
        """Re-run full validation on a completed task's existing shadow — no step execution.

        For a child whose plan steps are all done, runs the command validator on the
        existing shadow and reports the true result, reusing the task's ORIGINAL baseline
        (baseline_error_fingerprints) so only errors introduced by the task's work fail.
        Does NOT run the repair loop — on failure the task goes FAILED with the diagnostics;
        the user can resume execute to fix.
        """
        task = await self._store.get(task_id)
        self._running_tasks.add(task_id)
        task.artifacts_root_path = str(self._artifacts_root(task.task_id, task.workspace_path))
        try:
            if not task.shadow_workspace_path or not Path(task.shadow_workspace_path).exists():
                task = transition(task, TaskStatus.FAILED, "revalidate: shadow workspace missing")
                await self._store.save(task)
                return task
            shadow_path = Path(task.shadow_workspace_path)
            baseline = frozenset(task.baseline_error_fingerprints)

            task = transition(task, TaskStatus.EXECUTING, "revalidation started")
            await self._store.save(task)
            task = transition(task, TaskStatus.VALIDATING, "full validation (revalidate)")
            await self._store.save(task)

            validation = await self._validator.run(str(shadow_path))
            validation = self._filter_baseline_errors(validation, baseline)
            self._write_debug_artifact(
                task.task_id,
                "full-validation",
                validation.model_dump(mode="json"),
                artifacts_root_path=task.artifacts_root_path,
            )
            if validation.success:
                task.diagnostics = []
                task = transition(task, TaskStatus.VALIDATED, "revalidation passed")
                await self._store.save(task)
                task = transition(
                    task, TaskStatus.READY_FOR_REVIEW, "revalidation passed; ready for review"
                )
                await self._store.save(task)
            else:
                task = await self._pause_for_validation_decision(task, validation)
            return task
        except Exception as exc:
            logger.error(f"Task {task_id} failed during revalidate", exc_info=True)
            task.diagnostics.append(Diagnostic(source="orchestrator", message=str(exc), level="error"))
            task = transition(task, TaskStatus.FAILED, "revalidate failed")
            await self._store.save(task)
            return task
        finally:
            self._running_tasks.discard(task_id)
            self.broadcaster.broadcast(task_id, {"type": "done", "payload": {}})

    async def _pause_for_validation_decision(
        self,
        task: TaskRecord,
        validation: ValidationResult,
        persistent_diagnostics: list[Diagnostic] | None = None,
    ) -> TaskRecord:
        """Gate on a terminal validation failure: surface the surviving diagnostics and let
        the user accept (treat as pre-existing/acceptable → VALIDATED → READY_FOR_REVIEW) or
        reject (→ FAILED). Mirrors the scope-decision gate; no repair is attempted here.

        Entered from VALIDATING. Resolved by POST /tasks/{id}/validation-decision.
        """
        persistent = list(persistent_diagnostics or [])
        task.diagnostics = [*persistent, *validation.diagnostics]
        future: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
        self._pending_validation_decisions[task.task_id] = future
        task = transition(task, TaskStatus.AWAITING_VALIDATION_DECISION, "validation decision gate")
        await self._store.save(task)
        self.broadcaster.broadcast(task.task_id, {
            "type": "validation_decision_requested",
            "payload": {
                "task_id": task.task_id,
                "diagnostics": [
                    {**d.model_dump(mode="json"), "message": self._cap_diagnostic_message(d.message)}
                    for d in validation.diagnostics
                ],
            },
        })
        accept = False
        try:
            if self._validation_decision_timeout_sec > 0:
                accept = await asyncio.wait_for(future, timeout=self._validation_decision_timeout_sec)
            else:
                accept = await future
        except asyncio.TimeoutError:
            accept = False
        finally:
            self._pending_validation_decisions.pop(task.task_id, None)
        task = await self._store.get(task.task_id)
        if accept:
            # TODO(pradeep): READY_FOR_REVIEW here is largely hollow — _partial_promote already
            # wrote each completed step's changes to the real workspace during execution, so the
            # final PROMOTE just re-copies the same files (nothing new to review/ship). Revisit:
            # accept → SUCCEEDED directly, and/or drop the redundant final promote when per-step
            # promotion already ran. Same critique applies to the normal pass path, not just this gate.
            task.diagnostics = [*persistent]
            task = transition(task, TaskStatus.VALIDATED, "validation errors accepted")
            await self._store.save(task)
            task = transition(task, TaskStatus.READY_FOR_REVIEW, "validation accepted; ready for review")
            await self._store.save(task)
        else:
            task = transition(task, TaskStatus.FAILED, "validation errors rejected")
            await self._store.save(task)
        return task

    async def get_task(self, task_id: str) -> TaskRecord:
        return await self._store.get(task_id)

    async def resume_from_execute(self, parent_task_id: str, *, chat_channel_id: str | None = None) -> str:
        """Create a child task resuming from execute stage and launch it. Returns child task_id.

        Equivalent to POST /tasks/{id}/resume with stage=execute, but callable directly
        from the chat agent without going through the HTTP layer.

        chat_channel_id: if set, ToolLoop events are streamed to this channel and
        chat_done is broadcast when execution completes.  The caller must NOT broadcast
        chat_done itself in this case — this method's background task owns that.
        """
        parent = await self._store.get(parent_task_id)
        # SSE: broadcast to the parent's task channel AND the chat channel (when
        # this resume is triggered from chat) so both UIs surface env_profile_*.
        await self._env_ensurer.ensure(
            Path(parent.workspace_path),
            channel_id=parent.task_id,
            chat_channel_id=chat_channel_id,
        )
        if parent.status not in {TaskStatus.FAILED, TaskStatus.ABORTED}:
            raise ValueError(f"Cannot resume task in {parent.status} state (must be FAILED or ABORTED)")
        if not parent.plan:
            raise ValueError("No executable plan to resume from")
        if not parent.shadow_workspace_path or not Path(parent.shadow_workspace_path).exists():
            raise ValueError("Parent shadow workspace no longer exists; cannot resume execute stage")

        child_id = f"task-{uuid4()}"
        now = datetime.now(timezone.utc)
        child = TaskRecord(
            task_id=child_id,
            goal=parent.goal,
            workspace_path=parent.workspace_path,
            mode=parent.mode,
            budget=TaskBudget(),  # fresh budget with current defaults
            status=TaskStatus.PLANNED,
            plan=parent.plan,
            plan_markdown=parent.plan_markdown,
            completed_step_ids=list(parent.completed_step_ids),
            modified_files=list(parent.modified_files),
            plan_approval_snapshot=parent.plan_approval_snapshot,
            resume_of_task_id=parent.task_id,
            created_at=now,
            updated_at=now,
        )
        await self._store.create(child)

        async def _run() -> None:
            shadow = await self._workspace_manager.clone(
                parent.task_id,
                child_id,
                parent.workspace_path,
                src_override=Path(parent.shadow_workspace_path) if parent.shadow_workspace_path else None,
            )
            child_record = await self._store.get(child_id)
            child_record.shadow_workspace_path = str(shadow.shadow_path)
            await self._store.save(child_record)
            if chat_channel_id:
                await self._resume_with_channel(child_id, chat_channel_id)
            else:
                await self.resume_task(child_id)

        asyncio.create_task(_run())
        return child_id

    async def _resume_with_channel(self, child_id: str, chat_channel_id: str) -> None:
        """Run resume_task streaming events to chat_channel_id, then close the chat SSE."""
        try:
            await self.resume_task(child_id, broadcast_key=chat_channel_id)
        finally:
            self.broadcaster.broadcast(chat_channel_id, {"type": "chat_done", "payload": {}})

    async def run_inline_change(
        self,
        *,
        thread_id: str,
        goal: str,
        workspace_path: str,
        plan_markdown: str,
        explore_context: list[dict[str, object]],
        likely_targets: list[str] | None = None,
        channel_id: str,
        store: object,
        thinking_log: list[str] | None = None,
    ) -> None:
        """Run a small inline change within a chat thread.

        Creates a lightweight shadow containing only the target files, runs a
        single-step ToolLoop with skip_verify=True routed to channel_id, then
        broadcasts diff_ready with the computed file diffs.  The shadow is
        retained until the user promotes or discards it.
        """
        from agentd.tools.loop import ToolLoop, VerifyResult, build_tool_registry

        inline_task_id = f"inline-{uuid4().hex[:12]}"
        real_path = Path(workspace_path)
        # W5: ensure the workspace env profile exists for inline changes too.
        # Routes SSE to the chat channel so the user sees env activity in chat.
        await self._env_ensurer.ensure(real_path, channel_id=channel_id)
        logger.info(
            "[inline] run_inline_change start: id=%s goal=%.80s workspace=%s explore_entries=%d",
            inline_task_id, goal, workspace_path, len(explore_context),
        )

        # Collect target files from the explore phase.
        # Priority: (1) read_file entries from explore, (2) likely_targets from
        # the intent classifier (relative paths), (3) empty (ToolLoop self-discovers).
        target_files: list[str] = []
        for entry in explore_context:
            tool = entry.get("tool", "")
            if tool == "read_file":
                args_val = entry.get("args")
                if isinstance(args_val, dict) and "path" in args_val:
                    candidate = str(args_val["path"])
                    # Strip workspace prefix to get a relative path
                    try:
                        rel = str(Path(candidate).relative_to(real_path))
                        candidate_path = real_path / rel
                    except ValueError:
                        rel = candidate
                        candidate_path = real_path / rel
                    if candidate_path.is_file() and rel not in target_files:
                        target_files.append(rel)

        if not target_files and likely_targets:
            for t in likely_targets:
                rel = t.lstrip("/")
                candidate_path = real_path / rel
                if candidate_path.is_file() and rel not in target_files:
                    target_files.append(rel)

        logger.info("[inline] target_files resolved: %s", target_files)
        _making_msg = "Making changes…"
        self.broadcaster.broadcast(channel_id, {
            "type": "chat_agent_thinking",
            "payload": {"message": _making_msg},
        })
        if thinking_log is not None:
            thinking_log.append(_making_msg)
        shadow = await self._workspace_manager.prepare_lightweight(
            inline_task_id, workspace_path, target_files
        )
        shadow_path = Path(shadow.shadow_path)
        logger.info("[inline] shadow prepared: %s", shadow_path)

        self._inline_shadows[inline_task_id] = {
            "shadow_path": str(shadow_path),
            "workspace_path": workspace_path,
            "touched_files": [],
        }
        # Persist to disk so promote/discard survive a reload.
        _manifest = shadow_path / ".inline-manifest.json"
        _manifest.write_text(json.dumps({
            "inline_task_id": inline_task_id,
            "workspace_path": workspace_path,
            "touched_files": [],
        }))

        # Build a single-step plan targeting all collected files
        plan_targets = [
            PlanTarget(path=f, intent=PlanTargetIntent.EXISTING)
            for f in target_files
        ]
        step = PlanStep(
            id="s1",
            goal=goal,
            targets=plan_targets,
            risk="low",
            testing_strategy="none — inline change, no verify",
        )

        # Seed the ToolLoop with the explore phase results as pre-populated history.
        # The model starts knowing what was already found and only fetches what it
        # genuinely still needs (e.g. a specific line range it hasn't seen yet).
        initial_history: list[dict[str, object]] = []
        for entry in explore_context:
            tool_name = entry.get("tool", "")
            args_val = entry.get("args") or {}
            result_text = str(entry.get("result", ""))
            is_error = bool(entry.get("is_error", False))
            if not tool_name or not result_text:
                continue
            initial_history.append({
                "role": "assistant",
                "content": json.dumps({
                    "type": "tool_call",
                    "thought": "Exploring workspace before making changes.",
                    "tool": tool_name,
                    "args": args_val,
                }),
            })
            initial_history.append({
                "role": "tool_result",
                "tool": tool_name,
                "content": result_text,
                "is_error": is_error,
            })

        registry = build_tool_registry(
            shadow_path,
            self._retrieval_client,
            real_workspace_path=real_path,
        )
        tool_loop = ToolLoop(
            self._reasoning_engine,
            registry,
            self.broadcaster,
            inline_task_id,
            self._patch_engine,
            shadow_path,
            broadcast_key=channel_id,
            skip_verify=True,
            thinking_log=thinking_log,
        )

        inline_budget = TaskBudget(max_tool_calls_per_step=32)
        patch_context: dict[str, object] = {
            "goal": goal,
            "workspace_path": workspace_path,
            "plan_markdown": plan_markdown,
        }

        logger.info(
            "[inline] starting ToolLoop: id=%s initial_history_entries=%d",
            inline_task_id, len(initial_history),
        )
        try:
            outcome = await tool_loop.run(
                step,
                patch_context,
                inline_budget,
                TaskUsage(),
                initial_history=initial_history or None,
            )
        except Exception as exc:
            logger.exception("run_inline_change tool loop failed", extra={"inline_task_id": inline_task_id})
            self.broadcaster.broadcast(channel_id, {
                "type": "patch_failed",
                "payload": {"step_id": "s1", "error": str(exc)},
            })
            self.broadcaster.broadcast(channel_id, {"type": "chat_done", "payload": {}})
            return

        touched_files: list[str] = []
        if isinstance(outcome, VerifyResult):
            touched_files = outcome.touched_files
            logger.info(
                "[inline] ToolLoop complete: id=%s verified=%s touched=%s",
                inline_task_id, outcome.verified, touched_files,
            )
        else:
            logger.warning(
                "[inline] ToolLoop returned PlanHandoff (unexpected for inline): id=%s reason=%.100s",
                inline_task_id, getattr(outcome, "reason", ""),
            )

        self._inline_shadows[inline_task_id]["touched_files"] = touched_files
        _manifest = shadow_path / ".inline-manifest.json"
        _manifest.write_text(json.dumps({
            "inline_task_id": inline_task_id,
            "workspace_path": workspace_path,
            "touched_files": touched_files,
        }))

        # Compute diff entries
        diff_entries = self._compute_diff_entries(
            real_path, shadow_path, touched_files, inline_task_id
        )

        logger.info(
            "[inline] diff computed: id=%s entries=%d",
            inline_task_id, len(diff_entries),
        )

        # Persist the diff card to the thread so it survives panel reloads.
        from agentd.chat.models import ChatMessage as _ChatMsg
        diff_entries_payload = [
            {"path": e.path, "additions": e.additions, "deletions": e.deletions, "temp_path": e.temp_path}
            for e in diff_entries
        ]
        store.append_message(  # type: ignore[union-attr]
            thread_id,
            _ChatMsg(
                role="agent",
                content=inline_task_id,
                type="diff_card",
                task_id=inline_task_id,
                metadata={
                    "diff_entries": diff_entries_payload,
                    "thinking_log": thinking_log or [],
                },
            ),
        )

        self.broadcaster.broadcast(channel_id, {
            "type": "diff_ready",
            "payload": {
                "task_id": inline_task_id,
                "diff_entries": [
                    {
                        "path": e.path,
                        "additions": e.additions,
                        "deletions": e.deletions,
                        "temp_path": e.temp_path,
                    }
                    for e in diff_entries
                ],
                "thinking_log": thinking_log or [],
                "completed_steps": 1,
                "total_steps": 1,
            },
        })
        self.broadcaster.broadcast(channel_id, {"type": "chat_done", "payload": {}})

    def _compute_diff_entries(
        self,
        real_path: Path,
        shadow_path: Path,
        touched_files: list[str],
        inline_task_id: str,
    ) -> list[DiffEntry]:
        entries: list[DiffEntry] = []
        for rel in touched_files:
            shadow_file = shadow_path / rel
            real_file = real_path / rel
            if not shadow_file.exists():
                continue
            shadow_lines = shadow_file.read_text(errors="replace").splitlines(keepends=True)
            real_lines = real_file.read_text(errors="replace").splitlines(keepends=True) if real_file.exists() else []
            diff = list(difflib.unified_diff(real_lines, shadow_lines, lineterm=""))
            additions = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
            deletions = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
            entries.append(DiffEntry(
                path=rel,
                additions=additions,
                deletions=deletions,
                temp_path=str(shadow_file),
            ))
        return entries

    def _load_inline_meta_from_disk(self, inline_task_id: str) -> dict | None:
        """Reconstruct inline shadow metadata from the on-disk manifest (survives reload)."""
        shadow_path = self._workspace_manager._resolve_shadow_path(inline_task_id)
        manifest = shadow_path / ".inline-manifest.json"
        if not manifest.exists():
            return None
        try:
            data = json.loads(manifest.read_text())
            return {
                "shadow_path": str(shadow_path),
                "workspace_path": data["workspace_path"],
                "touched_files": data.get("touched_files", []),
            }
        except Exception:
            return None

    async def promote_inline_change(self, inline_task_id: str) -> None:
        """Copy shadow files into the real workspace."""
        meta = self._inline_shadows.get(inline_task_id)
        if meta is None:
            meta = self._load_inline_meta_from_disk(inline_task_id)
        if meta is None:
            raise KeyError(f"Unknown inline change: {inline_task_id!r}")
        shadow_path = Path(str(meta["shadow_path"]))
        real_path = Path(str(meta["workspace_path"]))
        touched: list[str] = list(meta.get("touched_files", []))  # type: ignore[arg-type]
        for rel in touched:
            src = shadow_path / rel
            dst = real_path / rel
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        self._inline_shadows.pop(inline_task_id, None)

    async def discard_inline_change(self, inline_task_id: str) -> None:
        """Remove the inline change shadow without touching the real workspace."""
        meta = self._inline_shadows.pop(inline_task_id, None)
        if meta is None:
            meta = self._load_inline_meta_from_disk(inline_task_id)
        if meta:
            shadow_path = Path(str(meta["shadow_path"]))
            if shadow_path.exists():
                shutil.rmtree(shadow_path, ignore_errors=True)

    async def create_task_from_chat(
        self,
        *,
        thread_id: str,
        goal: str,
        workspace_path: str,
        explore_context: list[dict[str, object]],
        store: object,
    ) -> str:
        """Create a full planning task pre-seeded with chat explore context."""
        from agentd.domain.models import TaskCreateRequest
        logger.info(
            "[chat→task] create_task_from_chat: thread=%s goal=%.80s explore_entries=%d",
            thread_id, goal, len(explore_context),
        )
        request = TaskCreateRequest(
            goal=goal,
            workspace_path=workspace_path,
            mode="project_edit",
            initial_explore_context=explore_context,
        )
        # Honor AI_EDITOR_STEP_REVIEW_AUTO_ACCEPT the same way POST /v1/tasks does.
        # Pre-fix this code path silently defaulted to True (TaskRecord field default),
        # so the env knob had no effect on chat-created tasks and step diffs were
        # auto-accepted regardless. Now matches the API-created behaviour.
        import os
        _env_step_review_default = os.environ.get(
            "AI_EDITOR_STEP_REVIEW_AUTO_ACCEPT", "true",
        ).strip().lower() not in ("0", "false", "no", "off")
        task = TaskRecord(
            task_id=f"task-{uuid4().hex[:12]}",
            goal=request.goal,
            workspace_path=request.workspace_path,
            mode=request.mode,
            status=TaskStatus.QUEUED,
            initial_explore_context=explore_context,
            chat_channel_id=f"chat:{thread_id}",
            step_review_auto_accept=_env_step_review_default,
        )
        await self._store.create(task)
        asyncio.create_task(self.run_task(task.task_id))
        logger.info("[chat→task] task created and queued: task_id=%s", task.task_id)
        return task.task_id

    def _write_chat_plan_card(self, task: "TaskRecord") -> None:
        """Persist a revised plan card to the chat DB so it survives a reload."""
        if not task.chat_channel_id or self._chat_store is None or not task.plan_markdown:
            return
        from agentd.chat.models import ChatMessage
        thread_id = task.chat_channel_id[len("chat:"):]
        msg = ChatMessage(
            role="agent",
            content=task.plan_markdown,
            type="plan_card",
            task_id=task.task_id,
            metadata={"taskId": task.task_id, "plan_markdown": task.plan_markdown},
        )
        self._chat_store.append_message(thread_id, msg)  # type: ignore[union-attr]

    def _write_chat_completion(self, task: "TaskRecord") -> None:
        """Write a completion card to the chat DB for tasks originating from chat."""
        if not task.chat_channel_id or self._chat_store is None:
            return
        from agentd.chat.models import ChatMessage
        thread_id = task.chat_channel_id[len("chat:"):]
        if task.status == TaskStatus.READY_FOR_REVIEW:
            content = "Execution complete — review the diff in the Tasks panel."
        elif task.status == TaskStatus.SUCCEEDED:
            content = "Task completed successfully."
        else:
            diag = task.diagnostics[-1].message if task.diagnostics else "Unknown error"
            content = f"Execution failed: {diag}"
        msg = ChatMessage(role="agent", content=content, type="text",
                          task_id=task.task_id, metadata={"task_id": task.task_id})
        self._chat_store.append_message(thread_id, msg)  # type: ignore[union-attr]

    async def await_plan_ready(self, task_id: str, timeout_sec: float = 3600.0) -> "TaskRecord | None":
        """Poll until task reaches AWAITING_PLAN_APPROVAL or a terminal state."""
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            task = await self._store.get(task_id)
            if task.status == TaskStatus.AWAITING_PLAN_APPROVAL:
                return task
            if task.status in (TaskStatus.FAILED, TaskStatus.ABORTED, TaskStatus.SUCCEEDED):
                return task
            await asyncio.sleep(2.0)
        return None

    async def _execute_plan(
        self,
        task: TaskRecord,
        shadow_workspace: ShadowWorkspace,
        retrieval_context: RetrievalContext,
        persistent_diagnostics: list[Diagnostic],
        started_at_ms: int,
        *,
        broadcast_key: str | None = None,
    ) -> TaskRecord:
        try:
            shadow_path = Path(shadow_workspace.shadow_path)
            real_path = shadow_workspace.real_path
            if task.plan is None:
                task = transition(task, TaskStatus.FAILED, "plan missing")
                await self._store.save(task)
                return task

            task = transition(task, TaskStatus.EXECUTING, "execution started")
            await self._store.save(task)
            self.broadcaster.broadcast(task.task_id, {
                "type": "task_status_changed",
                "payload": {"task_id": task.task_id, "status": "EXECUTING", "message": "Executing plan…"},
            })

            baseline_errors = await self._collect_baseline_errors(
                shadow_path,
                task_id=task.task_id,
                artifacts_root_path=task.artifacts_root_path,
            )
            # Persist the ORIGINAL baseline so a later `validate`-stage resume can reuse it
            # rather than re-collecting on the already-mutated shadow (which would mask
            # every error by capturing it as baseline).
            task.baseline_error_fingerprints = sorted(baseline_errors)
            if baseline_errors:
                logger.info(
                    "Baseline validation captured pre-existing errors",
                    extra={"task_id": task.task_id, "baseline_error_count": len(baseline_errors)},
                )

            # Per-step run counter: incremented each time a step is (re)attempted after a delta
            # replan, so artifact attempt numbers don't collide across runs of the same step.
            step_attempt_offsets: dict[str, int] = {}
            # Accumulates applied patch ops per file across completed steps so that later
            # steps can see what was changed in prior-step files without needing cat.
            completed_ops_by_file: dict[str, list[dict[str, object]]] = {}

            while (step := self._next_incomplete_step(task)) is not None:
                attempt_offset = step_attempt_offsets.get(step.id, 0)
                step_result = await self._run_step_with_retries(
                    task,
                    step,
                    shadow_path,
                    retrieval_context,
                    persistent_diagnostics,
                    started_at_ms,
                    attempt_offset=attempt_offset,
                    broadcast_key=broadcast_key,
                    prior_step_patches=completed_ops_by_file,
                    static_baseline=baseline_errors,
                )

                if isinstance(step_result, PlanHandoff):
                    request = DeltaReplanRequest(
                        requested_by_step_id=step_result.step_id,
                        reason=step_result.reason,
                        evidence=step_result.evidence,
                        hinted_affected_steps=step_result.hinted_affected_steps,
                        requested_at=datetime.now(timezone.utc),
                    )
                    task.execution_state.delta_replan_requests.append(request)

                    if task.execution_state.delta_replans_used >= task.budget.max_delta_replans:
                        task.diagnostics.append(Diagnostic(
                            source="orchestrator",
                            message=(
                                f"Delta replan budget exhausted "
                                f"({task.budget.max_delta_replans} max). "
                                f"Last request from step {step_result.step_id}: {step_result.reason}"
                            ),
                            level="error",
                        ))
                        task = transition(task, TaskStatus.FAILED, "delta replan budget exhausted")
                        await self._store.save(task)
                        return task

                    task.execution_state.delta_replans_used += 1
                    print(
                        f"\n[DELTA-REPLAN] Step {step_result.step_id} requested revision "
                        f"({task.execution_state.delta_replans_used}/{task.budget.max_delta_replans})"
                        f"\n[DELTA-REPLAN] Reason: {step_result.reason[:200]}"
                    )
                    logger.info(
                        "Delta replan triggered",
                        extra={
                            "task_id": task.task_id,
                            "step_id": step_result.step_id,
                            "reason": step_result.reason,
                            "replans_used": task.execution_state.delta_replans_used,
                        },
                    )

                    planning_agent = self._build_planning_agent(task.task_id, task.workspace_path)
                    revision = await planning_agent.revise(task, real_path)

                    rev_n = task.execution_state.delta_replans_used
                    self._write_debug_artifact(
                        task.task_id,
                        f"delta-replan-{rev_n}-revision",
                        {
                            "revision_summary": revision.revision_summary,
                            "revised_steps": [r.model_dump(mode="json") for r in revision.revised_steps],
                            "reverted_step_ids": revision.reverted_step_ids,
                            "tool_trace": revision.tool_trace.model_dump(mode="json"),
                        },
                        artifacts_root_path=task.artifacts_root_path,
                    )

                    self._apply_revision(task, shadow_path, revision)

                    reverted = revision.reverted_step_ids
                    revised_ids = [r.step_id for r in revision.revised_steps]
                    new_plan_summary = ", ".join(
                        f"{s.id}:[{','.join(t.path for t in s.targets)}]"
                        for s in task.plan.steps
                    )
                    print(
                        f"[DELTA-REPLAN] Reverted: {reverted}  |  Revised: {revised_ids}"
                        f"\n[DELTA-REPLAN] New plan: {new_plan_summary}"
                    )
                    logger.info(
                        "Delta replan applied",
                        extra={
                            "task_id": task.task_id,
                            "reverted": reverted,
                            "revised": revised_ids,
                            "new_plan_steps": [s.id for s in task.plan.steps],
                        },
                    )

                    # Bump offsets for every reverted step so re-runs write to new attempt dirs
                    for sid in reverted:
                        step_attempt_offsets[sid] = (
                            step_attempt_offsets.get(sid, 0) + self._max_attempts_per_step
                        )

                    await self._store.save(task)
                    continue

                self._merge_step_result(task, step_result, persistent_diagnostics)
                for _fpath, _ops in step_result.patch_ops_by_file.items():
                    completed_ops_by_file.setdefault(_fpath, []).extend(_ops)
                await self._store.save(task)
                if step_result.outcome != "step_completed":
                    task = transition(task, TaskStatus.FAILED, "step execution exhausted")
                    await self._store.save(task)
                    return task

                # Per-step review gate: pause and show diff to user before continuing.
                if not task.step_review_auto_accept:
                    decision = await self._pause_for_step_review(
                        task, step, step_result, shadow_path, real_path,
                    )
                    if decision == "discard":
                        await self._unmerge_step_result(
                            task, step, step_result, shadow_path, real_path,
                            completed_ops_by_file,
                        )
                        continue
                # Always partial-promote so subsequent steps' EXPLORE reads see prior
                # accepted changes. Review (if enabled above) runs BEFORE; promote is
                # required regardless of whether review was shown.
                await self._partial_promote(shadow_path, real_path, step_result.touched_files)

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
                    "excerpt": "\n".join(
                        self._cap_diagnostic_message(d.message)
                        for d in validation.diagnostics[:10]
                    ),
                },
                broadcast_key=broadcast_key,
                prior_step_patches=completed_ops_by_file,
                static_baseline=baseline_errors,
            )
            # Repair cannot delta-replan (the plan is already done by this point);
            # if the agent kept hitting scope violations, fail out cleanly with the
            # PlanHandoff's evidence rather than crashing in _merge_step_result.
            if isinstance(repair_result, PlanHandoff):
                task.diagnostics.append(Diagnostic(
                    source="orchestrator",
                    message=(
                        f"Repair step needs files outside its scope: {repair_result.reason}. "
                        f"Evidence: {repair_result.evidence}"
                    ),
                    level="error",
                ))
                task = transition(task, TaskStatus.FAILED, "repair scope violation")
                await self._store.save(task)
                return task
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
                # Auto-repair couldn't clear it — ask the user (errors may be pre-existing/flaky)
                # instead of failing outright.
                return await self._pause_for_validation_decision(
                    task, repair_validation, persistent_diagnostics
                )

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
            self.broadcaster.broadcast(task.task_id, {"type": "done", "payload": {"status": task.status.value}})
            if task.chat_channel_id and self._chat_store is not None:
                self._write_chat_completion(task)
            if task.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.ABORTED}:
                try:
                    await self._workspace_manager.prune_checkpoints()
                except Exception:
                    logger.exception(
                        "Checkpoint pruning failed",
                        extra={"task_id": task.task_id},
                    )

    def _build_scope_callback(
        self,
        task_id: str,
        step_id: str,
        step: PlanStep,
    ) -> ScopeExtensionCallback:
        """Return a scope-extension callback bound to this task + the orchestrator's policy."""

        async def _cb(files: list[str], reason: str) -> ScopeDecision:
            # Filter against already-approved files from earlier in this task.
            try:
                task = await self._store.get(task_id)
            except KeyError:
                return ScopeDecision(approve=False, extended_files=[], reason="task missing")
            already_approved = set(task.execution_state.auto_approved_scope_files)
            truly_new = [f for f in files if f not in already_approved]
            if not truly_new:
                return ScopeDecision(approve=True, extended_files=files)

            # Trigger filter: with `nearby`, gate as long as AT LEAST ONE requested
            # file is plausibly in-scope (same dir as a target, conventional pattern,
            # or under a directory-shaped target). The user sees the whole batch and
            # decides — partial-nearby batches are still worth surfacing.
            if self._scope_trigger == ScopeTrigger.NEARBY:
                target_paths = [t.path for t in step.targets]
                if not any(_is_nearby_file(f, target_paths) for f in truly_new):
                    return ScopeDecision(
                        approve=False, extended_files=[], reason="no nearby files in batch",
                    )

            if self._scope_policy == ScopePolicy.STRICT:
                return ScopeDecision(approve=False, extended_files=[], reason="strict policy")

            if self._scope_policy == ScopePolicy.AUTO:
                self._write_debug_artifact(
                    task_id, "scope-auto-approve",
                    {"step_id": step_id, "files": truly_new, "reason": reason},
                    artifacts_root_path=task.artifacts_root_path,
                )
                return ScopeDecision(approve=True, extended_files=truly_new)

            # ASK policy — pause + future + broadcast event.
            decision_id = uuid4().hex
            future: asyncio.Future[ScopeDecision] = asyncio.get_event_loop().create_future()
            self._pending_scope_decisions[task_id] = future

            task.execution_state.pending_scope_request = ScopeExtensionRequest(
                decision_id=decision_id, files=truly_new, reason=reason, step_id=step_id,
            )
            try:
                task = transition(task, TaskStatus.AWAITING_SCOPE_DECISION, "scope gate")
            except ValueError:
                # Already in AWAITING_SCOPE_DECISION (re-entrant edge); leave status alone.
                pass
            await self._store.save(task)
            self.broadcaster.broadcast(task_id, {
                "type": "scope_extension_requested",
                "payload": {"decision_id": decision_id, "files": truly_new, "reason": reason, "step_id": step_id},
            })

            decision = ScopeDecision(approve=False, extended_files=[], reason="cancelled")
            try:
                if self._scope_timeout_sec > 0:
                    decision = await asyncio.wait_for(future, timeout=self._scope_timeout_sec)
                else:
                    decision = await future
            except asyncio.TimeoutError:
                decision = ScopeDecision(approve=False, extended_files=[], reason="timeout")
            finally:
                self._pending_scope_decisions.pop(task_id, None)
                # Resume EXECUTING regardless of outcome.
                task = await self._store.get(task_id)
                task.execution_state.pending_scope_request = None
                if task.status == TaskStatus.AWAITING_SCOPE_DECISION:
                    task = transition(task, TaskStatus.EXECUTING, "scope decision received")
                if (
                    decision.approve
                    and decision.remember
                    and self._scope_remember == ScopeRemember.TASK
                ):
                    for path in decision.extended_files:
                        if path not in task.execution_state.auto_approved_scope_files:
                            task.execution_state.auto_approved_scope_files.append(path)
                await self._store.save(task)

            return decision

        return _cb

    def _build_command_approval_callback(self, task_id: str):
        """Mirror of _build_scope_callback for run_command gating.

        Returns an async callback (command, args, cwd) -> CommandDecision that:
        - approves silently when policy is ALLOW_ALL,
        - approves silently when the command matches a per-task or per-workspace
          remembered rule (token-aware match),
        - otherwise pauses the task at AWAITING_COMMAND_DECISION, broadcasts a
          `command_approval_requested` SSE event, awaits POST /command-decision,
          and (if approve+remember) persists the user-chosen rule to both the
          per-task set and the per-workspace CommandRuleStore.
        """
        from uuid import uuid4

        from agentd.tools.command_rules import CommandRuleStore

        async def _cb(command: str, args: list[str], cwd: str) -> CommandDecision:
            task = await self._store.get(task_id)

            policy = task.shell_policy or self._shell_policy
            if policy == ShellPolicy.ALLOW_ALL:
                return CommandDecision(approve=True)

            # Per-task + per-workspace remembered approvals (token-aware match
            # via the same CommandRuleStore.rule_matches used for the JSON store).
            cmd_tokens = [command, *args]
            for rule in task.execution_state.approved_commands:
                if CommandRuleStore.rule_matches(rule, cmd_tokens):
                    return CommandDecision(approve=True)
            if CommandRuleStore(task.workspace_path).matches(command, args):
                return CommandDecision(approve=True)

            # ASK — pause + future + broadcast.
            decision_id = uuid4().hex
            step_id = task.execution_state.current_step_id or ""
            future: asyncio.Future[CommandDecision] = (
                asyncio.get_event_loop().create_future()
            )
            self._pending_command_decisions[task_id] = future
            task.execution_state.pending_command_request = CommandApprovalRequest(
                decision_id=decision_id,
                command=command,
                args=args,
                cwd=cwd,
                step_id=step_id,
            )
            try:
                task = transition(task, TaskStatus.AWAITING_COMMAND_DECISION, "command gate")
            except ValueError:
                # Already in AWAITING_COMMAND_DECISION (re-entrant edge); leave status alone.
                pass
            await self._store.save(task)
            self.broadcaster.broadcast(task_id, {
                "type": "command_approval_requested",
                "payload": {
                    "decision_id": decision_id,
                    "command": command,
                    "args": args,
                    "cwd": cwd,
                    "step_id": step_id,
                },
            })

            decision = CommandDecision(approve=False)
            try:
                if self._command_decision_timeout_sec > 0:
                    decision = await asyncio.wait_for(
                        future, timeout=self._command_decision_timeout_sec,
                    )
                else:
                    decision = await future
            except asyncio.TimeoutError:
                decision = CommandDecision(approve=False)
            finally:
                self._pending_command_decisions.pop(task_id, None)
                task = await self._store.get(task_id)
                task.execution_state.pending_command_request = None
                if task.status == TaskStatus.AWAITING_COMMAND_DECISION:
                    task = transition(task, TaskStatus.EXECUTING, "command decision received")
                if decision.approve and decision.remember:
                    import shlex
                    if decision.rule_value:
                        value = decision.rule_value
                    elif decision.scope == "binary":
                        value = command.rsplit("/", 1)[-1]
                    elif decision.scope == "exact":
                        value = shlex.join([command, *args])
                    else:  # prefix with no explicit value → lock command + first arg
                        _toks = [command, *args]
                        value = shlex.join(_toks[:2] if len(_toks) > 1 else _toks)
                    rule = CommandRule(
                        type=decision.scope,
                        value=value,
                        added_at=datetime.now(timezone.utc).isoformat(),
                    )
                    task.execution_state.approved_commands.append(rule)
                    CommandRuleStore(task.workspace_path).add(rule)
                await self._store.save(task)

            return decision

        return _cb

    async def _pause_for_step_review(
        self,
        task: TaskRecord,
        step: PlanStep,
        step_result: StepRunResult,
        shadow_path: Path,
        real_path: Path,
    ) -> str:
        """Pause after a step completes, broadcast a diff, and wait for accept/discard."""
        diff_entries = self._compute_diff_entries(
            real_path, shadow_path, step_result.touched_files, task.task_id,
        )
        serialized = [dataclasses.asdict(e) for e in diff_entries]
        payload = StepReviewPayload(
            step_id=step.id,
            step_title=step.goal[:120],
            diff_entries=serialized,
        )
        task.execution_state.pending_step_review = payload
        task = transition(task, TaskStatus.AWAITING_STEP_REVIEW, "step review gate")
        await self._store.save(task)
        self.broadcaster.broadcast(task.task_id, {
            "type": "step_review_requested",
            "payload": {
                "step_id": step.id,
                "step_title": payload.step_title,
                "diff_entries": serialized,
            },
        })

        future: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        self._pending_step_decisions[task.task_id] = future
        decision = "discard"
        try:
            decision = await future
        finally:
            self._pending_step_decisions.pop(task.task_id, None)
            task = await self._store.get(task.task_id)
            task.execution_state.pending_step_review = None
            if task.status == TaskStatus.AWAITING_STEP_REVIEW:
                task = transition(task, TaskStatus.EXECUTING, "step decision received")
            await self._store.save(task)

        return decision

    async def _partial_promote(
        self,
        shadow_path: Path,
        real_path: Path,
        touched_files: list[str],
    ) -> None:
        """Copy accepted step's files from shadow → real workspace so subsequent steps read them."""
        for rel in touched_files:
            src = shadow_path / rel
            dst = real_path / rel
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

    async def _unmerge_step_result(
        self,
        task: TaskRecord,
        step: PlanStep,
        step_result: StepRunResult,
        shadow_path: Path,
        real_path: Path,
        completed_ops_by_file: dict[str, list[dict[str, object]]],
    ) -> None:
        """Revert a discarded step: restore shadow files and unset completed state."""
        # Revert shadow files back to real workspace state
        for rel in step_result.touched_files:
            real_file = real_path / rel
            shadow_file = shadow_path / rel
            if real_file.exists():
                shutil.copy2(real_file, shadow_file)
            elif shadow_file.exists():
                shadow_file.unlink()

        # Remove step from completed list
        if step.id in task.completed_step_ids:
            task.completed_step_ids.remove(step.id)

        # Remove step's file ops from the accumulator
        for fpath in step_result.touched_files:
            completed_ops_by_file.pop(fpath, None)

        # Remove step's touched files from task.modified_files (only if no earlier step touched them)
        remaining = set(completed_ops_by_file.keys())
        task.modified_files = sorted(f for f in task.modified_files if f in remaining)

        await self._store.save(task)

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

    def _apply_revision(
        self,
        task: TaskRecord,
        shadow_path: Path,
        revision: PlanRevisionResult,
    ) -> None:
        """Apply a PlanRevisionResult to task state and the shadow workspace.

        Steps:
        1. Restore shadow to the pre-earliest-reverted-step checkpoint.
        2. Remove reverted step IDs from completed_step_ids.
        3. Update task.plan.steps with revised/new step definitions.
        """
        revert_ids = set(revision.reverted_step_ids)

        if revert_ids and task.plan is not None:
            # Find the earliest reverted step in plan order and restore its checkpoint
            for step in task.plan.steps:
                if step.id in revert_ids:
                    checkpoint_path = task.execution_state.step_checkpoints.get(step.id)
                    if checkpoint_path:
                        self._restore_shadow_checkpoint(shadow_path, checkpoint_path)
                        logger.info(
                            "Delta replan: shadow restored to pre-step checkpoint",
                            extra={"task_id": task.task_id, "reverted_to_step": step.id},
                        )
                    break

            # Remove reverted IDs from completed steps and their checkpoints
            task.completed_step_ids = [s for s in task.completed_step_ids if s not in revert_ids]
            for step_id in revert_ids:
                task.execution_state.step_checkpoints.pop(step_id, None)

        if task.plan is None:
            return

        # Apply revised step definitions
        revised_by_id = {r.step_id: r for r in revision.revised_steps}
        existing_ids = {s.id for s in task.plan.steps}
        # All original steps being reverted — used for test_command fallback so
        # that merging multiple steps into one doesn't silently drop test coverage.
        reverted_step_objects = [s for s in task.plan.steps if s.id in revert_ids]
        new_steps: list[PlanStep] = []

        for step in task.plan.steps:
            if step.id in revert_ids and step.id not in revised_by_id:
                continue  # reverted without replacement — drop it
            revised = revised_by_id.get(step.id)
            if revised is not None:
                new_steps.append(PlanStep.model_validate({
                    "id": step.id,
                    "goal": revised.goal,
                    "targets": revised.targets,
                    "risk": revised.risk,
                    "implementation_details": revised.implementation_details,
                    "edge_cases": revised.edge_cases or None,
                    "testing_strategy": revised.testing_strategy or None,
                    "test_command": _resolve_test_command(revised, reverted_step_objects),
                }))
            else:
                new_steps.append(step)

        # Append entirely new steps (not present in the original plan)
        for revised in revision.revised_steps:
            if revised.step_id not in existing_ids:
                new_steps.append(PlanStep.model_validate({
                    "id": revised.step_id,
                    "goal": revised.goal,
                    "targets": revised.targets,
                    "risk": revised.risk,
                    "implementation_details": revised.implementation_details,
                    "edge_cases": revised.edge_cases or None,
                    "testing_strategy": revised.testing_strategy or None,
                    "test_command": _resolve_test_command(revised, reverted_step_objects),
                }))

        task.plan = PlanDocument(
            analysis=task.plan.analysis,
            steps=new_steps,
            expected_files=task.plan.expected_files,
            stop_conditions=task.plan.stop_conditions,
        )

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
        attempt_offset: int = 0,
        broadcast_key: str | None = None,
        prior_step_patches: dict[str, list[dict[str, object]]] | None = None,
        static_baseline: frozenset[str] | None = None,
    ) -> "StepRunResult | PlanHandoff":
        allowed_files = sorted(set(step.target_paths()))
        if not allowed_files:
            allowed_files = [*task.modified_files] or [*task.plan.expected_files]
        max_files = max(1, min(task.budget.max_files_touched, len(allowed_files)))
        max_ops = max(1, min(12, max_files * 3))
        allowed_files_set = set(allowed_files)

        # Preflight gate for test_command: null it out if the referenced test file doesn't
        # exist in the shadow workspace and isn't a new target being created in this step.
        effective_test_command: str | None = step.test_command
        if effective_test_command:
            test_path = self._extract_path_from_test_command(effective_test_command)
            if test_path is not None:
                step_target_paths = {t.path for t in step.targets}
                if test_path not in step_target_paths and not (shadow_path / test_path).exists():
                    logger.warning(
                        "test_command references non-existent path not in step targets; skipping",
                        extra={"task_id": task.task_id, "step_id": step.id, "test_path": test_path},
                    )
                    print(f"[WARN] test_command path '{test_path}' not found in shadow; skipping for step {step.id}")
                    effective_test_command = None

        trace_entries: list[StepExecutionTrace] = []
        checkpoints: list[CheckpointManifest] = []
        last_result_diagnostics: list[Diagnostic] = [*persistent_diagnostics]
        last_selected_candidate_id: str | None = None
        touched_files_result: list[str] = []

        for attempt in range(1, self._max_attempts_per_step + 1):
            effective_attempt = attempt_offset + attempt
            print(
                f"\n[STEP] Running step {step.id} "
                f"(Attempt {effective_attempt}, local {attempt}/{self._max_attempts_per_step})"
            )
            print(f"[GOAL] {step.goal}")

            logger.info(
                "Step attempt started",
                extra={
                    "task_id": task.task_id,
                    "step_id": step.id,
                    "attempt": effective_attempt,
                    "max_attempts_per_step": self._max_attempts_per_step,
                },
            )
            assert_budget(task, started_at_ms, int(time.time() * 1000))
            task = bump_usage(task)

            checkpoint = self._create_shadow_checkpoint(
                task,
                step,
                effective_attempt,
                shadow_path,
                tracked_files=allowed_files,
            )
            if attempt == 1:
                # Record pre-step shadow state for potential delta replan revert
                task.execution_state.step_checkpoints[step.id] = checkpoint.checkpoint_path
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
                        Path(task.workspace_path), context_files, step.goal, retrieval_context
                    ),
                    planner_evidence=retrieval_context.planner_evidence,
                )
                retrieval_payload = patch_retrieval_context.as_prompt_payload()

                _completed_ids = set(task.completed_step_ids)
                _step_progress = [
                    {
                        "id": s.id,
                        "goal": s.goal,
                        "status": (
                            "completed" if s.id in _completed_ids
                            else "current" if s.id == step.id
                            else "pending"
                        ),
                    }
                    for s in (task.plan.steps if task.plan else [])
                ]
                patch_request_context = {
                    "current_step": step.model_dump(mode="json"),
                    "overall_goal": task.goal,
                    "step_progress": _step_progress,
                    "allowed_files": allowed_files,
                    "max_ops": max_ops,
                    "max_files": max_files,
                    "last_failure": last_failure,
                    "diagnostics": [
                        {
                            **item.model_dump(mode="json"),
                            "message": self._cap_diagnostic_message(item.message),
                        }
                        for item in task.diagnostics
                    ],
                    "retrieval_context": retrieval_payload,
                    "prior_step_files": list(task.modified_files),
                    "prior_step_patches": prior_step_patches or {},
                }
                self._write_debug_artifact(
                    task.task_id,
                    "patch-context",
                    patch_request_context,
                    step_id=step.id,
                    attempt=effective_attempt,
                    artifacts_root_path=task.artifacts_root_path,
                )

                if self._tool_loop_enabled:
                    print("\n[PATCH] Entering Tool-Use Loop (ReAct)...")
                    from agentd.tools.loop import ToolLoop, VerifyResult, build_tool_registry
                    registry = build_tool_registry(
                        shadow_path,
                        self._retrieval_client,
                        real_workspace_path=Path(task.workspace_path),
                        command_approval_callback=self._build_command_approval_callback(task.task_id),
                    )
                    tool_loop = ToolLoop(
                        self._reasoning_engine,
                        registry,
                        self.broadcaster,
                        broadcast_key or task.task_id,
                        self._patch_engine,
                        shadow_path,
                        scope_extension_callback=self._build_scope_callback(
                            task.task_id, step.id, step,
                        ),
                        static_baseline=static_baseline,
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

                    assert isinstance(step_outcome, VerifyResult)

                    if not step_outcome.verified:
                        # Verify phase failed — restore shadow and retry with test output as context
                        self._restore_shadow_checkpoint(shadow_path, checkpoint.checkpoint_path)
                        task.modified_files = previous_modified_files
                        last_failure = {
                            "failure_code": "verify_failed",
                            "excerpt": step_outcome.test_output[:2000] if step_outcome.test_output else "Verify phase failed",
                        }
                        self._write_debug_artifact(
                            task.task_id, "tool-trace",
                            step_outcome.tool_trace.model_dump(mode="json"),
                            step_id=step.id, attempt=effective_attempt,
                            artifacts_root_path=task.artifacts_root_path,
                        )
                        trace_entries.append(StepExecutionTrace(
                            step_id=step.id, attempt=effective_attempt, status="validation_failed",
                            checkpoint_id=checkpoint.checkpoint_id,
                            message=f"verify phase failed: {step_outcome.test_output[:200]}",
                        ))
                        last_result_diagnostics = [
                            *persistent_diagnostics,
                            Diagnostic(source="verify_phase", message=step_outcome.test_output[:500], level="error"),
                        ]
                        checkpoints.append(checkpoint)
                        continue

                    # VerifyResult(verified=True) — patch already applied inline, proceed directly
                    touched = step_outcome.touched_files
                    tool_trace = step_outcome.tool_trace
                    self._write_debug_artifact(
                        task.task_id, "tool-trace",
                        tool_trace.model_dump(mode="json"),
                        step_id=step.id, attempt=effective_attempt,
                        artifacts_root_path=task.artifacts_root_path,
                    )
                    self._write_debug_artifact(
                        task.task_id, "patch",
                        step_outcome.patch_document,
                        step_id=step.id, attempt=effective_attempt,
                        artifacts_root_path=task.artifacts_root_path,
                    )
                    print(
                        f"[PATCH] Tool loop complete"
                        f" ({len(tool_trace.calls)} tool calls, verified=True,"
                        f" touched={touched})"
                    )
                    task.modified_files = sorted(set(task.modified_files) | set(touched))
                    trace_entries.append(StepExecutionTrace(
                        step_id=step.id, attempt=effective_attempt, status="step_completed",
                        checkpoint_id=checkpoint.checkpoint_id,
                        message=f"verify passed: {step_outcome.test_output[:200] if step_outcome.test_output else 'no test output'}",
                    ))
                    # Extract ops by file so callers can pass them as prior_step_patches
                    _ops_by_file: dict[str, list[dict[str, object]]] = {}
                    _candidates = step_outcome.patch_document.get("candidates") or []
                    if isinstance(_candidates, list) and _candidates:
                        for _op in (_candidates[0].get("patch_ops") or []):
                            if isinstance(_op, dict) and "file" in _op:
                                _f = str(_op["file"])
                                _ops_by_file.setdefault(_f, []).append(_op)
                    return StepRunResult(
                        step_id=step.id,
                        outcome="step_completed",
                        validation_result="validation_passed",
                        attempts_used=attempt,
                        selected_candidate_id=None,
                        touched_files=touched,
                        patch_ops_by_file=_ops_by_file,
                        diagnostics=[*persistent_diagnostics],
                        trace_entries=trace_entries,
                        checkpoint_manifests=checkpoints,
                        last_failure=None,
                    )
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
                    attempt=effective_attempt,
                    artifacts_root_path=task.artifacts_root_path,
                )
                patch_document = PatchDocumentV2.model_validate(patch_raw)
                task.latest_patch_v2 = patch_document
                task.latest_patch = None

                print(f"[PATCH] Evaluating {len(patch_document.candidates)} candidates...")
                evaluations, ranking_path = await self._evaluate_candidates(
                    task=task,
                    step=step,
                    attempt=effective_attempt,
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
                            attempt=effective_attempt,
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
                            attempt=effective_attempt,
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
                        attempt=effective_attempt,
                        status="patch_applied",
                        candidate_id=selected.candidate.candidate_id,
                        checkpoint_id=checkpoint.checkpoint_id,
                        score=selected.score,
                        preflight_summary={"success": True},
                        message="selected candidate applied",
                    )
                )
                
                print(f"[VALIDATE] Running fast validation on {len(touched)} files...")
                if effective_test_command:
                    print(f"[VALIDATE] Running test command in parallel: {effective_test_command}")
                    fast_v, test_v = await asyncio.gather(
                        self._run_fast_validation(str(shadow_path), touched),
                        self._run_step_test_command(shadow_path, effective_test_command, task.workspace_path),
                    )
                    validation = _merge_validation_results(fast_v, test_v)
                else:
                    validation = await self._run_fast_validation(str(shadow_path), touched)
                validation_path = self._write_debug_artifact(
                    task.task_id,
                    "validation-selected",
                    validation.model_dump(mode="json"),
                    step_id=step.id,
                    attempt=effective_attempt,
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
                            attempt=effective_attempt,
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
                    "excerpt": "\n".join(
                        self._cap_diagnostic_message(d.message)
                        for d in validation.diagnostics[:10]
                    ),
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
                        attempt=effective_attempt,
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
                        attempt=effective_attempt,
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

        Returns the set of pre-existing diagnostic fingerprints (errors AND
        warnings) so they can be filtered from post-patch validation results. A
        pre-existing warning bloats the repair context as badly as an error — a
        single ruff/mypy blob can be tens of thousands of tokens — so both levels
        must be captured here. Failures here are non-fatal — if the baseline run
        itself errors we just return an empty set and proceed normally.
        """
        try:
            result = await self._validator.run(str(shadow_path))
            errors = frozenset(
                self._normalize_error_message(d.message)
                for d in result.diagnostics
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
            return "pytest:FAILED:"
        # Detect cargo test output — extract failed test names from the "failures:" block.
        # Shadow paths and timing in cargo output make raw comparison unstable.
        if "test result: FAILED" in msg or ("failures:" in msg and "test result:" in msg):
            failed_tests = re.findall(r"^    (\S+)\s*$", msg, re.MULTILINE)
            return "cargo:FAILED:" + ",".join(sorted(failed_tests))
        # Strip pytest/cargo timing: "N error(s) in X.XXs" at end of output
        msg = re.sub(r"\d+ errors? in \d+\.\d+s\s*$", "", msg, flags=re.MULTILINE).rstrip()
        # Strip compiler/linter line:col numbers so shifted lines still match
        msg = re.sub(r"(?m)(:\d+){1,2}(?=:|\s|$)", "", msg)
        # Strip absolute paths that embed shadow task UUIDs
        msg = re.sub(r"/[^\s]*/\.agentd/shadows/[^\s/]+", "<shadow>", msg)
        return msg

    def _filter_baseline_errors(
        self, result: ValidationResult, baseline: frozenset[str]
    ) -> ValidationResult:
        """Remove diagnostics (errors or warnings) already present before patching.

        Filtering is level-agnostic: any pre-existing diagnostic is noise once we
        only care about what the patch changed. success still keys on error-level
        diagnostics only, so removing pre-existing warnings never flips a passing
        run to failing — it just keeps stale noise out of task.diagnostics and the
        repair context.
        """
        if not baseline:
            return result
        filtered = [
            d for d in result.diagnostics
            if self._normalize_error_message(d.message) not in baseline
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

    @staticmethod
    def _extract_path_from_test_command(command: str) -> str | None:
        """Return the test file path embedded in a test_command string, or None if not identifiable.

        Handles pytest/jest/vitest where the file path is a positional argument.
        Returns None for cargo/npm test (no file path to validate in those commands).
        """
        parts = command.strip().split()
        if not parts:
            return None
        runner = parts[0]
        if runner == "pytest" and len(parts) > 1:
            # pytest tests/test_auth.py::TestClass::test_name -> tests/test_auth.py
            # skip leading flags
            for part in parts[1:]:
                if not part.startswith("-"):
                    return part.split("::")[0]
        if runner in ("jest", "vitest") and len(parts) > 1:
            for part in parts[1:]:
                if not part.startswith("-"):
                    return part.split("::")[0]
        return None

    @staticmethod
    def _build_test_env(shadow_path: Path, real_workspace_path: str) -> dict[str, str]:
        """Return an env dict with project venv / node_modules injected into PATH.

        Search order (first match wins per runtime):
          • <shadow>/.venv/bin  — Python venv inside the shadow copy
          • <real_workspace>/.venv/bin  — Python venv in the original workspace
          • <shadow>/venv/bin, <real_workspace>/venv/bin  — alternate venv names
          • <shadow>/node_modules/.bin  — Node tools (jest, vitest, tsc, eslint)
        All existing directories are prepended so the project's binaries take priority
        over system PATH.
        """
        env = os.environ.copy()
        real = Path(real_workspace_path)
        candidates = [
            shadow_path / ".venv" / "bin",
            real / ".venv" / "bin",
            shadow_path / "venv" / "bin",
            real / "venv" / "bin",
            shadow_path / "node_modules" / ".bin",
            real / "node_modules" / ".bin",
        ]
        extra = [str(p) for p in candidates if p.is_dir()]
        if extra:
            env["PATH"] = os.pathsep.join(extra) + os.pathsep + env.get("PATH", "")
        return env

    async def _run_step_test_command(
        self,
        shadow_path: Path,
        test_command: str,
        real_workspace_path: str,
    ) -> ValidationResult:
        """Execute test_command inside the shadow workspace with venv-aware PATH.

        Failure semantics:
          • command not found (FileNotFoundError) → warning, step not blocked
          • non-zero exit code → hard error, step fails and retries with this output
        """
        parts = test_command.strip().split()
        if not parts:
            return ValidationResult(success=True, diagnostics=[], duration_ms=0)
        cmd, args = parts[0], parts[1:]

        # The validation `test_command` path has its own narrow allowlist of
        # test runners — distinct from the agent's run_command, which is now
        # gated by the interactive command-approval gate (AI_EDITOR_SHELL_POLICY).
        # Hardcoded because expanding the set is rare and the env-var read used
        # to share its name with the now-removed AI_EDITOR_SHELL_ALLOWLIST.
        allowlist = {"pytest", "npm", "cargo", "ruff", "mypy", "tsc", "eslint", "jest", "vitest"}
        if cmd not in allowlist:
            return ValidationResult(
                success=True,
                diagnostics=[
                    Diagnostic(
                        source=f"test_command:{cmd}",
                        message=f"[skipped — '{cmd}' not in shell allowlist]",
                        level="warning",
                    )
                ],
                duration_ms=0,
            )

        env = self._build_test_env(shadow_path, real_workspace_path)
        start_ms = int(time.time() * 1000)
        try:
            proc = await asyncio.create_subprocess_exec(
                cmd,
                *args,
                cwd=str(shadow_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            return ValidationResult(
                success=False,
                diagnostics=[
                    Diagnostic(
                        source=f"test_command:{cmd}",
                        message=f"test_command timed out after 120s: {test_command}",
                        level="error",
                    )
                ],
                duration_ms=120_000,
            )
        except FileNotFoundError:
            # Not on PATH even after venv injection — infra issue, don't block the step.
            logger.warning("test_command binary not found: %s", cmd)
            return ValidationResult(
                success=True,
                diagnostics=[
                    Diagnostic(
                        source=f"test_command:{cmd}",
                        message=f"[skipped — '{cmd}' not found on PATH]",
                        level="warning",
                    )
                ],
                duration_ms=0,
            )

        duration_ms = int(time.time() * 1000) - start_ms
        output = stdout.decode("utf-8", errors="replace")
        exit_code = proc.returncode or 0
        print(f"[TEST] {cmd} exit={exit_code} ({duration_ms}ms)")
        if exit_code != 0:
            return ValidationResult(
                success=False,
                diagnostics=[
                    Diagnostic(
                        source=f"test_command:{cmd}",
                        message=f"exit {exit_code}\n{output[:2000]}",
                        level="error",
                    )
                ],
                duration_ms=duration_ms,
            )
        return ValidationResult(success=True, diagnostics=[], duration_ms=duration_ms)

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

    def _truncate_text(self, text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[:limit] + "\n...[truncated]"

    def _cap_diagnostic_message(self, text: str, limit: int = 5000) -> str:
        """Bound a diagnostic message before it enters an LLM context window.

        A single validator diagnostic can carry the tool's entire stdout blob
        (a ruff/mypy run over a large file is tens of thousands of tokens), which
        overflows the repair loop's context. Keep the head (error type/location)
        and the tail (pytest/cargo failure summaries live at the end) so the
        error stays debuggable; drop the bulk in between.
        """
        if len(text) <= limit:
            return text
        head = (limit * 3) // 4
        tail = limit - head
        dropped = len(text) - head - tail
        return f"{text[:head]}\n...[truncated {dropped:,} chars]...\n{text[-tail:]}"
