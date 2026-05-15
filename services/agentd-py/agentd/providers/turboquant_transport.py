"""TurboQuant transport — llama-cpp-turboquant server, OpenAI chat-completions protocol.

Talks to llama-server built from atomicmilkshake/llama-cpp-turboquant (or any compatible
llama.cpp fork with TurboQuant KV-cache compression). The server exposes the standard
OpenAI /v1/chat/completions endpoint, which supports response_format JSON Schema for
structured output — same constraint mechanism as Gemini/Groq.

Key difference from OllamaJsonTransport:
  - Endpoint: /v1/chat/completions (not /api/chat)
  - Structured output: response_format.json_schema (not format=<schema>)
  - Response path: choices[0].message.content (not message.content)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import httpx

from agentd.providers.contracts import ModelJsonTransport

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})
_RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
)

_DEFAULT_HOST = "http://localhost:11435"


class TurboQuantTransport(ModelJsonTransport):
    """JSON transport backed by a llama-cpp-turboquant server."""

    def __init__(
        self,
        *,
        host: str | None = None,
        timeout_sec: float = 600.0,
        max_retries: int = 4,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._host = (host or os.getenv("TURBOQUANT_HOST") or _DEFAULT_HOST).rstrip("/")
        self._timeout_sec = timeout_sec
        self._max_retries = max(0, max_retries)
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(timeout=timeout_sec)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def generate_json(
        self,
        *,
        model: str,
        schema_name: str,
        schema: dict[str, object],
        system_instructions: str,
        user_payload: dict[str, object],
    ) -> dict[str, object]:
        # Use json_object (not json_schema) so the grammar constraint does NOT block
        # </think>. With json_schema strict mode the grammar prevents </think> from
        # being emitted, trapping the model in an infinite thinking loop. With
        # json_object the model thinks naturally, closes </think>, then outputs JSON.
        instructions_with_schema = (
            f"{system_instructions}\n\n"
            f"REQUIRED OUTPUT FORMAT — JSON object matching this schema:\n"
            f"{json.dumps(schema, indent=2)}\n"
            "Return ONLY the JSON object. No markdown fences. No commentary."
        )
        contents = json.dumps(user_payload)
        body = self._build_body(
            model=model,
            system=instructions_with_schema,
            user_content=contents,
            use_json_object=True,
            max_tokens=0,
        )
        response = await self._call_with_retry(body)
        self._log_usage(model, schema_name, system_instructions, contents, response)
        output_text = self._extract_text(response)
        logger.debug("turboquant raw output (%s): %s", schema_name, output_text[:600])
        return self._parse_output_object(output_text, schema_name)

    async def generate_text(
        self,
        *,
        model: str,
        system_instructions: str,
        user_payload: dict[str, object],
    ) -> str:
        contents = json.dumps(user_payload)
        body = self._build_body(
            model=model,
            system=system_instructions,
            user_content=contents,
            max_tokens=0,
        )
        response = await self._call_with_retry(body)
        self._log_usage(model, "text", system_instructions, contents, response)
        return self._extract_text(response)

    def _build_body(
        self,
        *,
        model: str,
        system: str,
        user_content: str,
        use_json_object: bool = False,
        max_tokens: int = 0,
    ) -> dict[str, object]:
        body: dict[str, object] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0,
            "stream": False,
        }
        if max_tokens > 0:
            body["max_tokens"] = max_tokens
        if use_json_object:
            body["response_format"] = {"type": "json_object"}
        return body

    async def _call_with_retry(self, body: dict[str, object]) -> dict[str, Any]:
        url = f"{self._host}/v1/chat/completions"
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            if attempt > 0:
                delay = min(5.0 * (2 ** (attempt - 1)), 60.0)
                logger.warning(
                    "TurboQuant transient error (attempt %d/%d), retrying in %.0fs",
                    attempt, self._max_retries, delay,
                )
                await asyncio.sleep(delay)

            try:
                response = await asyncio.wait_for(
                    self._client.post(url, json=body),
                    timeout=self._timeout_sec,
                )
            except TimeoutError as exc:
                msg = f"TurboQuant request timed out after {self._timeout_sec}s"
                raise RuntimeError(msg) from exc
            except _RETRYABLE_EXCEPTIONS as exc:
                last_exc = exc
                continue
            except Exception:
                raise

            if response.status_code in _RETRYABLE_STATUS_CODES:
                last_exc = httpx.HTTPStatusError(
                    f"TurboQuant returned {response.status_code}: {response.text[:200]}",
                    request=response.request,
                    response=response,
                )
                continue
            if response.status_code >= 400:
                raise RuntimeError(
                    f"TurboQuant returned {response.status_code}: {response.text[:500]}"
                )
            try:
                return response.json()
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"TurboQuant returned non-JSON body: {response.text[:200]}"
                ) from exc

        assert last_exc is not None
        raise RuntimeError(
            f"TurboQuant request failed after {self._max_retries} retries: {last_exc}"
        ) from last_exc

    @staticmethod
    def _extract_text(response: dict[str, Any]) -> str:
        choices = response.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message", {})
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
            # Fallback: some llama-server builds emit JSON inside reasoning_content
            # when the grammar constraint prevents clean separation of think/answer.
            reasoning = message.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning.strip():
                # Try to extract the last JSON object from the thinking trace
                text = reasoning.strip()
                last_brace = text.rfind("{")
                if last_brace != -1:
                    candidate = text[last_brace:]
                    if candidate.rstrip().endswith("}"):
                        return candidate.strip()
        raise RuntimeError("TurboQuant response contained no text content")

    @staticmethod
    def _parse_output_object(output_text: str, schema_name: str) -> dict[str, object]:
        text = output_text.strip()
        # Find the start of the JSON object (skip any leading thinking trace)
        start = text.find("{")
        if start == -1:
            raise RuntimeError(
                f"TurboQuant output is not valid JSON for {schema_name}: {text[:500]}"
            )
        text = text[start:]
        try:
            # raw_decode consumes exactly one JSON value and ignores trailing garbage
            # (e.g. extra closing braces emitted by qwen3-family models)
            payload, _ = json.JSONDecoder().raw_decode(text)
        except json.JSONDecodeError as exc:
            msg = f"TurboQuant output is not valid JSON for {schema_name}: {text[:500]}"
            raise RuntimeError(msg) from exc
        if not isinstance(payload, dict):
            msg = "TurboQuant output must be a JSON object"
            raise RuntimeError(msg)
        return payload

    @staticmethod
    def _log_usage(
        model: str,
        schema_name: str,
        system_instructions: str,
        contents: str,
        response: dict[str, Any],
    ) -> None:
        usage = response.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")
        logger.info(
            "turboquant call: model=%s schema=%s sys_chars=%d user_chars=%d "
            "prompt_tokens=%s output_tokens=%s",
            model, schema_name,
            len(system_instructions), len(contents),
            prompt_tokens, output_tokens,
        )
