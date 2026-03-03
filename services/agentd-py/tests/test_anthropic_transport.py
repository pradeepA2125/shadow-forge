from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from agentd.providers.anthropic_transport import AnthropicJsonTransport


@dataclass
class FakeTextBlock:
    type: str
    text: str


@dataclass
class FakeResponse:
    content: list[Any]


class FakeMessagesClient:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_anthropic_transport_sends_expected_request_shape() -> None:
    client = FakeMessagesClient(
        responses=[
            FakeResponse(
                content=[
                    FakeTextBlock(type="text", text=json.dumps({"ok": True})),
                ]
            )
        ]
    )
    transport = AnthropicJsonTransport(
        api_key="test-key",
        messages_client=client,
        max_tokens=111,
    )

    payload = await transport.generate_json(
        model="claude-3-5-sonnet-latest",
        schema_name="plan_document",
        schema={"type": "object"},
        system_instructions="plan",
        user_payload={"task_id": "task-1"},
    )

    assert payload == {"ok": True}
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["model"] == "claude-3-5-sonnet-latest"
    assert call["max_tokens"] == 111
    assert call["temperature"] == 0
    assert call["messages"][0]["role"] == "user"
    assert json.loads(call["messages"][0]["content"]) == {"task_id": "task-1"}
    assert "plan" in call["system"]
    assert "plan_document" in call["system"]


@pytest.mark.asyncio
async def test_anthropic_transport_rejects_empty_text_output() -> None:
    client = FakeMessagesClient(responses=[FakeResponse(content=[])])
    transport = AnthropicJsonTransport(api_key="test-key", messages_client=client)

    with pytest.raises(RuntimeError, match="no text output"):
        await transport.generate_json(
            model="claude-3-5-sonnet-latest",
            schema_name="patch_document",
            schema={"type": "object"},
            system_instructions="patch",
            user_payload={},
        )


@pytest.mark.asyncio
async def test_anthropic_transport_rejects_invalid_json() -> None:
    client = FakeMessagesClient(
        responses=[
            FakeResponse(
                content=[FakeTextBlock(type="text", text="not-json")],
            )
        ]
    )
    transport = AnthropicJsonTransport(api_key="test-key", messages_client=client)

    with pytest.raises(RuntimeError, match="not valid JSON"):
        await transport.generate_json(
            model="claude-3-5-sonnet-latest",
            schema_name="patch_document",
            schema={"type": "object"},
            system_instructions="patch",
            user_payload={},
        )


@pytest.mark.asyncio
async def test_anthropic_transport_rejects_non_object_json() -> None:
    client = FakeMessagesClient(
        responses=[
            FakeResponse(
                content=[FakeTextBlock(type="text", text=json.dumps(["x"]))],
            )
        ]
    )
    transport = AnthropicJsonTransport(api_key="test-key", messages_client=client)

    with pytest.raises(RuntimeError, match="must be a JSON object"):
        await transport.generate_json(
            model="claude-3-5-sonnet-latest",
            schema_name="patch_document",
            schema={"type": "object"},
            system_instructions="patch",
            user_payload={},
        )


def test_anthropic_transport_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        AnthropicJsonTransport()


def test_anthropic_transport_sdk_constructor_uses_base_url_and_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_kwargs: dict[str, object] = {}

    class FakeSDKClient:
        def __init__(self, **kwargs: object) -> None:
            captured_kwargs.update(kwargs)
            self.messages = object()

    monkeypatch.setattr("agentd.providers.anthropic_transport.AsyncAnthropicClient", FakeSDKClient)

    AnthropicJsonTransport(
        api_key="test-key",
        endpoint="https://anthropic.example/v1/messages",
        anthropic_version="2023-06-01",
        timeout_sec=12.5,
    )

    assert captured_kwargs["api_key"] == "test-key"
    assert captured_kwargs["timeout"] == 12.5
    assert captured_kwargs["base_url"] == "https://anthropic.example"
    assert captured_kwargs["default_headers"] == {"anthropic-version": "2023-06-01"}
