"""PlanningAgent: owns plan correctness for the agentic editor."""
from __future__ import annotations

from pathlib import Path

from agentd.domain.models import (
    PlanRevisionResult,
    PlanningResult,
    TaskBudget,
    TaskRecord,
)
from agentd.orchestrator.broadcaster import PatchEventBroadcaster
from agentd.planning.loop import PlanningLoop
from agentd.planning.registry import PlanningToolRegistry
from agentd.reasoning.contracts import ReasoningEngine


class PlanningAgent:
    """Stateless agent for plan generation and delta revision.

    All state lives in TaskRecord. This class is a thin coordinator:
    it builds context, delegates to PlanningLoop, and returns typed results.
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

    async def generate_plan(
        self,
        task: TaskRecord,
        initial_context: dict[str, object],
        budget: TaskBudget,
    ) -> PlanningResult:
        """Explore the workspace and produce a markdown plan.

        Args:
            task: Current task (reads goal and workspace_path).
            initial_context: Output of load_context() — seed, not a constraint.
            budget: Controls max_planning_tool_calls.
        """
        plan_context: dict[str, object] = {
            "goal": task.goal,
            "workspace_path": task.workspace_path,
            "initial_context": initial_context,
        }
        loop = PlanningLoop(
            reasoning_engine=self._reasoning,
            registry=self._registry,
            broadcaster=self._broadcaster,
            task_id=self._task_id,
        )
        result = await loop.run(plan_context, budget, revision_mode=False)
        assert isinstance(result, PlanningResult)
        return result

    async def revise(
        self,
        task: TaskRecord,
        real_path: Path,
    ) -> PlanRevisionResult:
        """Explore the actual workspace and produce a targeted plan revision.

        Called after execution agent emits revision_needed.
        Reads the latest DeltaReplanRequest from task.execution_state.
        """
        request = task.execution_state.delta_replan_requests[-1]
        completed_set = set(task.completed_step_ids)
        assert task.plan is not None, "Cannot revise without a plan"

        plan_steps_context = [
            {
                "step_id": s.id,
                "goal": s.goal,
                "targets": [{"path": t.path, "intent": t.intent} for t in s.targets],
                "implementation_details": s.implementation_details,
                "status": (
                    "completed" if s.id in completed_set
                    else "failed" if s.id == request.requested_by_step_id
                    else "pending"
                ),
            }
            for s in task.plan.steps
        ]

        plan_context: dict[str, object] = {
            "goal": task.goal,
            "workspace_path": str(real_path),
            "plan_steps": plan_steps_context,
            "revision_request": {
                "failed_step_id": request.requested_by_step_id,
                "reason": request.reason,
                "evidence": request.evidence,
                "hinted_affected_steps": request.hinted_affected_steps,
            },
            "revertable_step_ids": list(task.execution_state.step_checkpoints.keys()),
        }

        loop = PlanningLoop(
            reasoning_engine=self._reasoning,
            registry=self._registry,
            broadcaster=self._broadcaster,
            task_id=self._task_id,
        )
        result = await loop.run(plan_context, task.budget, revision_mode=True)
        assert isinstance(result, PlanRevisionResult)
        return result
