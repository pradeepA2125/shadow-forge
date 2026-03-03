from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

import agentd.providers.gemini_transport as gemini_transport_module
from agentd.providers.gemini_transport import GeminiJsonTransport


@dataclass
class FakeResponse:
    text: str


class FakeModelsClient:
    def __init__(self, outputs: list[str]) -> None:
        self._outputs = outputs
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        return FakeResponse(text=self._outputs.pop(0))


@pytest.mark.asyncio
async def test_gemini_transport_sends_expected_request_shape() -> None:
    fake_client = FakeModelsClient(outputs=[json.dumps({"ok": True})])
    transport = GeminiJsonTransport(models_client=fake_client)

    payload = await transport.generate_json(
        model="gemini-3-flash-preview",
        schema_name="plan_document",
        schema={"type": "object"},
        system_instructions="plan",
        user_payload={"task_id": "task-1", "goal": "x"},
    )

    assert payload == {"ok": True}
    assert len(fake_client.calls) == 1
    call = fake_client.calls[0]
    assert call["model"] == "gemini-3-flash-preview"
    assert json.loads(call["contents"]) == {"task_id": "task-1", "goal": "x"}
    assert call["config"]["temperature"] == 0
    assert call["config"]["system_instruction"] == "plan"
    assert call["config"]["response_mime_type"] == "application/json"
    assert call["config"]["response_json_schema"] == {"type": "object"}


@pytest.mark.asyncio
async def test_gemini_transport_rejects_empty_text_output() -> None:
    fake_client = FakeModelsClient(outputs=[""])
    transport = GeminiJsonTransport(models_client=fake_client)

    with pytest.raises(RuntimeError, match="no text output"):
        await transport.generate_json(
            model="gemini-3-flash-preview",
            schema_name="plan_document",
            schema={"type": "object"},
            system_instructions="plan",
            user_payload={},
        )


@pytest.mark.asyncio
async def test_gemini_transport_rejects_invalid_json() -> None:
    fake_client = FakeModelsClient(outputs=["not-json"])
    transport = GeminiJsonTransport(models_client=fake_client)

    with pytest.raises(RuntimeError, match="not valid JSON"):
        await transport.generate_json(
            model="gemini-3-flash-preview",
            schema_name="plan_document",
            schema={"type": "object"},
            system_instructions="plan",
            user_payload={},
        )


@pytest.mark.asyncio
async def test_gemini_transport_rejects_non_object_json() -> None:
    fake_client = FakeModelsClient(outputs=[json.dumps(["x"])])
    transport = GeminiJsonTransport(models_client=fake_client)

    with pytest.raises(RuntimeError, match="must be a JSON object"):
        await transport.generate_json(
            model="gemini-3-flash-preview",
            schema_name="plan_document",
            schema={"type": "object"},
            system_instructions="plan",
            user_payload={},
        )


def test_gemini_transport_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        GeminiJsonTransport()


def test_gemini_transport_sdk_constructor_keeps_client_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_api_key: dict[str, str] = {}

    class FakeSDKClient:
        def __init__(self, api_key: str) -> None:
            captured_api_key["value"] = api_key
            self.aio = type("FakeAio", (), {"models": object()})()

    fake_google = type("FakeGoogleGenAI", (), {"Client": FakeSDKClient})()
    monkeypatch.setattr(gemini_transport_module, "google_genai", fake_google)

    transport = GeminiJsonTransport(api_key="gemini-test-key")

    assert captured_api_key["value"] == "gemini-test-key"
    assert transport._client is not None
