"""Tests for the four new SSE emission improvements.

1. planning_tool_call events carry `args`
2. Chat explore turn emits explore_tool_call followed by explore_tool_result
3. cap_event_output: ≤2000 unchanged; >2000 → truncated with suffix
4. step_started fires once per executed step with correct step_index/total_steps
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agentd.chat.agent import ChatAgent
from agentd.chat.storage import ChatThreadStore
from agentd.domain.models import (
    Diagnostic,
    TaskRecord,
    TaskStatus,
    ValidationResult,
)
from agentd.orchestrator.broadcaster import EventBroadcaster, cap_event_output
from agentd.orchestrator.engine import AgentOrchestrator
from agentd.patch.engine import PatchEngine
from agentd.planning.loop import PlanningLoop
from agentd.planning.registry import PlanningToolRegistry
from agentd.retrieval.artifact_client import RetrievalContext
from agentd.storage.in_memory import InMemoryTaskStore
from agentd.workspace.shadow import ShadowWorkspaceManager


# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------


def _drain(queue) -> list[dict]:
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


class _AlwaysPassValidator:
    async def run(self, workspace_path: str) -> ValidationResult:
        return ValidationResult(success=True, diagnostics=[], duration_ms=1)

    async def run_touched(self, workspace_path: str, touched_files: list) -> ValidationResult:
        return ValidationResult(success=True, diagnostics=[], duration_ms=1)


class _NullRetrievalClient:
    def load_context(
        self, workspace_path: str, goal: str
    ) -> tuple[RetrievalContext, list[Diagnostic]]:
        return (
            RetrievalContext(
                related_files=[],
                related_symbols=[],
                graph_neighbors=[],
                diagnostics_excerpt=[],
                snapshot_age_sec=0.0,
                snapshot_stats={"node_count": 0, "edge_count": 0, "diagnostic_count": 0},
                planner_evidence={"workspace_files_index": []},
            ),
            [],
        )


# ---------------------------------------------------------------------------
# Test 1 — planning_tool_call carries `args`
# ---------------------------------------------------------------------------


class _ScriptedPlanningEngine:
    """Emits one tool_call with known args, then emit_plan."""

    def __init__(self) -> None:
        self._calls = 0

    async def create_planning_step(
        self,
        plan_context: dict,
        history: list,
        tool_definitions: list,
        on_thinking=None,
        state_description: str = "",
        allowed_action_types=None,
    ) -> dict:
        self._calls += 1
        if self._calls == 1:
            return {
                "type": "tool_call",
                "thought": "Need to look at the directory",
                "tool": "list_directory",
                "args": {"path": "src/", "depth": 2},
            }
        return {
            "type": "emit_plan",
            "thought": "Done",
            "plan_markdown": "# Plan\n- no-op",
            "files_examined": [],
            "confidence": "high",
        }

    async def create_plan(self, *a, **k):
        return {}

    async def create_patch(self, *a, **k):
        return {}

    async def create_tool_step(self, *a, **k):
        return {"type": "emit_patch", "thought": "", "patch_ops": []}


@pytest.mark.asyncio
async def test_planning_tool_call_carries_args(tmp_path: Path) -> None:
    broadcaster = EventBroadcaster()
    queue = broadcaster.subscribe("t-plan")

    loop = PlanningLoop(
        reasoning_engine=_ScriptedPlanningEngine(),
        registry=PlanningToolRegistry(real_path=tmp_path),
        broadcaster=broadcaster,
        task_id="t-plan",
    )
    from agentd.domain.models import TaskBudget
    await loop.run({"goal": "explore", "workspace_path": str(tmp_path)}, TaskBudget())

    events = _drain(queue)
    tool_call_events = [e for e in events if e["type"] == "planning_tool_call"]
    assert len(tool_call_events) >= 1, "Expected at least one planning_tool_call event"

    evt = tool_call_events[0]
    payload = evt["payload"]
    assert "args" in payload, "planning_tool_call must carry 'args'"
    assert payload["tool"] == "list_directory"
    assert payload["args"] == {"path": "src/", "depth": 2}


# ---------------------------------------------------------------------------
# Test 2 — Chat explore emits explore_tool_result after explore_tool_call
# ---------------------------------------------------------------------------


class _OneToolExploreTransport:
    """Returns a single tool_call on first explore step, then done; qa for classifier."""

    def __init__(self) -> None:
        self._explore_calls = 0

    async def generate_text(self, **_) -> str:
        return "Some answer."

    async def generate_json(
        self, *, model, schema_name, schema, system_instructions, user_payload, on_thinking=None
    ) -> dict:
        if schema_name == "explore_step":
            self._explore_calls += 1
            if self._explore_calls == 1:
                return {
                    "thought": "Let me look at the files",
                    "action": "tool_call",
                    "tool": "list_directory",
                    "args": {"path": str(user_payload.get("workspace_path", "."))},
                }
            return {"action": "done"}
        return {"intent": "qa", "rationale": "scripted", "likely_targets": []}


@pytest.mark.asyncio
async def test_explore_emits_tool_result_after_tool_call(tmp_path: Path) -> None:
    store = ChatThreadStore(tmp_path / "chat.db")
    broadcaster = EventBroadcaster()
    queue = broadcaster.subscribe("ch-explore")

    agent = ChatAgent(
        workspace_path=str(tmp_path),
        transport=_OneToolExploreTransport(),
        model="test-model",
        thread_store=store,
        orchestrator=None,
        broadcaster=broadcaster,
    )
    thread = store.create_thread(str(tmp_path))
    await agent.handle_message(thread.thread_id, "What files are here?", channel_id="ch-explore")

    events = _drain(queue)
    types = [e["type"] for e in events]

    # explore_tool_call must appear
    assert "explore_tool_call" in types, f"explore_tool_call missing from {types}"
    # explore_tool_result must appear
    assert "explore_tool_result" in types, f"explore_tool_result missing from {types}"

    # explore_tool_call must precede explore_tool_result
    call_idx = next(i for i, e in enumerate(events) if e["type"] == "explore_tool_call")
    result_idx = next(i for i, e in enumerate(events) if e["type"] == "explore_tool_result")
    assert call_idx < result_idx, "explore_tool_call must precede explore_tool_result"

    # tool names must match
    call_tool = events[call_idx]["payload"]["tool"]
    result_tool = events[result_idx]["payload"]["tool"]
    assert call_tool == result_tool, f"tool names must match: {call_tool!r} vs {result_tool!r}"

    # result must report is_error=False on a successful call
    assert events[result_idx]["payload"]["is_error"] is False


# ---------------------------------------------------------------------------
# Test 3 — cap_event_output boundary behaviour
# ---------------------------------------------------------------------------


def test_cap_event_output_short_text_unchanged() -> None:
    text = "hello world"
    assert cap_event_output(text) == text


def test_cap_event_output_exactly_limit_unchanged() -> None:
    text = "x" * 2000
    assert cap_event_output(text) == text


def test_cap_event_output_over_limit_truncated() -> None:
    text = "y" * 2500
    result = cap_event_output(text)
    assert result.endswith("\n… truncated")
    # The kept prefix is exactly `limit` chars
    prefix = result[: -len("\n… truncated")]
    assert len(prefix) == 2000
    assert prefix == "y" * 2000


def test_cap_event_output_custom_limit() -> None:
    text = "z" * 100
    result = cap_event_output(text, limit=50)
    assert result.endswith("\n… truncated")
    assert len(result[: -len("\n… truncated")]) == 50


# ---------------------------------------------------------------------------
# Test 4 — step_started fires with correct step_index / total_steps
# ---------------------------------------------------------------------------


class _TwoStepReasoningEngine:
    """Scripted engine that produces a two-step plan and patches both."""

    PLAN = {
        "analysis": "two steps",
        "steps": [
            {
                "id": "s1",
                "goal": "Create file one",
                "targets": [{"path": "one.txt", "intent": "new"}],
                "risk": "low",
            },
            {
                "id": "s2",
                "goal": "Create file two",
                "targets": [{"path": "two.txt", "intent": "new"}],
                "risk": "low",
            },
        ],
        "expected_files": ["one.txt", "two.txt"],
        "stop_conditions": ["done"],
    }

    # Each step needs: emit_patch on first call, verify_done once patch is applied.
    # We track a global call counter and alternate: odd calls emit_patch, even calls verify_done.
    def __init__(self) -> None:
        self._call_n = 0

    async def create_planning_step(
        self,
        plan_context: dict,
        history: list,
        tool_definitions: list,
        on_thinking=None,
        state_description: str = "",
        allowed_action_types=None,
    ) -> dict:
        return {
            "type": "emit_plan",
            "thought": "stub",
            "plan_markdown": "# Plan\n- s1\n- s2",
            "files_examined": [],
            "confidence": "high",
        }

    async def create_plan(self, *a, **k):
        return self.PLAN

    async def create_patch(self, *a, **k):
        return {"candidates": [{"candidate_id": "c0", "patch_ops": []}]}

    async def create_tool_step(
        self,
        step_context: dict,
        history: list,
        tool_definitions: list,
        on_thinking=None,
        state_description: str = "",
        allowed_action_types=None,
    ) -> dict:
        # Detect verify phase by presence of "Patch applied successfully" in history
        in_verify = any(
            isinstance(msg.get("content"), str) and "Patch applied successfully" in msg["content"]
            for msg in history
        )
        if in_verify:
            return {"type": "verify_done", "thought": "scripted", "verified": True, "test_output": ""}
        # Which step? Infer from goal in step_context.
        goal = str(step_context.get("goal", ""))
        filename = "one.txt" if "one" in goal else "two.txt"
        return {
            "type": "emit_patch",
            "thought": "create file",
            "patch_ops": [{"op": "create_file", "file": filename, "content": "ok\n", "reason": "scripted"}],
        }


@pytest.mark.asyncio
async def test_step_started_fires_with_correct_index(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "README.md").write_text("hello\n")

    store = InMemoryTaskStore()
    task = TaskRecord(
        task_id="task-step-idx",
        goal="Two step task",
        workspace_path=str(ws),
    )
    await store.create(task)

    orchestrator = AgentOrchestrator(
        store=store,
        reasoning_engine=_TwoStepReasoningEngine(),
        validator=_AlwaysPassValidator(),
        patch_engine=PatchEngine(),
        workspace_manager=ShadowWorkspaceManager(root_path=tmp_path / "shadows"),
        retrieval_client=_NullRetrievalClient(),
    )

    # Subscribe to the task channel before execution starts
    queue = orchestrator.broadcaster.subscribe("task-step-idx")

    initialized = await orchestrator.run_task("task-step-idx")
    assert initialized.status == TaskStatus.AWAITING_PLAN_APPROVAL

    result = await orchestrator.continue_task("task-step-idx", feedback=None)
    assert result.status == TaskStatus.READY_FOR_REVIEW

    events = _drain(queue)
    step_started_events = [e for e in events if e["type"] == "step_started"]

    # Two steps → two step_started events
    assert len(step_started_events) == 2, (
        f"Expected 2 step_started events, got {len(step_started_events)}: "
        f"{[e['payload'] for e in step_started_events]}"
    )

    payloads = [e["payload"] for e in step_started_events]
    assert payloads[0]["step_id"] == "s1"
    assert payloads[0]["step_index"] == 1
    assert payloads[0]["total_steps"] == 2

    assert payloads[1]["step_id"] == "s2"
    assert payloads[1]["step_index"] == 2
    assert payloads[1]["total_steps"] == 2
