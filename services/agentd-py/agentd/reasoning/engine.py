from __future__ import annotations

from agentd.domain.models import Diagnostic, PatchDocument, PlanDocument, TaskRecord
from agentd.providers.contracts import ModelJsonTransport
from agentd.reasoning.contracts import ReasoningEngine
from agentd.reasoning.prompt_builder import (
    PATCH_SYSTEM_INSTRUCTIONS,
    PLAN_SYSTEM_INSTRUCTIONS,
    build_patch_payload,
    build_plan_payload,
)


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
            ),
        )
        return PlanDocument.model_validate(payload).model_dump(mode="json")

    async def create_patch(
        self,
        task: TaskRecord,
        workspace_path: str,
        diagnostics: list[Diagnostic],
        retrieval_context: dict[str, object],
    ) -> object:
        payload = await self._transport.generate_json(
            model=self._model,
            schema_name="patch_document",
            schema=PatchDocument.model_json_schema(),
            system_instructions=PATCH_SYSTEM_INSTRUCTIONS,
            user_payload=build_patch_payload(
                task,
                workspace_path=workspace_path,
                diagnostics=diagnostics,
                retrieval_context=retrieval_context,
            ),
        )
        return PatchDocument.model_validate(payload).model_dump(mode="json")
