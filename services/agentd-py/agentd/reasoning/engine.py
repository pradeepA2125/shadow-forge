from __future__ import annotations

import json
from pathlib import Path as _Path

from agentd.domain.models import (
    Diagnostic,
    PatchDocumentV2,
    PlanCritiqueResult,
    PlanDocument,
    PlanStep,
    TaskRecord,
)
from agentd.providers.contracts import ModelJsonTransport
from agentd.reasoning.contracts import ReasoningEngine
from agentd.reasoning.prompt_builder import (
    JSON_PLAN_CRITIQUE_SYSTEM_INSTRUCTIONS,
    MARKDOWN_PLAN_SYSTEM_INSTRUCTIONS,
    MARKDOWN_PLAN_CRITIQUE_SYSTEM_INSTRUCTIONS,
    PATCH_SYSTEM_INSTRUCTIONS,
    PLAN_SYSTEM_INSTRUCTIONS,
    build_json_plan_critique_payload,
    build_markdown_plan_critique_payload,
    build_patch_payload,
    build_plan_payload,
)
from agentd.runtime.artifacts import task_artifacts_root


def _debug_dump_input(
    task_id: str,
    name: str,
    system_instructions: str,
    user_payload: object,
    *,
    workspace_path: str,
) -> None:
    """Dump the complete LLM input (system + user) for token analysis."""
    try:
        out = task_artifacts_root(task_id, workspace_path)
        out.mkdir(parents=True, exist_ok=True)
        full_input = {
            "system_instructions": system_instructions,
            "user_payload": user_payload,
            "system_token_estimate": len(system_instructions) // 4,
            "user_payload_token_estimate": len(json.dumps(user_payload, default=str)) // 4,
        }
        (out / f"debug-input-{name}.json").write_text(
            json.dumps(full_input, indent=2, default=str), encoding="utf-8"
        )
    except Exception:
        pass


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
                else None,
            ),
        )
        _debug_dump(task.task_id, "plan-raw", payload, workspace_path=task.workspace_path)
        return PlanDocument.model_validate(payload).model_dump(mode="json")

    async def create_markdown_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        retrieval_context: dict[str, object],
    ) -> str:
        user_payload = build_plan_payload(
            task,
            workspace_path=workspace_path,
            retrieval_context=retrieval_context,
            plan_feedback=retrieval_context.get("plan_feedback"),
        )
        _debug_dump_input(
            task.task_id,
            "markdown-plan",
            MARKDOWN_PLAN_SYSTEM_INSTRUCTIONS,
            user_payload,
            workspace_path=task.workspace_path,
        )
        content = await self._transport.generate_text(
            model=self._model,
            system_instructions=MARKDOWN_PLAN_SYSTEM_INSTRUCTIONS,
            user_payload=user_payload,
        )
        _debug_dump(
            task.task_id,
            "plan-markdown",
            {"content": content},
            workspace_path=task.workspace_path,
        )
        return content

    async def critique_markdown_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        retrieval_context: dict[str, object],
        plan_markdown: str,
    ) -> object:
        payload = await self._transport.generate_json(
            model=self._model,
            schema_name="markdown_plan_critique",
            schema=PlanCritiqueResult.model_json_schema(),
            system_instructions=MARKDOWN_PLAN_CRITIQUE_SYSTEM_INSTRUCTIONS,
            user_payload=build_markdown_plan_critique_payload(
                task,
                workspace_path=workspace_path,
                retrieval_context=retrieval_context,
                plan_markdown=plan_markdown,
                plan_feedback=retrieval_context.get("plan_feedback")
                if isinstance(retrieval_context.get("plan_feedback"), str)
                else None,
            ),
        )
        _debug_dump(
            task.task_id,
            "markdown-plan-critique-raw",
            payload,
            workspace_path=task.workspace_path,
        )
        return PlanCritiqueResult.model_validate(payload).model_dump(mode="json")

    async def critique_json_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        retrieval_context: dict[str, object],
        candidate_plan: dict[str, object],
    ) -> object:
        payload = await self._transport.generate_json(
            model=self._model,
            schema_name="json_plan_critique",
            schema=PlanCritiqueResult.model_json_schema(),
            system_instructions=JSON_PLAN_CRITIQUE_SYSTEM_INSTRUCTIONS,
            user_payload=build_json_plan_critique_payload(
                task,
                workspace_path=workspace_path,
                retrieval_context=retrieval_context,
                plan_markdown=task.plan_markdown or "",
                candidate_plan=candidate_plan,
                plan_validation_feedback=retrieval_context.get("plan_validation_feedback")
                if isinstance(retrieval_context.get("plan_validation_feedback"), dict)
                else None,
            ),
        )
        _debug_dump(
            task.task_id,
            "json-plan-critique-raw",
            payload,
            workspace_path=task.workspace_path,
        )
        return PlanCritiqueResult.model_validate(payload).model_dump(mode="json")

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
    ) -> dict[str, object]:
        from agentd.reasoning.tool_prompts import (
            AGENT_STEP_RESPONSE_SCHEMA,
            build_tool_step_payload,
            format_tool_system_prompt,
        )
        user_payload = build_tool_step_payload(step_context, history, tool_definitions)
        system_instructions = format_tool_system_prompt(tool_definitions)
        result = await self._transport.generate_json(
            model=self._model,
            schema_name="agent_step_response",
            schema=AGENT_STEP_RESPONSE_SCHEMA,
            system_instructions=system_instructions,
            user_payload=user_payload,
        )
        return result

    async def create_planning_step(
        self,
        plan_context: dict[str, object],
        history: list[dict[str, object]],
        tool_definitions: list[dict[str, object]],
    ) -> dict[str, object]:
        from agentd.planning.prompts import (
            PLANNING_STEP_RESPONSE_SCHEMA,
            build_planning_step_payload,
            format_planning_system_prompt,
        )
        revision_mode = "revision_request" in plan_context
        system_instructions = format_planning_system_prompt(tool_definitions, revision_mode=revision_mode)
        user_payload = build_planning_step_payload(plan_context, history, tool_definitions)
        result = await self._transport.generate_json(
            model=self._model,
            schema_name="planning_step_response",
            schema=PLANNING_STEP_RESPONSE_SCHEMA,
            system_instructions=system_instructions,
            user_payload=user_payload,
        )
        return result if isinstance(result, dict) else {}
