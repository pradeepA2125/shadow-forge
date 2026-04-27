"""Explore-then-commit ReAct loop for the PlanningAgent."""
from __future__ import annotations

import json
import logging
import os
from uuid import uuid4

from agentd.domain.models import (
    AgentToolTrace,
    PlanRevisionResult,
    PlanningResult,
    RevisedStep,
    TaskBudget,
    ToolCall,
    ToolResult,
)
from agentd.orchestrator.broadcaster import PatchEventBroadcaster
from agentd.planning.registry import PlanningToolRegistry
from agentd.reasoning.contracts import ReasoningEngine

logger = logging.getLogger(__name__)

_MAX_OUTPUT_INJECT_CHARS = int(os.environ.get("AI_EDITOR_TOOL_RESULT_MAX_CHARS", "4000"))


class PlanningBudgetExceededError(Exception):
    """Raised when the planning loop exhausts its tool-call budget."""


def _validate_no_duplicate_file_targets(steps: list[dict[str, object]]) -> list[str]:
    """Check that no file path appears in more than one step's targets."""
    seen: dict[str, str] = {}
    errors: list[str] = []
    for step in steps:
        step_id = str(step.get("id", step.get("step_id", "?")))
        targets = step.get("targets", [])
        if not isinstance(targets, list):
            continue
        for target in targets:
            path = target.get("path", "") if isinstance(target, dict) else str(target)
            if path in seen:
                errors.append(
                    f"File '{path}' appears in both step '{seen[path]}' and step '{step_id}'. "
                    "Consolidate all changes to this file into one step."
                )
            else:
                seen[path] = step_id
    return errors


class PlanningLoop:
    """Implements the explore-then-commit ReAct loop for PlanningAgent.

    Calls reasoning_engine.create_planning_step() each iteration.
    Returns when the agent emits emit_plan or emit_revision.
    Raises PlanningBudgetExceededError if budget exhausted without emitting.
    """

    def __init__(
        self,
        reasoning_engine: ReasoningEngine,
        registry: PlanningToolRegistry,
        broadcaster: PatchEventBroadcaster,
        task_id: str,
    ) -> None:
        self._reasoning = reasoning_engine
        self._registry = registry
        self._broadcaster = broadcaster
        self._task_id = task_id

    async def run(
        self,
        plan_context: dict[str, object],
        budget: TaskBudget,
        *,
        revision_mode: bool = False,
    ) -> PlanningResult | PlanRevisionResult:
        """Run one planning loop. Returns PlanningResult or PlanRevisionResult."""
        tool_defs = [t.model_dump() for t in self._registry.definitions()]
        max_calls = (
            budget.max_revision_tool_calls if revision_mode else budget.max_planning_tool_calls
        )
        emit_type = "emit_revision" if revision_mode else "emit_plan"
        return await self._run_single_pass(
            plan_context=plan_context,
            tool_defs=tool_defs,
            max_calls=max_calls,
            emit_type=emit_type,
        )

    async def _run_single_pass(
        self,
        plan_context: dict[str, object],
        tool_defs: list[dict[str, object]],
        max_calls: int,
        emit_type: str,
    ) -> PlanningResult | PlanRevisionResult:
        trace = AgentToolTrace(step_id="planning")
        history: list[dict[str, object]] = []

        for iteration in range(max_calls + 1):
            response = await self._reasoning.create_planning_step(
                plan_context=plan_context,
                history=history,
                tool_definitions=tool_defs,
            )

            action_type = str(response.get("type", ""))
            thought = str(response.get("thought", ""))

            if action_type == "emit_plan":
                plan_markdown = str(response.get("plan_markdown", ""))
                files_examined = list(response.get("files_examined", []))
                confidence = str(response.get("confidence", "medium"))
                if confidence not in ("high", "medium", "low"):
                    confidence = "medium"
                self._broadcaster.broadcast(self._task_id, {
                    "type": "planning_complete",
                    "files_examined": files_examined,
                    "confidence": confidence,
                })
                return PlanningResult(
                    plan_markdown=plan_markdown,
                    files_examined=files_examined,
                    confidence=confidence,  # type: ignore[arg-type]
                    tool_trace=trace,
                )

            if action_type == "emit_revision":
                raw_steps = response.get("revised_steps", [])
                if not isinstance(raw_steps, list):
                    raw_steps = []
                revised_steps = [
                    RevisedStep(
                        step_id=str(s.get("step_id", "")),
                        goal=str(s.get("goal", "")),
                        targets=s.get("targets", []),  # type: ignore[arg-type]
                        implementation_details=str(s.get("implementation_details", "")),
                        edge_cases=str(s.get("edge_cases", "")),
                        testing_strategy=str(s.get("testing_strategy", "")),
                        risk=str(s.get("risk", "low")),
                    )
                    for s in raw_steps
                    if isinstance(s, dict)
                ]
                reverted_step_ids = list(response.get("reverted_step_ids", []))
                revision_summary = str(response.get("revision_summary", ""))
                return PlanRevisionResult(
                    revised_steps=revised_steps,
                    reverted_step_ids=reverted_step_ids,
                    revision_summary=revision_summary,
                    tool_trace=trace,
                )

            if action_type != "tool_call":
                logger.warning(
                    "Unexpected planning loop response type '%s'; treating as empty plan",
                    action_type,
                    extra={"task_id": self._task_id},
                )
                return PlanningResult(
                    plan_markdown="",
                    files_examined=[],
                    confidence="low",
                    tool_trace=trace,
                )

            if iteration >= max_calls:
                raise PlanningBudgetExceededError(
                    f"Planning loop used {max_calls} tool calls without emitting {emit_type}"
                )

            tool_name = str(response.get("tool", ""))
            raw_args = response.get("args")
            args: dict[str, object] = raw_args if isinstance(raw_args, dict) else {}

            self._broadcaster.broadcast(self._task_id, {
                "type": "planning_tool_call",
                "tool": tool_name,
                "thought": thought[:300],
                "iteration": iteration + 1,
            })

            tool_output = await self._registry.execute(tool_name, args)

            self._broadcaster.broadcast(self._task_id, {
                "type": "planning_tool_result",
                "tool": tool_name,
                "output": tool_output.output[:500],
                "is_error": tool_output.is_error,
                "iteration": iteration + 1,
            })

            call_id = f"plan-{uuid4().hex[:8]}"
            trace.calls.append(ToolCall(call_id=call_id, tool_name=tool_name, arguments=args))
            trace.results.append(ToolResult(
                call_id=call_id,
                tool_name=tool_name,
                output=tool_output.output[:_MAX_OUTPUT_INJECT_CHARS],
                is_error=tool_output.is_error,
            ))

            history.append({"role": "assistant", "content": json.dumps(response, default=str)})
            history.append({
                "role": "tool_result",
                "tool": tool_name,
                "content": tool_output.output[:_MAX_OUTPUT_INJECT_CHARS],
            })

        raise PlanningBudgetExceededError("Planning loop exited without result")
