from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from agentd.domain.models import TaskRecord
from agentd.providers.openai_reasoner import OpenAIReasoningEngine


@dataclass
class FakeResponse:
    output_text: str


class FakeResponsesClient:
    def __init__(self, outputs: list[dict[str, Any]]) -> None:
        self._outputs = outputs
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        payload = self._outputs.pop(0)
        return FakeResponse(output_text=json.dumps(payload))


@pytest.mark.asyncio
async def test_openai_reasoner_generates_schema_valid_plan_and_patch(tmp_path: Path) -> None:
    fake_client = FakeResponsesClient(
        outputs=[
            {
                "analysis": "Plan",
                "steps": [{"id": "S1", "goal": "Edit", "targets": ["a.py"], "risk": "low"}],
                "expected_files": ["a.py"],
                "stop_conditions": ["tests pass"],
            },
            {
                "patch_ops": [
                    {
                        "op": "create_file",
                        "file": "a.py",
                        "content": "print('hi')",
                        "reason": "add file",
                    }
                ]
            },
        ]
    )
    reasoner = OpenAIReasoningEngine(model="gpt-5", responses_client=fake_client)

    task = TaskRecord(task_id="t1", goal="goal", workspace_path=str(tmp_path))

    retrieval_context = {"related_files": ["a.py"], "related_symbols": ["build"]}
    plan = await reasoner.create_plan(task, str(tmp_path), retrieval_context=retrieval_context)
    patch = await reasoner.create_patch(
        task,
        str(tmp_path),
        diagnostics=[],
        retrieval_context=retrieval_context,
    )

    assert plan["steps"][0]["id"] == "S1"
    assert patch["patch_ops"][0]["op"] == "create_file"
    assert len(fake_client.calls) == 2
    first_payload = json.loads(str(fake_client.calls[0]["input"]))
    second_payload = json.loads(str(fake_client.calls[1]["input"]))
    assert first_payload["retrieval_context"]["related_files"] == ["a.py"]
    assert second_payload["retrieval_context"]["related_symbols"] == ["build"]
