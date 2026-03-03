from __future__ import annotations

from typing import Any

from agentd.domain.models import Diagnostic, TaskRecord
from agentd.providers.openai_transport import OpenAIJsonTransport
from agentd.reasoning.engine import DefaultReasoningEngine


class OpenAIReasoningEngine:
    def __init__(
        self,
        model: str = "gpt-5",
        api_key: str | None = None,
        responses_client: Any | None = None,
    ) -> None:
        transport = OpenAIJsonTransport(
            api_key=api_key,
            responses_client=responses_client,
        )
        self._engine = DefaultReasoningEngine(
            model=model,
            transport=transport,
        )

    async def create_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        retrieval_context: dict[str, object],
    ) -> object:
        return await self._engine.create_plan(
            task,
            workspace_path,
            retrieval_context,
        )

    async def create_patch(
        self,
        task: TaskRecord,
        workspace_path: str,
        diagnostics: list[Diagnostic],
        retrieval_context: dict[str, object],
    ) -> object:
        return await self._engine.create_patch(
            task,
            workspace_path,
            diagnostics,
            retrieval_context,
        )
