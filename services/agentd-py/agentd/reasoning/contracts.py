from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from agentd.domain.models import Diagnostic, PlanStep, TaskRecord


class ReasoningEngine(Protocol):
    async def create_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        retrieval_context: dict[str, object],
        on_thinking: Callable[[str], None] | None = None,
    ) -> object: ...

    async def create_patch(
        self,
        task: TaskRecord,
        workspace_path: str,
        diagnostics: list[Diagnostic],
        retrieval_context: dict[str, object],
        *,
        current_step: PlanStep | None = None,
        allowed_files: list[str] | None = None,
        max_ops: int | None = None,
        max_files: int | None = None,
        candidate_count: int | None = None,
        last_failure: dict[str, object] | None = None,
    ) -> object: ...

    async def create_tool_step(
        self,
        step_context: dict[str, object],
        history: list[dict[str, object]],
        tool_definitions: list[dict[str, object]],
        on_thinking: Callable[[str], None] | None = None,
        state_description: str = "",
        allowed_action_types: frozenset[str] | None = None,
    ) -> dict[str, object]:
        """Run one turn of the ReAct loop: given history + tools, return the next action.

        state_description is the per-turn output of
        VerifyPhaseStateMachine.state_description() — injected into the user
        payload so the model knows which state it is in and what is available.

        allowed_action_types restricts the top-level response 'type' enum
        (tool_call / emit_patch / verify_done / revision_needed) to the subset
        valid in the current SM state. When None, all four are allowed (legacy
        behaviour for callers not wired to the SM).

        Returns a dict with at minimum {"type": "tool_call"|"emit_patch", "thought": str}.
        For tool_call: also "tool" (name) and "args" (dict).
        For emit_patch: also "patch_ops" (list of patch op dicts).
        """
        ...

    async def create_planning_step(
        self,
        plan_context: dict[str, object],
        history: list[dict[str, object]],
        tool_definitions: list[dict[str, object]],
        on_thinking: Callable[[str], None] | None = None,
    ) -> dict[str, object]:
        """One turn of the planning ReAct loop.

        Returns a dict with at minimum {"type": "tool_call"|"emit_plan"|"emit_revision", "thought": str}.
        For tool_call: also "tool" (name) and "args" (dict).
        For emit_plan: also "plan_markdown", "files_examined", "confidence".
        For emit_revision: also "revised_steps", "reverted_step_ids", "revision_summary".
        """
        ...
