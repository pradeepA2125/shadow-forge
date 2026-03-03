from __future__ import annotations

import json
import os
from typing import Any

from openai import AsyncOpenAI

from agentd.providers.contracts import ModelJsonTransport


class OpenAIJsonTransport(ModelJsonTransport):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        responses_client: Any | None = None,
    ) -> None:
        if responses_client is not None:
            self._responses: Any = responses_client
            return

        resolved_api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not resolved_api_key:
            msg = "OPENAI_API_KEY is required for OpenAIJsonTransport"
            raise RuntimeError(msg)

        client = AsyncOpenAI(api_key=resolved_api_key)
        self._responses = client.responses

    async def generate_json(
        self,
        *,
        model: str,
        schema_name: str,
        schema: dict[str, object],
        system_instructions: str,
        user_payload: dict[str, object],
    ) -> dict[str, object]:
        response = await self._responses.create(
            model=model,
            instructions=system_instructions,
            input=json.dumps(user_payload),
            temperature=0,
            text={
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "schema": schema,
                    "strict": True,
                }
            },
        )

        output_text = getattr(response, "output_text", "")
        if not output_text:
            msg = "OpenAI response contained no output_text"
            raise RuntimeError(msg)

        try:
            payload = json.loads(output_text)
        except json.JSONDecodeError as exc:
            msg = f"OpenAI output is not valid JSON: {output_text[:500]}"
            raise RuntimeError(msg) from exc

        if not isinstance(payload, dict):
            msg = "OpenAI output must be a JSON object"
            raise RuntimeError(msg)

        return payload
