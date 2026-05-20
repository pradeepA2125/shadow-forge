from __future__ import annotations
import pytest
from agentd.orchestrator.broadcaster import EventBroadcaster
from agentd.domain.models import PlanStep, PlanTarget, PlanTargetIntent, TaskBudget, TaskUsage
from agentd.tools.loop import VerifyResult


class _EmitPatchEngine:
    """Scripted engine: emits patch immediately, then verify_done if asked."""
    async def create_tool_step(self, step_context, history, tool_definitions, on_thinking=None, state_description="", allowed_action_types=None):
        if not history:
            return {
                "type": "emit_patch",
                "thought": "patch",
                "patch_ops": [{"op": "search_replace", "file": "a.py", "search": "x", "replace": "y", "reason": "r"}],
            }
        return {"type": "verify_done", "thought": "done", "verified": True, "test_output": ""}

    async def create_patch(self, *args, **kwargs):
        return {}

    async def create_planning_step(self, *args, **kwargs):
        return {}

    async def create_plan(self, *args, **kwargs):
        return {}


@pytest.mark.asyncio
async def test_skip_verify_returns_immediately_after_patch_applied(tmp_path):
    from agentd.tools.loop import ToolLoop
    from agentd.patch.engine import PatchEngine
    from agentd.tools.registry import ToolRegistry

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("x = 1\n")

    broadcaster = EventBroadcaster()
    queue = broadcaster.subscribe("task-t2")

    registry = ToolRegistry(shadow_root=ws, real_workspace_path=ws)
    loop = ToolLoop(
        _EmitPatchEngine(), registry, broadcaster, "task-t2",
        patch_engine=PatchEngine(), shadow_path=ws,
        skip_verify=True,
    )
    step = PlanStep(
        id="S1", goal="g",
        targets=[PlanTarget(path="a.py", intent=PlanTargetIntent.EXISTING)],
        risk="low",
    )
    result = await loop.run(step, {}, TaskBudget(), TaskUsage())

    assert isinstance(result, VerifyResult), f"expected VerifyResult, got {type(result)}"
    assert result.verified is True

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())

    event_types = [e["type"] for e in events]
    assert "patch_applied" in event_types
    # verify phase tool calls must NOT be present — skip_verify short-circuits before that
    assert "tool_call" not in [e["type"] for e in events if e.get("payload", {}).get("phase") == "verify"]


@pytest.mark.asyncio
async def test_broadcast_key_routes_to_custom_channel(tmp_path):
    from agentd.tools.loop import ToolLoop
    from agentd.patch.engine import PatchEngine
    from agentd.tools.registry import ToolRegistry

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("x = 1\n")

    broadcaster = EventBroadcaster()
    task_queue = broadcaster.subscribe("task-t3")
    chat_queue = broadcaster.subscribe("chat-c1")

    registry = ToolRegistry(shadow_root=ws, real_workspace_path=ws)
    loop = ToolLoop(
        _EmitPatchEngine(), registry, broadcaster, "task-t3",
        patch_engine=PatchEngine(), shadow_path=ws,
        broadcast_key="chat-c1",
        skip_verify=True,
    )
    step = PlanStep(
        id="S1", goal="g",
        targets=[PlanTarget(path="a.py", intent=PlanTargetIntent.EXISTING)],
        risk="low",
    )
    await loop.run(step, {}, TaskBudget(), TaskUsage())

    # Events must land in chat channel, not task channel
    assert task_queue.empty(), "no events should go to the task channel when broadcast_key is set"
    assert not chat_queue.empty(), "events must go to the custom broadcast_key channel"
