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
async def test_reject_leaves_real_untouched_then_accept_promotes(tmp_path: Path):
    real = tmp_path / "ws"
    real.mkdir()
    (real / "f.py").write_text("x = 1\n")
    sm = ControllerPhaseSM()
    sm.enter_edit_mode()
    sess = TurnEditSession(
        turn_id="t1", real_path=real,
        workspace_manager=ShadowWorkspaceManager(tmp_path / "sh"), patch_engine=PatchEngine())
    decisions = iter([{"decision": "reject", "reason": "wrong var"}, {"decision": "accept"}])

    async def edit_cb(diff):
        return next(decisions)

    reg = AggregatingToolRegistry(
        [BuiltinToolSource(shadow_root=real, real_workspace_path=real)])
    loop = ControllerLoop(ScriptedReasoningEngine(None, [], controller_step_responses=[
        {"type": "edit", "thought": "t", "patch_ops": [
            {"op": "search_replace", "file": "f.py",
             "search": "x = 1", "replace": "x = 9", "reason": "r"}]},
        {"type": "edit", "thought": "fix", "patch_ops": [
            {"op": "search_replace", "file": "f.py",
             "search": "x = 1", "replace": "x = 2", "reason": "r"}]},
        {"type": "submit_changes", "thought": "done", "summary": "s"},
    ]), reg, EventBroadcaster(), channel_id="c", phase_sm=sm, edit_session=sess)
    out = await loop.run(
        {"goal": "g", "workspace_path": str(real)}, max_iters=8,
        auto_accept_edits=False, edit_decision_cb=edit_cb)
    assert out.kind == "submit_changes"
    # first rejected (real untouched, so the second sees the original "x = 1"), second accepted
    assert (real / "f.py").read_text() == "x = 2\n"


@pytest.mark.asyncio
async def test_reject_reason_feeds_history(tmp_path: Path):
    real = tmp_path / "ws"
    real.mkdir()
    (real / "f.py").write_text("x = 1\n")
    sm = ControllerPhaseSM()
    sm.enter_edit_mode()
    sess = TurnEditSession(
        turn_id="t2", real_path=real,
        workspace_manager=ShadowWorkspaceManager(tmp_path / "sh"), patch_engine=PatchEngine())

    async def edit_cb(diff):
        return {"decision": "reject", "reason": "use a constant"}

    reg = AggregatingToolRegistry(
        [BuiltinToolSource(shadow_root=real, real_workspace_path=real)])
    loop = ControllerLoop(ScriptedReasoningEngine(None, [], controller_step_responses=[
        {"type": "edit", "thought": "t", "patch_ops": [
            {"op": "search_replace", "file": "f.py",
             "search": "x = 1", "replace": "x = 9", "reason": "r"}]},
        {"type": "submit_changes", "thought": "giving up", "summary": "s"},
    ]), reg, EventBroadcaster(), channel_id="c", phase_sm=sm, edit_session=sess)
    out = await loop.run(
        {"goal": "g", "workspace_path": str(real)}, max_iters=8,
        auto_accept_edits=False, edit_decision_cb=edit_cb)
    assert out.kind == "submit_changes"
    assert (real / "f.py").read_text() == "x = 1\n"  # rejected → real untouched
    assert any("use a constant" in str(h.get("content", "")) for h in (out.history or []))


@pytest.mark.asyncio
async def test_bad_patch_feeds_back_instead_of_crashing(tmp_path: Path):
    """A wrong search string must not crash the turn — the error is fed back so the
    agent can re-emit, and a subsequent good edit still promotes."""
    real = tmp_path / "ws"
    real.mkdir()
    (real / "f.py").write_text("x = 1\n")
    sm = ControllerPhaseSM()
    sm.enter_edit_mode()
    sess = TurnEditSession(
        turn_id="t3", real_path=real,
        workspace_manager=ShadowWorkspaceManager(tmp_path / "sh"), patch_engine=PatchEngine())
    reg = AggregatingToolRegistry(
        [BuiltinToolSource(shadow_root=real, real_workspace_path=real)])
    loop = ControllerLoop(ScriptedReasoningEngine(None, [], controller_step_responses=[
        {"type": "edit", "thought": "t", "patch_ops": [
            {"op": "search_replace", "file": "f.py",
             "search": "NONEXISTENT", "replace": "y", "reason": "r"}]},  # bad search → raises
        {"type": "edit", "thought": "fix", "patch_ops": [
            {"op": "search_replace", "file": "f.py",
             "search": "x = 1", "replace": "x = 2", "reason": "r"}]},
        {"type": "submit_changes", "thought": "done", "summary": "s"},
    ]), reg, EventBroadcaster(), channel_id="c", phase_sm=sm, edit_session=sess)
    out = await loop.run(
        {"goal": "g", "workspace_path": str(real)}, max_iters=8, auto_accept_edits=True)
    assert out.kind == "submit_changes"
    assert (real / "f.py").read_text() == "x = 2\n"  # recovered + promoted
    assert any("PATCH FAILED" in str(h.get("content", "")) for h in (out.history or []))
