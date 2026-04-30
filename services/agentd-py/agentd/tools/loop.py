"""ReAct tool-use loop — two-phase explore+verify execution per plan step."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from agentd.domain.models import (
    AgentToolTrace,
    PlanStep,
    TaskBudget,
    TaskUsage,
    ToolCall,
    ToolResult,
)
from agentd.orchestrator.broadcaster import PatchEventBroadcaster
from agentd.reasoning.contracts import ReasoningEngine
from agentd.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

_MAX_OUTPUT_INJECT_CHARS = int(os.environ.get("AI_EDITOR_TOOL_RESULT_MAX_CHARS", "4000"))


@dataclass
class VerifyResult:
    patch_document: dict[str, object]   # last applied patch (for artifact writing)
    touched_files: list[str]            # all files modified across all emit_patch calls
    verified: bool
    test_output: str                    # empty when no test_command
    tool_trace: AgentToolTrace


@dataclass
class PlanHandoff:
    step_id: str
    reason: str
    evidence: str
    hinted_affected_steps: list[str]
    tool_trace: AgentToolTrace


StepOutcome = VerifyResult | PlanHandoff


class ToolBudgetExceededError(Exception):
    """Raised when the explore budget exhausts before emitting a patch."""


class ToolLoop:
    """Two-phase ReAct loop for a single plan step.

    Phase 1 (explore): agent calls tools and emits a patch. Patch is applied inline.
    Phase 2 (verify): agent runs linters/tests and emits verify_done, or corrects with another patch.
    """

    def __init__(
        self,
        reasoning_engine: ReasoningEngine,
        registry: ToolRegistry,
        broadcaster: PatchEventBroadcaster,
        task_id: str,
        patch_engine: object | None = None,   # PatchEngine — optional for backward compat in tests
        shadow_path: Path | None = None,
    ) -> None:
        self._reasoning = reasoning_engine
        self._registry = registry
        self._broadcaster = broadcaster
        self._task_id = task_id
        self._patch_engine = patch_engine
        self._shadow_path = shadow_path

    async def run(
        self,
        step: PlanStep,
        patch_request_context: dict[str, object],
        budget: TaskBudget,
        usage: TaskUsage,
    ) -> StepOutcome:
        trace = AgentToolTrace(step_id=step.id)
        history: list[dict[str, object]] = []
        phase = "explore"
        explore_calls = 0
        verify_calls = 0
        last_patch_document: dict[str, object] = {}
        all_touched_files: list[str] = []

        retrieval_ctx = patch_request_context.get("retrieval_context") or {}
        if not isinstance(retrieval_ctx, dict):
            retrieval_ctx = {}

        step_context: dict[str, object] = {
            "goal": step.goal,
            "targets": [{"path": t.path, "intent": t.intent} for t in step.targets],
            "risk": step.risk,
            "implementation_details": step.implementation_details,
            "edge_cases": step.edge_cases,
            "design_rationale": step.design_rationale,
            "testing_strategy": step.testing_strategy,
            "allowed_files": patch_request_context.get("allowed_files"),
            "file_contents": retrieval_ctx.get("file_contents"),
            "diagnostics": patch_request_context.get("diagnostics"),
            "last_failure": patch_request_context.get("last_failure"),
            "plan_markdown": patch_request_context.get("plan_markdown"),
        }

        max_explore = budget.max_tool_calls_per_step
        max_verify = budget.max_verify_calls_per_step
        total_budget = max_explore + max_verify + 10  # generous outer cap

        for iteration in range(total_budget):
            tool_defs = [t.model_dump() for t in self._registry.definitions(phase=phase)]

            response = await self._reasoning.create_tool_step(
                step_context=step_context,
                history=history,
                tool_definitions=tool_defs,
            )

            action_type = str(response.get("type", ""))
            thought = str(response.get("thought", ""))

            # ── verify_done ──────────────────────────────────────────────
            if action_type == "verify_done":
                return VerifyResult(
                    patch_document=last_patch_document,
                    touched_files=all_touched_files,
                    verified=bool(response.get("verified", False)),
                    test_output=str(response.get("test_output", "")),
                    tool_trace=trace,
                )

            # ── revision_needed ──────────────────────────────────────────
            if action_type == "revision_needed":
                reason = str(response.get("reason", ""))
                evidence = str(response.get("evidence", ""))
                raw_affected = response.get("affected_steps", [])
                affected = [str(s) for s in raw_affected] if isinstance(raw_affected, list) else []
                logger.info("Tool loop revision_needed: %s", reason[:200],
                            extra={"task_id": self._task_id, "step_id": step.id})
                self._broadcaster.broadcast(self._task_id, {
                    "type": "revision_needed", "step_id": step.id,
                    "reason": reason, "evidence": evidence[:300],
                })
                return PlanHandoff(
                    step_id=step.id, reason=reason, evidence=evidence,
                    hinted_affected_steps=affected, tool_trace=trace,
                )

            # ── emit_patch ───────────────────────────────────────────────
            if action_type == "emit_patch":
                patch_ops = response.get("patch_ops")
                if not isinstance(patch_ops, list):
                    raise ToolBudgetExceededError(
                        f"Step {step.id!r}: emit_patch has non-list 'patch_ops' at iteration {iteration}"
                    )

                patch_document = self._wrap_as_patch_document(patch_ops)
                history.append({"role": "assistant", "content": json.dumps(response, default=str)})

                # Apply inline if patch_engine is available
                if self._patch_engine is not None and self._shadow_path is not None:
                    apply_result = await self._apply_patch_inline(patch_document, step)

                    if apply_result.get("is_error"):
                        error_msg = str(apply_result.get("error", "patch application failed"))
                        logger.warning("Inline patch failed: %s", error_msg,
                                       extra={"task_id": self._task_id, "step_id": step.id})
                        history.append({
                            "role": "tool_result", "tool": "_patch_apply",
                            "content": f"Patch FAILED: {error_msg}\nFix your search strings and re-emit.",
                        })
                        self._broadcaster.broadcast(self._task_id, {
                            "type": "patch_failed", "step_id": step.id, "error": error_msg,
                        })
                        continue  # stay in explore, agent corrects and re-emits

                    # Patch succeeded
                    touched = apply_result.get("touched_files", [])
                    if isinstance(touched, list):
                        for f in touched:
                            if f not in all_touched_files:
                                all_touched_files.append(str(f))
                else:
                    # No patch engine (scripted tests without inline apply) — extract touched files
                    for op in patch_ops:
                        if isinstance(op, dict) and "file" in op:
                            f = str(op["file"])
                            if f not in all_touched_files:
                                all_touched_files.append(f)

                last_patch_document = patch_document

                # Short-circuit if no verify needed
                if not step.test_command:
                    return VerifyResult(
                        patch_document=last_patch_document,
                        touched_files=all_touched_files,
                        verified=True,
                        test_output="",
                        tool_trace=trace,
                    )

                # Transition to verify phase
                phase = "verify"
                history.append({
                    "role": "tool_result", "tool": "_patch_apply",
                    "content": (
                        "Patch applied successfully.\n"
                        "VERIFY PHASE: run linters then tests.\n"
                        f"test_command hint: {step.test_command}\n"
                        "Emit verify_done when all checks pass, or emit_patch again to correct."
                    ),
                })
                self._broadcaster.broadcast(self._task_id, {
                    "type": "patch_applied", "step_id": step.id,
                    "phase": "verify", "touched_files": all_touched_files,
                })
                continue

            # ── tool_call ────────────────────────────────────────────────
            if action_type != "tool_call":
                raise ToolBudgetExceededError(
                    f"Step {step.id!r}: unexpected response type '{action_type}' at iteration {iteration}"
                )

            # Budget enforcement per phase
            if phase == "explore":
                if explore_calls >= max_explore:
                    raise ToolBudgetExceededError(
                        f"Step {step.id!r}: explore budget ({max_explore}) exhausted without emitting a patch"
                    )
                explore_calls += 1
            else:
                if verify_calls >= max_verify:
                    return VerifyResult(
                        patch_document=last_patch_document,
                        touched_files=all_touched_files,
                        verified=False,
                        test_output=f"Verify budget exhausted after {verify_calls} calls without passing checks",
                        tool_trace=trace,
                    )
                verify_calls += 1

            tool_name = str(response.get("tool", ""))
            raw_args = response.get("args")
            args: dict[str, object] = raw_args if isinstance(raw_args, dict) else {}

            self._broadcaster.broadcast(self._task_id, {
                "type": "tool_call", "tool": tool_name,
                "thought": thought[:300], "iteration": iteration + 1, "phase": phase,
            })

            tool_output = await self._registry.execute(tool_name, args)
            usage.tool_calls_used += 1

            self._broadcaster.broadcast(self._task_id, {
                "type": "tool_result", "tool": tool_name,
                "output": tool_output.output[:500], "is_error": tool_output.is_error,
                "iteration": iteration + 1,
            })

            call_id = f"{step.id}-{uuid4().hex[:8]}"
            trace.calls.append(ToolCall(call_id=call_id, tool_name=tool_name, arguments=args))
            trace.results.append(ToolResult(
                call_id=call_id, tool_name=tool_name,
                output=tool_output.output[:_MAX_OUTPUT_INJECT_CHARS],
                is_error=tool_output.is_error,
            ))

            history.append({"role": "assistant", "content": json.dumps(response, default=str)})
            history.append({
                "role": "tool_result", "tool": tool_name,
                "content": tool_output.output[:_MAX_OUTPUT_INJECT_CHARS],
            })

        raise ToolBudgetExceededError(f"Step {step.id!r}: total budget exceeded")

    async def _apply_patch_inline(
        self,
        patch_document: dict[str, object],
        step: PlanStep,
    ) -> dict[str, object]:
        """Apply patch_document to shadow_path. Returns {touched_files, is_error, error}."""
        from agentd.domain.models import PatchDocumentV2
        from pydantic import ValidationError

        assert self._patch_engine is not None
        assert self._shadow_path is not None

        try:
            doc = PatchDocumentV2.model_validate(patch_document)
        except (ValidationError, Exception) as exc:
            return {"is_error": True, "error": f"Invalid patch document: {exc}", "touched_files": []}

        if not doc.candidates:
            return {"is_error": True, "error": "No candidates in patch document", "touched_files": []}

        candidate = doc.candidates[0]
        allowed_files = {t.path for t in step.targets}

        try:
            result = await self._patch_engine.apply_patch_candidate(  # type: ignore[union-attr]
                self._shadow_path,
                candidate,
                allowed_files=allowed_files,
            )
        except Exception as exc:
            return {"is_error": True, "error": str(exc), "touched_files": []}

        if not result.success:
            issues = "; ".join(i.message for i in result.issues[:3])
            return {"is_error": True, "error": issues, "touched_files": []}

        touched = [
            op.get("file", "") for op in (candidate.patch_ops or [])
            if isinstance(op, dict)
        ]
        return {"is_error": False, "touched_files": [f for f in touched if f]}

    @staticmethod
    def _wrap_as_patch_document(patch_ops: list[object]) -> dict[str, object]:
        """Wrap patch ops into a PatchDocumentV2-compatible raw dict."""
        return {
            "candidates": [
                {
                    "candidate_id": "tool-loop-c1",
                    "patch_ops": patch_ops,
                }
            ]
        }


def build_tool_registry(
    shadow_root: Path,
    retrieval_client: object | None = None,
    real_workspace_path: Path | None = None,
) -> ToolRegistry:
    """Construct a ToolRegistry for a step, extracting the semantic index if available."""
    semantic_index = getattr(retrieval_client, "_semantic_index", None)
    return ToolRegistry(
        shadow_root=shadow_root,
        real_workspace_path=real_workspace_path or shadow_root,
        semantic_index=semantic_index,
    )
