"""Detached controller turn lifecycle: _active_turns register/clear, gate-clear at
turn start, the in-flight membership signal, and stop_turn. These mirror the
orchestrator's _running_tasks + /abort pattern (CLAUDE.md Tier B)."""
import asyncio

import pytest

from agentd.chat.controller import ChatController
from agentd.chat.models import PendingGate
from agentd.chat.storage import ChatThreadStore
from agentd.orchestrator.broadcaster import EventBroadcaster
from agentd.orchestrator.scripted_engine import ScriptedReasoningEngine


def _controller(tmp_path, store) -> ChatController:
    return ChatController(
        workspace_path=str(tmp_path),
        reasoning_engine=ScriptedReasoningEngine(None, []),
        thread_store=store, orchestrator=None,
        broadcaster=EventBroadcaster(), retrieval_client=None)


@pytest.mark.asyncio
async def test_launch_turn_registers_then_clears(tmp_path):
    store = ChatThreadStore(tmp_path / "chat.sqlite3")
    thread = store.create_thread(str(tmp_path))
    ctrl = _controller(tmp_path, store)

    started = asyncio.Event()
    release = asyncio.Event()

    async def _body():
        started.set()
        await release.wait()

    task = ctrl.launch_turn(thread.thread_id, _body())
    await started.wait()
    # Registered while in flight — this is the in-flight guard + turn_active signal.
    assert thread.thread_id in ctrl._active_turns
    release.set()
    await task
    # Cleared in the finally on completion.
    assert thread.thread_id not in ctrl._active_turns


@pytest.mark.asyncio
async def test_launch_turn_clears_on_exception(tmp_path):
    store = ChatThreadStore(tmp_path / "chat.sqlite3")
    thread = store.create_thread(str(tmp_path))
    ctrl = _controller(tmp_path, store)

    async def _body():
        raise RuntimeError("boom")

    task = ctrl.launch_turn(
        thread.thread_id, _body(), channel_id=f"chat:{thread.thread_id}")
    await task  # swallowed + logged, task completes
    assert thread.thread_id not in ctrl._active_turns


@pytest.mark.asyncio
async def test_handle_message_clears_stale_gate_at_start(tmp_path):
    store = ChatThreadStore(tmp_path / "chat.sqlite3")
    thread = store.create_thread(str(tmp_path))
    store.set_controller_gate(thread.thread_id, PendingGate(kind="mode", payload={}))
    ctrl = _controller(tmp_path, store)

    # Capture the gate state at the moment the turn body runs — proves the clear
    # happens at the START, independent of what the turn ultimately does. Stub the
    # loop so the test never depends on scripted-engine responses.
    gate_at_turn_start: list = []

    async def _fake_loop(thread_id, channel_id, goal, **kwargs):
        refreshed = store.get_thread(thread_id)
        gate_at_turn_start.append(refreshed.pending_controller_gate)
        from agentd.chat.controller_loop import ControllerOutcome
        return ControllerOutcome(kind="answer", text="ok")

    ctrl._run_loop = _fake_loop  # type: ignore[assignment]
    await ctrl.handle_message(
        thread.thread_id, "hello", channel_id=f"chat:{thread.thread_id}")

    assert gate_at_turn_start == [None]  # cleared before the loop ran


@pytest.mark.asyncio
async def test_stop_turn_cancels_and_breadcrumbs(tmp_path):
    store = ChatThreadStore(tmp_path / "chat.sqlite3")
    thread = store.create_thread(str(tmp_path))
    ctrl = _controller(tmp_path, store)
    channel_id = f"chat:{thread.thread_id}"

    release = asyncio.Event()

    async def _body():
        await release.wait()  # never released — stop_turn cancels it

    task = ctrl.launch_turn(thread.thread_id, _body(), channel_id=channel_id)
    await asyncio.sleep(0.02)
    assert thread.thread_id in ctrl._active_turns

    ok = await ctrl.stop_turn(thread.thread_id)
    assert ok is True
    assert task.cancelled()
    assert thread.thread_id not in ctrl._active_turns

    # Durable ✗ Stopped breadcrumb persisted.
    refreshed = store.get_thread(thread.thread_id)
    assert any(
        m.metadata.get("breadcrumb") and "Stopped" in m.content
        for m in refreshed.messages)
    # Idempotent: stopping an idle thread is a benign no-op.
    assert await ctrl.stop_turn(thread.thread_id) is False
