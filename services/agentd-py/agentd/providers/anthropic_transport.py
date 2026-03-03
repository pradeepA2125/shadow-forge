from __future__ import annotations

import json
import os
from typing import Any

try:
    from anthropic import AsyncAnthropic as AsyncAnthropicClient
except ImportError:
    AsyncAnthropicClient = None

from agentd.providers.contracts import ModelJsonTransport


class AnthropicJsonTransport(ModelJsonTransport):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        endpoint: str | None = "https://api.anthropic.com/v1/messages",
        anthropic_version: str = "2023-06-01",
        max_tokens: int = 4096,
        timeout_sec: float = 60.0,
        messages_client: Any | None = None,
    ) -> None:
        self._max_tokens = max_tokens

        if messages_client is not None:
            self._messages: Any = messages_client
            return

        resolved_api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not resolved_api_key:
            msg = "ANTHROPIC_API_KEY is required for AnthropicJsonTransport"
            raise RuntimeError(msg)
        if AsyncAnthropicClient is None:
            msg = "anthropic package is required for AnthropicJsonTransport"
            raise RuntimeError(msg)

        client_kwargs: dict[str, Any] = {
            "api_key": resolved_api_key,
            "timeout": timeout_sec,
        }

        base_url = normalize_endpoint_to_base_url(endpoint)
        if base_url is not None:
            client_kwargs["base_url"] = base_url

        if anthropic_version:
            client_kwargs["default_headers"] = {"anthropic-version": anthropic_version}

        client = AsyncAnthropicClient(**client_kwargs)
        self._messages = client.messages

    async def generate_json(
        self,
        *,
        model: str,
        schema_name: str,
        schema: dict[str, object],
        system_instructions: str,
        user_payload: dict[str, object],
    ) -> dict[str, object]:
        response = await self._messages.create(
            model=model,
            max_tokens=self._max_tokens,
            temperature=0,
            system=self._build_system_prompt(
                system_instructions=system_instructions,
                schema_name=schema_name,
                schema=schema,
            ),
            messages=[
                {
                    "role": "user",
                    "content": json.dumps(user_payload),
                }
            ],
        )

        output_text = self._extract_text(response)
        return self._parse_output_object(output_text)

    def _build_system_prompt(
        self,
        *,
        system_instructions: str,
        schema_name: str,
        schema: dict[str, object],
    ) -> str:
        return (
            f"{system_instructions}\n\n"
            f"Return ONLY a valid JSON object matching schema '{schema_name}'.\n"
            f"JSON schema: {json.dumps(schema, separators=(',', ':'))}\n"
            "Do not return markdown, code fences, or commentary."
        )

    def _extract_text(self, response: Any) -> str:
        content = read_value(response, "content")
        if not isinstance(content, list):
            raise RuntimeError("Anthropic response missing content blocks")

        blocks: list[str] = []
        for item in content:
            if read_value(item, "type") != "text":
                continue
            text = read_value(item, "text")
            if isinstance(text, str) and text.strip():
                blocks.append(text)

        output = "\n".join(blocks).strip()
        if not output:
            raise RuntimeError("Anthropic response contained no text output")
        return output

    def _parse_output_object(self, output_text: str) -> dict[str, object]:
        payload_text = strip_json_code_fences(output_text)
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            msg = f"Anthropic output is not valid JSON: {output_text[:500]}"
            raise RuntimeError(msg) from exc

        if not isinstance(payload, dict):
            msg = "Anthropic output must be a JSON object"
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


def normalize_endpoint_to_base_url(endpoint: str | None) -> str | None:
    if endpoint is None:
        return None

    trimmed = endpoint.rstrip("/")
    if not trimmed:
        return None

    suffix = "/v1/messages"
    if trimmed.endswith(suffix):
        return trimmed[: -len(suffix)]

    return trimmed
