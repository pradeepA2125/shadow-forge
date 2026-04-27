from __future__ import annotations

from agentd.domain.models import Diagnostic, PlanStep, TaskRecord


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

    async def create_markdown_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        retrieval_context: dict[str, object],
    ) -> str:
        _ = (task, workspace_path, retrieval_context)
        return "# Scripted Plan\n\n- Review generated changes"

    async def critique_markdown_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        retrieval_context: dict[str, object],
        plan_markdown: str,
    ) -> object:
        _ = (task, workspace_path, retrieval_context, plan_markdown)
        return {"verdict": "pass", "issues": []}

    async def critique_json_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        retrieval_context: dict[str, object],
        candidate_plan: dict[str, object],
    ) -> object:
        _ = (task, workspace_path, retrieval_context, candidate_plan)
        return {"verdict": "pass", "issues": []}

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
    ) -> object:
        _ = (
            task,
            workspace_path,
            diagnostics,
            retrieval_context,
            current_step,
            allowed_files,
            max_ops,
            max_files,
            candidate_count,
            last_failure,
        )
        if not self._patches:
            raise RuntimeError("ScriptedReasoningEngine has no patch payloads configured")

        index = min(self._patch_index, len(self._patches) - 1)
        self._patch_index += 1
        return self._patches[index]

    async def create_tool_step(
        self,
        step_context: dict[str, object],
        history: list[dict[str, object]],
        tool_definitions: list[dict[str, object]],
    ) -> dict[str, object]:
        _ = (step_context, history, tool_definitions)
        if not self._patches:
            raise RuntimeError("ScriptedReasoningEngine has no patch payloads configured")

        index = min(self._patch_index, len(self._patches) - 1)
        self._patch_index += 1
        patch_doc = self._patches[index]

        patch_ops: list[object] = []
        if isinstance(patch_doc, dict):
            raw_candidates = patch_doc.get("candidates")
            if isinstance(raw_candidates, list) and raw_candidates:
                first = raw_candidates[0]
                if isinstance(first, dict):
                    ops = first.get("patch_ops")
                    if isinstance(ops, list):
                        patch_ops = ops
        return {"type": "emit_patch", "thought": "scripted engine bypasses tool loop", "patch_ops": patch_ops}

    async def create_planning_step(
        self,
        plan_context: dict[str, object],
        history: list[dict[str, object]],
        tool_definitions: list[dict[str, object]],
    ) -> dict[str, object]:
        _ = (plan_context, history, tool_definitions)
        return {
            "type": "emit_plan",
            "thought": "scripted planning engine bypasses exploration",
            "plan_markdown": "# Scripted Plan\n\n- Review generated changes",
            "files_examined": [],
            "confidence": "high",
        }
