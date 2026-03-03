from __future__ import annotations

from typing import Protocol

from agentd.domain.models import Diagnostic, TaskRecord


class ReasoningEngine(Protocol):
    async def create_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        retrieval_context: dict[str, object],
    ) -> object: ...

    async def create_patch(
        self,
        task: TaskRecord,
        workspace_path: str,
        diagnostics: list[Diagnostic],
        retrieval_context: dict[str, object],
    ) -> object: ...
