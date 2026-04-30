"""ReAct tool-use loop for Phase 4 agentic execution.

Replaces the single-shot create_patch() call with a Thought→Tool→Observe loop.
The agent gathers context via tools and emits a patch when confident.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
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
class PatchResult:
    patch_document: dict[str, object]
    tool_trace: AgentToolTrace


@dataclass
class PlanHandoff:
    step_id: str
    reason: str
    evidence: str
    hinted_affected_steps: list[str]
    tool_trace: AgentToolTrace


StepOutcome = PatchResult | PlanHandoff


class ToolBudgetExceededError(Exception):
    """Raised when the step uses all tool-call budget without emitting a patch."""


class ToolLoop:
    """Implements the ReAct (Reason + Act) loop for a single plan step.

    Each iteration:
    1. Calls reasoning_engine.create_tool_step() with accumulated history
    2. If the response is emit_patch → returns the patch ops
    3. If the response is tool_call → executes the tool, appends result to history
    4. Repeats until budget exhausted (raises ToolBudgetExceededError)
    """

    def __init__(
        self,
        reasoning_engine: ReasoningEngine,
        registry: ToolRegistry,
        broadcaster: PatchEventBroadcaster,
        task_id: str,
    ) -> None:
        self._reasoning = reasoning_engine
        self._registry = registry
        self._broadcaster = broadcaster
        self._task_id = task_id

    async def run(
        self,
        step: PlanStep,
        patch_request_context: dict[str, object],
        budget: TaskBudget,
        usage: TaskUsage,
    ) -> StepOutcome:
        """Run the ReAct loop for one plan step.

        Returns PatchResult on success or PlanHandoff when the agent signals revision_needed.
        """
        trace = AgentToolTrace(step_id=step.id)
        history: list[dict[str, object]] = []
        tool_defs = [t.model_dump() for t in self._registry.definitions()]

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

        max_calls = budget.max_tool_calls_per_step

        for iteration in range(max_calls + 1):
            response = await self._reasoning.create_tool_step(
                step_context=step_context,
                history=history,
                tool_definitions=tool_defs,
            )

            action_type = str(response.get("type", ""))
            thought = str(response.get("thought", ""))

            if action_type == "emit_patch":
                patch_ops = response.get("patch_ops")
                if not isinstance(patch_ops, list):
                    raise ToolBudgetExceededError(
                        f"Step {step.id!r}: emit_patch response has non-list 'patch_ops' at iteration {iteration}"
                    )
                logger.debug(
                    "Tool loop emitting patch",
                    extra={
                        "task_id": self._task_id,
                        "step_id": step.id,
                        "iteration": iteration,
                        "op_count": len(patch_ops),
                    },
                )
                return PatchResult(
                    patch_document=self._wrap_as_patch_document(patch_ops),
                    tool_trace=trace,
                )

            if action_type == "revision_needed":
                reason = str(response.get("reason", ""))
                evidence = str(response.get("evidence", ""))
                raw_affected = response.get("affected_steps", [])
                affected = [str(s) for s in raw_affected] if isinstance(raw_affected, list) else []
                logger.info(
                    "Tool loop revision_needed: %s",
                    reason[:200],
                    extra={"task_id": self._task_id, "step_id": step.id},
                )
                self._broadcaster.broadcast(self._task_id, {
                    "type": "revision_needed",
                    "step_id": step.id,
                    "reason": reason,
                    "evidence": evidence[:300],
                })
                return PlanHandoff(
                    step_id=step.id,
                    reason=reason,
                    evidence=evidence,
                    hinted_affected_steps=affected,
                    tool_trace=trace,
                )

            if action_type != "tool_call":
                raise ToolBudgetExceededError(
                    f"Step {step.id!r}: unexpected response type '{action_type}' at iteration {iteration}; "
                    "expected tool_call, emit_patch, or revision_needed"
                )

            if iteration >= max_calls:
                raise ToolBudgetExceededError(
                    f"Step {step.id!r} used {max_calls} tool calls without emitting a patch"
                )

            tool_name = str(response.get("tool", ""))
            raw_args = response.get("args")
            args: dict[str, object] = raw_args if isinstance(raw_args, dict) else {}

            logger.debug(
                "Tool call: %s args=%s",
                tool_name,
                json.dumps(args, default=str)[:200],
                extra={"task_id": self._task_id, "step_id": step.id, "iteration": iteration},
            )

            # Broadcast tool_call event (visible in VS Code activity log)
            self._broadcaster.broadcast(self._task_id, {
                "type": "tool_call",
                "tool": tool_name,
                "thought": thought[:300],
                "iteration": iteration + 1,
            })

            # Execute tool
            tool_output = await self._registry.execute(tool_name, args)
            usage.tool_calls_used += 1

            # Broadcast tool_result event
            self._broadcaster.broadcast(self._task_id, {
                "type": "tool_result",
                "tool": tool_name,
                "output": tool_output.output[:500],
                "is_error": tool_output.is_error,
                "iteration": iteration + 1,
            })

            # Record in trace
            call_id = f"{step.id}-{uuid4().hex[:8]}"
            trace.calls.append(ToolCall(
                call_id=call_id,
                tool_name=tool_name,
                arguments=args,
            ))
            trace.results.append(ToolResult(
                call_id=call_id,
                tool_name=tool_name,
                output=tool_output.output[:_MAX_OUTPUT_INJECT_CHARS],
                is_error=tool_output.is_error,
            ))

            # Extend conversation history (flattened into single-turn payload)
            history.append({"role": "assistant", "content": json.dumps(response, default=str)})
            history.append({
                "role": "tool_result",
                "tool": tool_name,
                "content": tool_output.output[:_MAX_OUTPUT_INJECT_CHARS],
            })

        # Should be unreachable (ToolBudgetExceededError raised above when iteration >= max_calls)
        raise ToolBudgetExceededError(f"Step {step.id!r}: loop exited without patch")

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
