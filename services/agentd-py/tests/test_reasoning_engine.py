from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from agentd.domain.models import Diagnostic, TaskRecord
from agentd.reasoning.engine import DefaultReasoningEngine


class FakeTransport:
    def __init__(self, outputs: list[dict[str, object]]) -> None:
        self._outputs = outputs
        self.calls: list[dict[str, Any]] = []

    async def generate_json(
        self,
        *,
        model: str,
        schema_name: str,
        schema: dict[str, object],
        system_instructions: str,
        user_payload: dict[str, object],
    ) -> dict[str, object]:
        self.calls.append(
            {
                "model": model,
                "schema_name": schema_name,
                "schema": schema,
                "system_instructions": system_instructions,
                "user_payload": user_payload,
            }
        )
        return self._outputs.pop(0)


@pytest.mark.asyncio
async def test_reasoning_engine_builds_plan_and_patch_with_transport(tmp_path: Path) -> None:
    transport = FakeTransport(
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
    engine = DefaultReasoningEngine(model="gpt-5", transport=transport)
    task = TaskRecord(task_id="t1", goal="goal", workspace_path=str(tmp_path))
    retrieval_context = {"related_files": ["a.py"], "related_symbols": ["build"]}
    diagnostics = [Diagnostic(source="validator", message="warn", level="warning")]

    plan = await engine.create_plan(task, str(tmp_path), retrieval_context)
    patch = await engine.create_patch(task, str(tmp_path), diagnostics, retrieval_context)

    assert plan["steps"][0]["id"] == "S1"
    assert patch["patch_ops"][0]["op"] == "create_file"
    assert len(transport.calls) == 2

    plan_call = transport.calls[0]
    patch_call = transport.calls[1]
    assert plan_call["schema_name"] == "plan_document"
    assert patch_call["schema_name"] == "patch_document"
    assert plan_call["user_payload"]["retrieval_context"] == retrieval_context
    assert patch_call["user_payload"]["retrieval_context"] == retrieval_context
    assert patch_call["user_payload"]["diagnostics"][0]["source"] == "validator"
    assert plan_call["user_payload"]["constraints"]["max_files_touched"] == 20
    assert patch_call["user_payload"]["intent"]["mvp_execution_mode"] == "full-plan single-shot patching"
    assert "replace_range" in patch_call["user_payload"]["patch_op_catalog"]
    assert "single JSON object" in plan_call["system_instructions"]
    assert "Allowed ops are exactly" in patch_call["system_instructions"]


@pytest.mark.asyncio
async def test_reasoning_engine_rejects_schema_mismatch() -> None:
    transport = FakeTransport(outputs=[{"analysis": "incomplete"}])
    engine = DefaultReasoningEngine(model="gpt-5", transport=transport)
    task = TaskRecord(task_id="t1", goal="goal", workspace_path=".")

    with pytest.raises(ValidationError):
        await engine.create_plan(task, ".", retrieval_context={})
