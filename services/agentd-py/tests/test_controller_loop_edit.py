from pathlib import Path

import pytest

from agentd.chat.controller_loop import ControllerLoop
from agentd.chat.controller_phase import ControllerPhaseSM
from agentd.chat.edit_session import TurnEditSession
from agentd.orchestrator.broadcaster import EventBroadcaster
from agentd.orchestrator.scripted_engine import ScriptedReasoningEngine
from agentd.patch.engine import PatchEngine
from agentd.tools.sources import AggregatingToolRegistry, BuiltinToolSource
from agentd.workspace.shadow import ShadowWorkspaceManager


@pytest.mark.asyncio
async def test_edit_phase_promotes_then_submits(tmp_path: Path):
    real = tmp_path / "ws"
    real.mkdir()
    (real / "f.py").write_text("x = 1\n")
    sm = ControllerPhaseSM()
    sm.enter_edit_mode()  # simulate user picked edit
    sess = TurnEditSession(
        turn_id="t1", real_path=real,
        workspace_manager=ShadowWorkspaceManager(tmp_path / "sh"),
        patch_engine=PatchEngine())
    reg = AggregatingToolRegistry(
        [BuiltinToolSource(shadow_root=real, real_workspace_path=real)])
    steps = [
        {"type": "edit", "thought": "t", "patch_ops": [
            {"op": "search_replace", "file": "f.py",
             "search": "x = 1", "replace": "x = 2", "reason": "r"}]},
        {"type": "submit_changes", "thought": "done", "summary": "bumped x"},
    ]
    loop = ControllerLoop(
        ScriptedReasoningEngine(None, [], controller_step_responses=steps),
        reg, EventBroadcaster(), channel_id="c", phase_sm=sm, edit_session=sess)
    out = await loop.run(
        {"goal": "bump x", "workspace_path": str(real)}, max_iters=6,
        auto_accept_edits=True)
    assert out.kind == "submit_changes"
    assert (real / "f.py").read_text() == "x = 2\n"  # instant-promoted
