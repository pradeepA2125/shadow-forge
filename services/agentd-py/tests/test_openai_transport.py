from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from agentd.providers.openai_transport import OpenAIJsonTransport


@dataclass
class FakeResponse:
    output_text: str


class FakeResponsesClient:
    def __init__(self, outputs: list[str]) -> None:
        self._outputs = outputs
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        return FakeResponse(output_text=self._outputs.pop(0))


@pytest.mark.asyncio
async def test_openai_transport_sends_expected_request_shape() -> None:
    fake_client = FakeResponsesClient(outputs=[json.dumps({"ok": True})])
    transport = OpenAIJsonTransport(responses_client=fake_client)

    payload = await transport.generate_json(
        model="gpt-5",
        schema_name="plan_document",
        schema={"type": "object"},
        system_instructions="plan",
        user_payload={"task_id": "task-1", "goal": "x"},
    )

    assert payload == {"ok": True}
    assert len(fake_client.calls) == 1
    call = fake_client.calls[0]
    assert call["model"] == "gpt-5"
    assert call["instructions"] == "plan"
    assert json.loads(call["input"]) == {"task_id": "task-1", "goal": "x"}
    assert call["text"]["format"]["type"] == "json_schema"
    assert call["text"]["format"]["name"] == "plan_document"
    assert call["text"]["format"]["schema"] == {"type": "object"}
    assert call["text"]["format"]["strict"] is True


@pytest.mark.asyncio
async def test_openai_transport_rejects_empty_output_text() -> None:
    fake_client = FakeResponsesClient(outputs=[""])
    transport = OpenAIJsonTransport(responses_client=fake_client)

    with pytest.raises(RuntimeError, match="no output_text"):
        await transport.generate_json(
            model="gpt-5",
            schema_name="plan_document",
            schema={"type": "object"},
            system_instructions="plan",
            user_payload={},
        )


@pytest.mark.asyncio
async def test_openai_transport_rejects_invalid_json() -> None:
    fake_client = FakeResponsesClient(outputs=["not-json"])
    transport = OpenAIJsonTransport(responses_client=fake_client)

    with pytest.raises(RuntimeError, match="not valid JSON"):
        await transport.generate_json(
            model="gpt-5",
            schema_name="plan_document",
            schema={"type": "object"},
            system_instructions="plan",
            user_payload={},
        )


@pytest.mark.asyncio
async def test_openai_transport_rejects_non_object_json() -> None:
    fake_client = FakeResponsesClient(outputs=[json.dumps(["x"])])
    transport = OpenAIJsonTransport(responses_client=fake_client)

    with pytest.raises(RuntimeError, match="must be a JSON object"):
        await transport.generate_json(
            model="gpt-5",
            schema_name="plan_document",
            schema={"type": "object"},
            system_instructions="plan",
            user_payload={},
        )


def test_openai_transport_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        OpenAIJsonTransport()
