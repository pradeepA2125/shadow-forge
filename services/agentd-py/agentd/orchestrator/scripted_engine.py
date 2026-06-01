from __future__ import annotations

from agentd.domain.models import Diagnostic, PlanStep, TaskRecord


class ScriptedReasoningEngine:
    """Deterministic reasoning engine for tests.

    - ``patches``: patch documents returned by ``create_patch`` and by
      ``create_tool_step`` (when ``tool_step_responses`` is not set).
    - ``tool_step_responses``: ordered list of raw dicts returned one-at-a-time
      by ``create_tool_step``.  Use this to script multi-turn ReAct loops that
      include ``tool_call``, ``emit_patch``, ``verify_done``, etc.  When the list
      is exhausted, the last element is repeated.
    """

    def __init__(
        self,
        plan: object,
        patches: list[object],
        tool_step_responses: list[dict[str, object]] | None = None,
        draft_conventions_responses: list[dict[str, object]] | None = None,
    ) -> None:
        self._plan = plan
        self._patches = patches
        self._patch_index = 0
        self._tool_step_responses: list[dict[str, object]] = tool_step_responses or []
        self._tool_step_index = 0
        self._draft_conventions_responses: list[dict[str, object]] = list(
            draft_conventions_responses or []
        )

    async def create_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        retrieval_context: dict[str, object],
        on_thinking: object = None,
    ) -> object:
        _ = (task, workspace_path, retrieval_context, on_thinking)
        return self._plan

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
        on_thinking: object = None,
        state_description: str = "",
        allowed_action_types: frozenset[str] | None = None,
    ) -> dict[str, object]:
        _ = (step_context, history, tool_definitions, on_thinking, state_description)

        # Prefer explicit tool_step_responses when configured
        if self._tool_step_responses:
            index = min(self._tool_step_index, len(self._tool_step_responses) - 1)
            self._tool_step_index += 1
            return self._tool_step_responses[index]

        # Fallback: unwrap the next patch document into an emit_patch response
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
        on_thinking: object = None,
    ) -> dict[str, object]:
        _ = (plan_context, history, tool_definitions)
        return {
            "type": "emit_plan",
            "thought": "scripted planning engine bypasses exploration",
            "plan_markdown": "# Scripted Plan\n\n- Review generated changes",
            "files_examined": [],
            "confidence": "high",
        }

    async def draft_conventions(self, *, probe: object) -> dict[str, object]:
        _ = probe
        if not self._draft_conventions_responses:
            raise RuntimeError("no draft_conventions response scripted")
        return self._draft_conventions_responses.pop(0)
