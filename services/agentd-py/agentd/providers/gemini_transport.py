from __future__ import annotations

import json
import os
from typing import Any

try:
    from google import genai as google_genai
except ImportError:
    google_genai = None

from agentd.providers.contracts import ModelJsonTransport


class GeminiJsonTransport(ModelJsonTransport):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        models_client: Any | None = None,
    ) -> None:
        self._client: Any | None = None
        if models_client is not None:
            self._models: Any = models_client
            return

        resolved_api_key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not resolved_api_key:
            msg = "GEMINI_API_KEY (or GOOGLE_API_KEY) is required for GeminiJsonTransport"
            raise RuntimeError(msg)
        if google_genai is None:
            msg = "google-genai package is required for GeminiJsonTransport"
            raise RuntimeError(msg)

        client = google_genai.Client(api_key=resolved_api_key)
        # Keep a strong reference to the SDK client for the transport lifetime.
        # The async models handle is backed by this client and can fail if it is collected/closed.
        self._client = client
        self._models = client.aio.models

    async def generate_json(
        self,
        *,
        model: str,
        schema_name: str,
        schema: dict[str, object],
        system_instructions: str,
        user_payload: dict[str, object],
    ) -> dict[str, object]:
        response = await self._models.generate_content(
            model=model,
            contents=json.dumps(user_payload),
            config={
                "temperature": 0,
                "system_instruction": system_instructions,
                "response_mime_type": "application/json",
                "response_json_schema": schema,
            },
        )

        output_text = self._extract_text(response)
        return self._parse_output_object(output_text, schema_name)

    def _extract_text(self, response: Any) -> str:
        text = read_value(response, "text")
        if isinstance(text, str) and text.strip():
            return text.strip()
        raise RuntimeError("Gemini response contained no text output")

    def _parse_output_object(self, output_text: str, schema_name: str) -> dict[str, object]:
        payload_text = strip_json_code_fences(output_text)
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            msg = f"Gemini output is not valid JSON for {schema_name}: {output_text[:500]}"
            raise RuntimeError(msg) from exc

        if not isinstance(payload, dict):
            msg = "Gemini output must be a JSON object"
            raise RuntimeError(msg)

        return payload


def strip_json_code_fences(text: str) -> str:
    raw = text.strip()
    if not raw.startswith("```"):
        return raw

    lines = raw.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def read_value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)
