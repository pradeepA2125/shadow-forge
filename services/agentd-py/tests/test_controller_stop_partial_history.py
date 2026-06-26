"""Q2: a stopped turn must NOT lose its accumulated history.

`/stop` cancels the turn's asyncio.Task, raising CancelledError mid-loop — BEFORE
_run_loop's post-run history persistence. Without capturing it, everything the turn
did (its exploration AND any edits it already instant-promoted to the real workspace)
is dropped from controller_conversation_history, so the NEXT turn seeds from stale
pre-turn state and "forgets" what it just did. These tests pin the capture-on-cancel fix.
"""
import asyncio
from pathlib import Path

import pytest

from agentd.chat.controller import ChatController
from agentd.chat.controller_loop import ControllerLoop
from agentd.chat.controller_phase import ControllerPhaseSM
from agentd.chat.storage import ChatThreadStore
from agentd.orchestrator.broadcaster import EventBroadcaster
from agentd.tools.sources import AggregatingToolRegistry, BuiltinToolSource


class _CancelMidTurn:
    """Scripted controller engine that raises CancelledError on the Nth step, simulating
    /stop arriving during a model call (the asyncio.Task cancel)."""

    def __init__(self, responses, cancel_at):
        self._responses = list(responses)
        self._i = 0
        self._cancel_at = cancel_at

    async def create_controller_step(self, **kwargs):
        if self._i >= self._cancel_at:
            raise asyncio.CancelledError()
        resp = self._responses[self._i]
        self._i += 1
        return resp


@pytest.mark.asyncio
async def test_controller_loop_exposes_partial_history_on_cancel(tmp_path: Path):
    """The loop seam: after a cancel mid-turn, partial_history() returns the verbatim
    conversation accumulated so far (the read_file it ran before the stop)."""
    real = tmp_path / "ws"
    real.mkdir()
    (real / "f.py").write_text("x = 1\n")
    reg = AggregatingToolRegistry(
        [BuiltinToolSource(shadow_root=real, real_workspace_path=real)])
    eng = _CancelMidTurn([
        {"type": "tool_call", "thought": "l", "tool": "read_file",
         "args": {"path": "f.py"}},
    ], cancel_at=1)
    loop = ControllerLoop(
        eng, reg, EventBroadcaster(), channel_id="c", phase_sm=ControllerPhaseSM())
    with pytest.raises(asyncio.CancelledError):
        await loop.run({"goal": "g", "workspace_path": str(real)}, max_iters=5)
    hist = loop.partial_history()
    assert any("read_file" in str(h.get("content", "")) for h in hist), hist


@pytest.mark.asyncio
async def test_stopped_turn_persists_partial_history(tmp_path: Path):
    """End-to-end: a cancelled turn's history is persisted to the store so the next turn
    rehydrates it (the same path an edit's 'applied+promoted' result rides)."""
    (tmp_path / "f.py").write_text("x = 1\n")
    store = ChatThreadStore(tmp_path / "chat.sqlite3")
    thread = store.create_thread(str(tmp_path), title="t")
    ctrl = ChatController(
        workspace_path=str(tmp_path),
        reasoning_engine=_CancelMidTurn([
            {"type": "tool_call", "thought": "look", "tool": "read_file",
             "args": {"path": "f.py"}},
        ], cancel_at=1),
        thread_store=store, orchestrator=None, broadcaster=EventBroadcaster(),
        retrieval_client=None)

    with pytest.raises(asyncio.CancelledError):
        await ctrl.handle_message(thread.thread_id, "what is x", channel_id="c1")

    reloaded = store.get_thread(thread.thread_id)
    assert reloaded is not None
    hist = reloaded.controller_conversation_history
    assert hist, "partial history not persisted on cancel"
    assert any("read_file" in str(h.get("content", ""))
               or "x = 1" in str(h.get("content", "")) for h in hist), hist
