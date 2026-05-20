from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from agentd.domain.models import Diagnostic, TaskRecord
from agentd.reasoning.engine import DefaultReasoningEngine


class FakeTransport:
    def __init__(self, *, json_outputs: list[dict[str, object]] | None = None) -> None:
        self._json_outputs = json_outputs or []
        self.calls: list[dict[str, Any]] = []

    async def generate_json(
        self,
        *,
        model: str,
        schema_name: str,
        schema: dict[str, object],
        system_instructions: str,
        user_payload: dict[str, object],
        on_thinking: object = None,
    ) -> dict[str, object]:
        _ = on_thinking
        self.calls.append(
            {
                "kind": "json",
                "model": model,
                "schema_name": schema_name,
                "schema": schema,
                "system_instructions": system_instructions,
                "user_payload": user_payload,
            }
        )
        return self._json_outputs.pop(0)

    async def generate_text(self, **_: object) -> str:
        raise NotImplementedError("generate_text is not used")


@pytest.mark.asyncio
async def test_reasoning_engine_builds_plan_and_patch_with_transport(tmp_path: Path) -> None:
    transport = FakeTransport(
        json_outputs=[
            {
                "analysis": "Plan",
                "steps": [
                    {
                        "id": "S1",
                        "goal": "Edit",
                        "targets": [{"path": "a.py", "intent": "existing"}],
                        "risk": "low",
                    }
                ],
                "expected_files": ["a.py"],
                "stop_conditions": ["tests pass"],
            },
            {
                "candidates": [
                    {
                        "candidate_id": "c1",
                        "patch_ops": [
                            {
                                "op": "create_file",
                                "file": "a.py",
                                "content": "print('hi')",
                                "reason": "add file",
                            }
                        ],
                    }
                ],
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
    assert patch["candidates"][0]["patch_ops"][0]["op"] == "create_file"
    assert len(transport.calls) == 2

    plan_call = transport.calls[0]
    patch_call = transport.calls[1]
    assert plan_call["schema_name"] == "plan_document"
    assert patch_call["schema_name"] == "patch_document_v2"
    assert plan_call["user_payload"]["retrieval_context"] == retrieval_context
    assert patch_call["user_payload"]["retrieval_context"] == retrieval_context
    assert patch_call["user_payload"]["diagnostics"][0]["source"] == "validator"
    assert plan_call["user_payload"]["constraints"]["max_files_touched"] == 20
    assert patch_call["user_payload"]["intent"]["execution_mode"] == "step_scoped_bounded_patching"
    assert "replace_node" in patch_call["user_payload"]["patch_op_catalog"]
    assert "deterministic planning engine" in plan_call["system_instructions"]
    assert "deterministic code patch generation engine" in patch_call["system_instructions"]


@pytest.mark.asyncio
async def test_reasoning_engine_rejects_schema_mismatch() -> None:
    transport = FakeTransport(json_outputs=[{"analysis": "incomplete"}])
    engine = DefaultReasoningEngine(model="gpt-5", transport=transport)
    task = TaskRecord(task_id="t1", goal="goal", workspace_path=".")

    with pytest.raises(ValidationError):
        await engine.create_plan(task, ".", retrieval_context={})


@pytest.mark.asyncio
async def test_create_tool_step_filters_type_enum_by_allowed_action_types() -> None:
    """When allowed_action_types is passed, the schema's outer `type` enum is
    restricted to that subset — closes the schema-bypass gap for emit_patch
    and verify_done that aren't covered by the inner tool-list filter."""
    transport = FakeTransport(
        json_outputs=[{"type": "tool_call", "thought": "t",
                        "tool": "read_file", "args": {"path": "a.py"}}],
    )
    engine = DefaultReasoningEngine(model="gpt-5", transport=transport)

    await engine.create_tool_step(
        step_context={"goal": "g", "targets": []},
        history=[],
        tool_definitions=[],
        allowed_action_types=frozenset({"tool_call", "revision_needed"}),
    )

    call = transport.calls[0]
    type_enum = call["schema"]["properties"]["type"]["enum"]
    assert type_enum == ["tool_call", "revision_needed"]
    assert "emit_patch" not in type_enum
    assert "verify_done" not in type_enum


@pytest.mark.asyncio
async def test_create_tool_step_unfiltered_when_allowed_action_types_none() -> None:
    """Default (None) keeps all four action types — legacy back-compat."""
    transport = FakeTransport(
        json_outputs=[{"type": "tool_call", "thought": "t",
                        "tool": "read_file", "args": {"path": "a.py"}}],
    )
    engine = DefaultReasoningEngine(model="gpt-5", transport=transport)

    await engine.create_tool_step(
        step_context={"goal": "g", "targets": []},
        history=[],
        tool_definitions=[],
    )

    type_enum = transport.calls[0]["schema"]["properties"]["type"]["enum"]
    assert set(type_enum) == {"tool_call", "emit_patch", "verify_done", "revision_needed"}


