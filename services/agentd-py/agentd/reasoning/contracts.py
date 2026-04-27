from __future__ import annotations

from typing import Protocol

from agentd.domain.models import Diagnostic, PlanStep, TaskRecord


class ReasoningEngine(Protocol):
    async def create_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        retrieval_context: dict[str, object],
    ) -> object: ...

    async def create_markdown_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        retrieval_context: dict[str, object],
    ) -> str: ...

    async def critique_markdown_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        retrieval_context: dict[str, object],
        plan_markdown: str,
    ) -> object: ...

    async def critique_json_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        retrieval_context: dict[str, object],
        candidate_plan: dict[str, object],
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
    ) -> dict[str, object]:
        """Run one turn of the ReAct loop: given history + tools, return the next action.

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
    ) -> dict[str, object]:
        """One turn of the planning ReAct loop.

        Returns a dict with at minimum {"type": "tool_call"|"emit_plan"|"emit_revision", "thought": str}.
        For tool_call: also "tool" (name) and "args" (dict).
        For emit_plan: also "plan_markdown", "files_examined", "confidence".
        For emit_revision: also "revised_steps", "reverted_step_ids", "revision_summary".
        """
        ...
