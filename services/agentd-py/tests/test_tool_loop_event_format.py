from __future__ import annotations
import pytest
from agentd.orchestrator.broadcaster import EventBroadcaster
from agentd.domain.models import PlanStep, TaskBudget, TaskUsage


class _ToolCallEngine:
    """Scripted engine: one tool_call then emit_patch."""
    async def create_tool_step(self, step_context, history, tool_definitions, on_thinking=None, state_description="", allowed_action_types=None):
        if not history:
            return {"type": "tool_call", "thought": "t", "tool": "read_file", "args": {"path": "a.py"}}
        if len(history) == 2:  # after tool_result
            return {
                "type": "emit_patch",
                "thought": "p",
                "patch_ops": [{"op": "search_replace", "file": "a.py", "search": "x", "replace": "y", "reason": "r"}],
            }
        return {"type": "verify_done", "thought": "v", "verified": True, "test_output": ""}

    async def create_planning_step(self, *args, **kwargs):
        return {}

    async def create_plan(self, *args, **kwargs):
        return {}


@pytest.mark.asyncio
async def test_tool_call_event_uses_payload_envelope(tmp_path):
    from agentd.tools.loop import ToolLoop, build_tool_registry
    from agentd.patch.engine import PatchEngine
    from agentd.tools.registry import ToolRegistry

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("x = 1\n")

    broadcaster = EventBroadcaster()
    queue = broadcaster.subscribe("task-t1")

    registry = ToolRegistry(shadow_root=ws, real_workspace_path=ws)
    loop = ToolLoop(
        _ToolCallEngine(), registry, broadcaster, "task-t1",
        patch_engine=PatchEngine(), shadow_path=ws,
    )
    step = PlanStep(
        id="S1", goal="g",
        targets=[{"path": "a.py", "intent": "existing"}],
        risk="low",
    )
    await loop.run(step, {}, TaskBudget(), TaskUsage())

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())

    tool_call_events = [e for e in events if e["type"] == "tool_call"]
    assert tool_call_events, "expected at least one tool_call event"
    evt = tool_call_events[0]
    assert "payload" in evt, f"event missing 'payload' key: {evt}"
    assert "tool" in evt["payload"], f"payload missing 'tool': {evt['payload']}"
