from pathlib import Path

import pytest

from agentd.chat.controller_loop import ControllerLoop
from agentd.chat.controller_phase import ControllerPhaseSM
from agentd.orchestrator.broadcaster import EventBroadcaster
from agentd.orchestrator.scripted_engine import ScriptedReasoningEngine
from agentd.tools.sources import AggregatingToolRegistry, BuiltinToolSource


def _loop(tmp_path, steps):
    reg = AggregatingToolRegistry(
        [BuiltinToolSource(shadow_root=tmp_path, real_workspace_path=tmp_path)]
    )
    return ControllerLoop(
        ScriptedReasoningEngine(None, [], controller_step_responses=steps), reg,
        EventBroadcaster(), channel_id="c", phase_sm=ControllerPhaseSM())


@pytest.mark.asyncio
async def test_clarify_terminal(tmp_path: Path):
    steps = [{"type": "clarify", "thought": "t", "question": "which file?"}]
    out = await _loop(tmp_path, steps).run(
        {"goal": "g", "workspace_path": str(tmp_path)}, max_iters=4)
    assert out.kind == "clarify" and out.text == "which file?"


@pytest.mark.asyncio
async def test_propose_mode_terminal_carries_payload(tmp_path: Path):
    steps = [{"type": "propose_mode", "thought": "t", "recommended": "create_task",
              "plan_sketch": "add a decorator and apply to 3 routes",
              "reason": "big", "options": [{"mode": "create_task"}]}]
    out = await _loop(tmp_path, steps).run(
        {"goal": "g", "workspace_path": str(tmp_path)}, max_iters=4)
    assert out.kind == "propose_mode" and out.payload["recommended"] == "create_task"
    assert out.payload["plan_sketch"] == "add a decorator and apply to 3 routes"
