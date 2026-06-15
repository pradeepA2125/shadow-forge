from pathlib import Path

import pytest

from agentd.chat.controller import ChatController
from agentd.chat.models import PendingGate
from agentd.chat.storage import ChatThreadStore
from agentd.orchestrator.broadcaster import EventBroadcaster
from agentd.orchestrator.scripted_engine import ScriptedReasoningEngine


class _Orch:
    def __init__(self):
        self.created = None

    async def create_task_from_chat(self, **kw):
        self.created = kw
        return "task-xyz"

    async def await_plan_ready(self, tid, timeout_sec=3600.0):
        return None


def _controller(tmp_path, store, eng, orch):
    return ChatController(
        workspace_path=str(tmp_path), reasoning_engine=eng, thread_store=store,
        orchestrator=orch, broadcaster=EventBroadcaster(), retrieval_client=None)


@pytest.mark.asyncio
async def test_propose_mode_emits_card_and_stores_history(tmp_path: Path):
    store = ChatThreadStore(tmp_path / "c.sqlite3")
    th = store.create_thread(str(tmp_path), title="t")
    eng = ScriptedReasoningEngine(None, [], controller_step_responses=[
        {"type": "propose_mode", "thought": "t", "plan_sketch": "add decorator",
         "recommended": "create_task", "reason": "big", "options": [{"mode": "create_task"}]}])
    ctrl = _controller(tmp_path, store, eng, _Orch())
    await ctrl.handle_message(th.thread_id, "do a big thing", channel_id=f"chat:{th.thread_id}")
    # Class-A: a durable thread gate is set (rendered by /live), NOT an SSE mode event.
    gate = store.get_thread(th.thread_id).pending_controller_gate
    assert gate is not None and gate.kind == "mode"
    assert gate.payload["plan_sketch"] == "add decorator"
    assert ctrl._histories[th.thread_id]  # history stored for resume/discuss


@pytest.mark.asyncio
async def test_mode_decision_create_task_dispatches(tmp_path: Path):
    store = ChatThreadStore(tmp_path / "c.sqlite3")
    th = store.create_thread(str(tmp_path), title="t")
    orch = _Orch()
    ctrl = _controller(tmp_path, store, ScriptedReasoningEngine(None, []), orch)
    ctrl._histories[th.thread_id] = [{"role": "assistant", "content": "{}"}]
    store.set_controller_gate(th.thread_id, PendingGate(kind="mode", payload={}))
    await ctrl.resolve_mode(
        th.thread_id, "create_task", channel_id=f"chat:{th.thread_id}", goal="g")
    assert orch.created is not None and orch.created["goal"] == "g"
    # The mode gate is cleared on resolution (Class-A: gates clear in place).
    assert store.get_thread(th.thread_id).pending_controller_gate is None


@pytest.mark.asyncio
async def test_mode_decision_double_dispatch_guarded(tmp_path: Path):
    """A second /mode-decision after the gate is resolved must NOT re-dispatch
    (no double task creation)."""
    store = ChatThreadStore(tmp_path / "c.sqlite3")
    th = store.create_thread(str(tmp_path), title="t")
    orch = _Orch()
    ctrl = _controller(tmp_path, store, ScriptedReasoningEngine(None, []), orch)
    store.set_controller_gate(th.thread_id, PendingGate(kind="mode", payload={}))
    await ctrl.resolve_mode(
        th.thread_id, "create_task", channel_id=f"chat:{th.thread_id}", goal="g")
    first = orch.created
    orch.created = None
    # Second resolve with the gate already cleared → no-op.
    await ctrl.resolve_mode(
        th.thread_id, "create_task", channel_id=f"chat:{th.thread_id}", goal="g")
    assert first is not None and orch.created is None


@pytest.mark.asyncio
async def test_mode_decision_explain_reenters_loop_with_answer(tmp_path: Path):
    store = ChatThreadStore(tmp_path / "c.sqlite3")
    th = store.create_thread(str(tmp_path), title="t")
    eng = ScriptedReasoningEngine(None, [], controller_step_responses=[
        {"type": "answer", "thought": "t", "answer": "here is what would happen"}])
    ctrl = _controller(tmp_path, store, eng, _Orch())
    ctrl._histories[th.thread_id] = [{"role": "assistant", "content": "{}"}]
    store.set_controller_gate(th.thread_id, PendingGate(kind="mode", payload={}))
    await ctrl.resolve_mode(
        th.thread_id, "explain", channel_id=f"chat:{th.thread_id}", goal="g")
    msgs = store.get_thread(th.thread_id).messages
    assert any(m.role == "agent" and "would happen" in m.content for m in msgs)
