from __future__ import annotations

from agentd.domain.models import Diagnostic, TaskRecord


class ScriptedReasoningEngine:
    def __init__(self, plan: object, patches: list[object]) -> None:
        self._plan = plan
        self._patches = patches
        self._patch_index = 0

    async def create_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        retrieval_context: dict[str, object],
    ) -> object:
        _ = (task, workspace_path, retrieval_context)
        return self._plan

    async def create_patch(
        self,
        task: TaskRecord,
        workspace_path: str,
        diagnostics: list[Diagnostic],
        retrieval_context: dict[str, object],
    ) -> object:
        _ = (task, workspace_path, diagnostics, retrieval_context)
        if not self._patches:
            raise RuntimeError("ScriptedReasoningEngine has no patch payloads configured")

        index = min(self._patch_index, len(self._patches) - 1)
        self._patch_index += 1
        return self._patches[index]
