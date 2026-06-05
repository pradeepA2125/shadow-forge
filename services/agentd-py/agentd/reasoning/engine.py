from __future__ import annotations

import json
from collections.abc import Callable

from agentd.domain.models import (
    Diagnostic,
    PatchDocumentV2,
    PlanDocument,
    PlanStep,
    TaskRecord,
)
from agentd.providers.contracts import ModelJsonTransport
from agentd.reasoning.contracts import ReasoningEngine
from agentd.reasoning.prompt_builder import (
    PATCH_SYSTEM_INSTRUCTIONS,
    PLAN_SYSTEM_INSTRUCTIONS,
    build_patch_payload,
    build_plan_payload,
)
from agentd.runtime.artifacts import task_artifacts_root


def _debug_dump(
    task_id: str,
    name: str,
    data: object,
    *,
    workspace_path: str,
    step_id: str | None = None,
) -> None:
    try:
        out = task_artifacts_root(task_id, workspace_path)
        if step_id:
            out = out / f"step-{step_id}"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"debug-{name}.json").write_text(
            json.dumps(data, indent=2, default=str), encoding="utf-8"
        )
    except Exception:
        pass


class DefaultReasoningEngine(ReasoningEngine):
    def __init__(
        self,
        *,
        model: str,
        transport: ModelJsonTransport,
    ) -> None:
        self._model = model
        self._transport = transport

    async def create_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        retrieval_context: dict[str, object],
        on_thinking: Callable[[str], None] | None = None,
    ) -> object:
        payload = await self._transport.generate_json(
            model=self._model,
            schema_name="plan_document",
            schema=PlanDocument.model_json_schema(),
            system_instructions=PLAN_SYSTEM_INSTRUCTIONS,
            user_payload=build_plan_payload(
                task,
                workspace_path=workspace_path,
                retrieval_context=retrieval_context,
                plan_validation_feedback=retrieval_context.get("plan_validation_feedback")
                if isinstance(retrieval_context.get("plan_validation_feedback"), dict)
                else None,  # type: ignore[arg-type]
            ),
            on_thinking=on_thinking,
        )
        _debug_dump(task.task_id, "plan-raw", payload, workspace_path=task.workspace_path)
        return PlanDocument.model_validate(payload).model_dump(mode="json")


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
        payload = build_patch_payload(
            task,
            workspace_path=workspace_path,
            diagnostics=diagnostics,
            retrieval_context=retrieval_context,
            current_step=current_step,
            allowed_files=allowed_files,
            max_ops=max_ops,
            max_files=max_files,
            candidate_count=candidate_count,
            last_failure=last_failure,
        )
        
        # Generate patch operations using the enriched payload
        patch_payload = await self._transport.generate_json(
            model=self._model,
            schema_name="patch_document_v2",
            schema=PatchDocumentV2.model_json_schema(),
            system_instructions=PATCH_SYSTEM_INSTRUCTIONS,
            user_payload=payload,
        )
        _debug_dump(
            task.task_id,
            "patch-raw",
            patch_payload,
            workspace_path=task.workspace_path,
            step_id=current_step.id if current_step else None,
        )
        return PatchDocumentV2.model_validate(patch_payload).model_dump(mode="json")

    async def create_tool_step(
        self,
        step_context: dict[str, object],
        history: list[dict[str, object]],
        tool_definitions: list[dict[str, object]],
        on_thinking: Callable[[str], None] | None = None,
        state_description: str = "",
        allowed_action_types: frozenset[str] | None = None,
    ) -> dict[str, object]:
        import copy

        from agentd.reasoning.tool_prompts import (
            AGENT_STEP_RESPONSE_SCHEMA,
            build_tool_step_payload,
            format_tool_system_prompt,
            inject_tools_into_payload,
        )
        user_payload = build_tool_step_payload(
            step_context, history, state_description=state_description,
        )
        inject_tools_into_payload(user_payload, tool_definitions)
        system_instructions = format_tool_system_prompt()

        # Filter the outer `type` enum per SM state when caller specifies what's
        # allowed. Deep-copy the module-level schema so other callers aren't affected.
        schema: dict[str, object] = AGENT_STEP_RESPONSE_SCHEMA
        if allowed_action_types is not None:
            schema = copy.deepcopy(AGENT_STEP_RESPONSE_SCHEMA)
            props = schema.get("properties")
            if isinstance(props, dict):
                type_prop = props.get("type")
                if isinstance(type_prop, dict):
                    # Preserve original ordering for stability.
                    original_enum = type_prop.get("enum")
                    if isinstance(original_enum, list):
                        type_prop["enum"] = [
                            t for t in original_enum if t in allowed_action_types
                        ]

        result = await self._transport.generate_json(
            model=self._model,
            schema_name="agent_step_response",
            schema=schema,
            system_instructions=system_instructions,
            user_payload=user_payload,
            on_thinking=on_thinking,
        )
        return result

    async def create_planning_step(
        self,
        plan_context: dict[str, object],
        history: list[dict[str, object]],
        tool_definitions: list[dict[str, object]],
        on_thinking: Callable[[str], None] | None = None,
    ) -> dict[str, object]:
        from agentd.planning.prompts import (
            _DEFAULT_MAX_TOOL_CALLS,
            PLANNING_STEP_RESPONSE_SCHEMA,
            build_planning_step_payload,
            format_planning_system_prompt,
        )
        revision_mode = "revision_request" in plan_context
        _raw_max = plan_context.get("max_tool_calls", _DEFAULT_MAX_TOOL_CALLS)
        max_calls = int(_raw_max) if isinstance(_raw_max, (int, str)) else _DEFAULT_MAX_TOOL_CALLS
        system_instructions = format_planning_system_prompt(
            tool_definitions, max_calls=max_calls, revision_mode=revision_mode
        )
        user_payload = build_planning_step_payload(plan_context, history, tool_definitions)
        # Per-turn payload capture: dump the EXACT system + user strings sent to the model
        # before the call fires, so a stuck or context-overflowing loop can be inspected
        # byte-for-byte from artifacts even when the call itself 400s. Skipped (via the
        # type guards) on code paths where task_id/workspace_path aren't in plan_context.
        _dbg_task_id = plan_context.get("task_id")
        _dbg_workspace = plan_context.get("workspace_path")
        if isinstance(_dbg_task_id, str) and isinstance(_dbg_workspace, str):
            _turn = len(history) // 2 + 1
            _debug_dump(
                _dbg_task_id,
                f"plan-turn-{_turn:02d}",
                {"system_instructions": system_instructions, "user_payload": user_payload},
                workspace_path=_dbg_workspace,
            )
        result = await self._transport.generate_json(
            model=self._model,
            schema_name="planning_step_response",
            schema=PLANNING_STEP_RESPONSE_SCHEMA,
            system_instructions=system_instructions,
            user_payload=user_payload,
            on_thinking=on_thinking,
        )
        return result if isinstance(result, dict) else {}

    async def draft_conventions(self, *, probe: object) -> dict[str, object]:
        from agentd.reasoning.env_prompts import (
            DRAFT_CONVENTIONS_RESPONSE_SCHEMA,
            DRAFT_CONVENTIONS_SYSTEM_PROMPT,
            build_draft_conventions_payload,
        )
        payload = build_draft_conventions_payload(probe)  # type: ignore[arg-type]
        result = await self._transport.generate_json(
            model=self._model,
            schema_name="env_profile_conventions",
            schema=DRAFT_CONVENTIONS_RESPONSE_SCHEMA,
            system_instructions=DRAFT_CONVENTIONS_SYSTEM_PROMPT,
            user_payload=payload,
        )
        return result if isinstance(result, dict) else {}
