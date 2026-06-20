"""Controller edit-turn durability + live/reload parity (smoke-found gaps #1-#4).

The earlier controller dropped the durable half of every surface: edits persisted
no diff_card, submit_changes dropped the turn's pills, the model's thinking never
streamed, and the "Review each edit" toggle was ignored. These tests lock in the
agent.py-parity behavior: broadcast live AND persist for reload.
"""
import json
from pathlib import Path

import pytest

from agentd.chat.controller import ChatController
from agentd.chat.controller_loop import ControllerLoop, ControllerOutcome
from agentd.chat.controller_phase import ControllerPhaseSM
from agentd.chat.edit_session import TurnEditSession
from agentd.chat.models import PendingGate
from agentd.chat.storage import ChatThreadStore
from agentd.domain.models import DiffEntry
from agentd.orchestrator.broadcaster import EventBroadcaster
from agentd.orchestrator.scripted_engine import ScriptedReasoningEngine
from agentd.patch.engine import PatchEngine
from agentd.tools.sources import AggregatingToolRegistry, BuiltinToolSource
from agentd.workspace.shadow import ShadowWorkspaceManager


def _drain(queue) -> list[dict]:
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


def _controller(tmp_path, store, orchestrator=None) -> ChatController:
    return ChatController(
        workspace_path=str(tmp_path),
        reasoning_engine=ScriptedReasoningEngine(None, []),
        thread_store=store, orchestrator=orchestrator,
        broadcaster=EventBroadcaster(), retrieval_client=None)


def _diff(path="f.py") -> list[DiffEntry]:
    return [DiffEntry(path=path, additions=1, deletions=0,
                      temp_path="/shadow/" + path, unified_diff="@@\n+x = 2")]


# ── Loop: edit_record_cb is the single transcript writer ───────────────────────

@pytest.mark.asyncio
async def test_loop_invokes_edit_record_cb_on_each_edit(tmp_path: Path):
    real = tmp_path / "ws"
    real.mkdir()
    (real / "f.py").write_text("x = 1\n")
    sm = ControllerPhaseSM()
    sm.enter_edit_mode()
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
    recorded: list[tuple[str, str, list[str]]] = []

    async def rec(diff, decision, reason):
        recorded.append((decision, reason, [d.path for d in diff]))

    bc = EventBroadcaster()
    q = bc.subscribe("c")
    loop = ControllerLoop(
        ScriptedReasoningEngine(None, [], controller_step_responses=steps),
        reg, bc, channel_id="c", phase_sm=sm, edit_session=sess)
    await loop.run(
        {"goal": "bump x", "workspace_path": str(real)}, max_iters=6,
        auto_accept_edits=True, edit_record_cb=rec)

    # The record cb fired exactly once, for the accepted edit.
    assert recorded == [("accept", "", ["f.py"])]
    # The loop itself no longer broadcasts diff_ready — the cb owns transcript render.
    assert "diff_ready" not in {e["type"] for e in _drain(q)}


@pytest.mark.asyncio
async def test_loop_streams_thinking_via_on_thinking(tmp_path: Path):
    class _ThinkingEngine:
        async def create_controller_step(
            self, *, plan_context, history, tool_definitions, phase, on_thinking=None,
        ):
            if on_thinking:
                on_thinking("weighing options")
            return {"type": "answer", "thought": "t", "answer": "hi"}

    reg = AggregatingToolRegistry(
        [BuiltinToolSource(shadow_root=tmp_path, real_workspace_path=tmp_path)])
    bc = EventBroadcaster()
    q = bc.subscribe("c1")
    loop = ControllerLoop(
        _ThinkingEngine(), reg, bc, channel_id="c1", phase_sm=ControllerPhaseSM())
    await loop.run({"goal": "q", "workspace_path": str(tmp_path)}, max_iters=4)

    chunks = [e["payload"]["chunk"] for e in _drain(q) if e["type"] == "tool_thinking_chunk"]
    assert "weighing options" in chunks


# ── Controller: durable diff_card + breadcrumb / live render ───────────────────

@pytest.mark.asyncio
async def test_edit_record_auto_accept_persists_inert_card_and_renders_live(tmp_path: Path):
    store = ChatThreadStore(tmp_path / "c.sqlite3")
    th = store.create_thread(str(tmp_path), title="t")
    bc = EventBroadcaster()
    q = bc.subscribe(f"chat:{th.thread_id}")
    ctrl = ChatController(
        workspace_path=str(tmp_path),
        reasoning_engine=ScriptedReasoningEngine(None, []),
        thread_store=store, orchestrator=None, broadcaster=bc, retrieval_client=None)

    await ctrl._edit_record_cb(
        th.thread_id, f"chat:{th.thread_id}", False, _diff(), "accept", "")

    cards = [m for m in store.get_thread(th.thread_id).messages if m.type == "diff_card"]
    assert len(cards) == 1
    assert cards[0].metadata["resolved"] == "applied"
    assert cards[0].metadata["diff_entries"][0]["path"] == "f.py"
    # temp_path is NOT persisted (instant-promote makes a native diff meaningless).
    assert "temp_path" not in cards[0].metadata["diff_entries"][0]
    # Auto-accept had no gate, so the inert card is rendered live (resolved set).
    ready = [e for e in _drain(q) if e["type"] == "diff_ready"]
    assert ready and ready[0]["payload"]["resolved"] == "applied"


@pytest.mark.asyncio
async def test_edit_record_review_persists_card_and_breadcrumb_no_live_diff(tmp_path: Path):
    store = ChatThreadStore(tmp_path / "c.sqlite3")
    th = store.create_thread(str(tmp_path), title="t")
    bc = EventBroadcaster()
    q = bc.subscribe(f"chat:{th.thread_id}")
    ctrl = ChatController(
        workspace_path=str(tmp_path),
        reasoning_engine=ScriptedReasoningEngine(None, []),
        thread_store=store, orchestrator=None, broadcaster=bc, retrieval_client=None)

    await ctrl._edit_record_cb(
        th.thread_id, f"chat:{th.thread_id}", True, _diff(), "reject", "not what I meant")

    msgs = store.get_thread(th.thread_id).messages
    cards = [m for m in msgs if m.type == "diff_card"]
    assert len(cards) == 1 and cards[0].metadata["resolved"] == "discarded"
    # Review mode → a durable breadcrumb AND a live inert card (the cleared EditGate
    # leaves a hole in the transcript; the inert card fills it without a reload).
    crumbs = [m for m in msgs if m.metadata.get("breadcrumb")]
    assert any("Edit rejected" in m.content for m in crumbs)
    events = _drain(q)
    types = {e["type"] for e in events}
    assert "chat_breadcrumb" in types
    ready = [e for e in events if e["type"] == "diff_ready"]
    assert ready and ready[0]["payload"]["resolved"] == "discarded"  # inert, rendered live


# ── Controller: submit_changes persists the turn's pills ───────────────────────

@pytest.mark.asyncio
async def test_finish_submit_changes_persists_pills_and_summary(tmp_path: Path):
    store = ChatThreadStore(tmp_path / "c.sqlite3")
    th = store.create_thread(str(tmp_path), title="t")
    bc = EventBroadcaster()
    q = bc.subscribe(f"chat:{th.thread_id}")
    ctrl = _controller(tmp_path, store)
    ctrl._broadcaster = bc

    outcome = ControllerOutcome(
        kind="submit_changes", text="bumped x",
        tool_events=[{"id": 0, "tool": "read_file", "done": True}],
        thinking_log=["read_file f.py"])
    await ctrl._finish(th.thread_id, f"chat:{th.thread_id}", outcome, step_review=False)

    agent_msgs = [m for m in store.get_thread(th.thread_id).messages if m.role == "agent"]
    assert any(
        m.content == "bumped x"
        and m.metadata.get("tool_events")
        and m.metadata.get("thinking_log") == ["read_file f.py"]
        for m in agent_msgs)
    assert "chat_done" in {e["type"] for e in _drain(q)}


# ── Controller: step_review threaded into the edit re-entry (gap #4) ───────────

@pytest.mark.asyncio
async def test_resolve_mode_edit_honors_remembered_step_review(tmp_path: Path):
    store = ChatThreadStore(tmp_path / "c.sqlite3")
    th = store.create_thread(str(tmp_path), title="t")
    ctrl = _controller(tmp_path, store, orchestrator=object())
    store.set_controller_gate(th.thread_id, PendingGate(
        kind="mode", payload={"options": [{"mode": "edit", "label": "Edit inline now"}]}))
    ctrl._step_review_by_thread[th.thread_id] = True

    captured: dict[str, object] = {}

    async def fake_run_loop(thread_id, channel_id, goal, *, seed_history, step_review, phase=None):
        captured["step_review"] = step_review
        captured["phase"] = phase
        return ControllerOutcome(kind="submit_changes", text="")

    async def fake_finish(*args, **kwargs):
        return None

    ctrl._run_loop = fake_run_loop  # type: ignore[assignment]
    ctrl._finish = fake_finish  # type: ignore[assignment]

    await ctrl.resolve_mode(
        th.thread_id, "edit", channel_id=f"chat:{th.thread_id}", goal="add discount")

    assert captured["step_review"] is True  # NOT the old hardcoded False
    assert captured["phase"] == "EDIT"


@pytest.mark.asyncio
async def test_resolve_mode_create_task_uses_plan_sketch_not_last_message(tmp_path: Path):
    store = ChatThreadStore(tmp_path / "c.sqlite3")
    th = store.create_thread(str(tmp_path), title="t")

    captured: dict[str, object] = {}

    class _Orch:
        async def create_task_from_chat(
            self, *, thread_id, goal, workspace_path, explore_context, store,
            step_review_auto_accept=None,
        ):
            captured["goal"] = goal
            captured["step_review_auto_accept"] = step_review_auto_accept
            captured["explore_context"] = explore_context
            return "task-xyz"

        async def await_plan_ready(self, task_id):
            return None

    ctrl = _controller(tmp_path, store, orchestrator=_Orch())
    # A vague last message + a concrete plan_sketch from the agent's propose_mode.
    store.set_controller_gate(th.thread_id, PendingGate(kind="mode", payload={
        "plan_sketch": "Create src/pricing/ package with discount.py and tax.py + tests",
        "options": [{"mode": "create_task", "label": "Plan it as a task"}]}))
    ctrl._step_review_by_thread[th.thread_id] = True
    # Prior-turn verbatim history with one tool call (the create_task handoff derives
    # pre_explored_context from this, not a separate accumulator).
    ctrl._histories[th.thread_id] = [
        {"role": "assistant", "content": json.dumps(
            {"type": "tool_call", "tool": "read_file", "args": {"path": "src/pricing.py"}})},
        {"role": "tool_result", "tool": "read_file", "content": "def price(): ..."},
    ]

    await ctrl.resolve_mode(
        th.thread_id, "create_task", channel_id=f"chat:{th.thread_id}", goal="keep it minimal")

    # The task goal is the conversation-aware sketch, NOT the bare last message.
    assert captured["goal"] == "Create src/pricing/ package with discount.py and tax.py + tests"
    assert captured["step_review_auto_accept"] is False  # review=True → gate each step
    # The controller's exploration is forwarded (not an empty list → planner re-explores).
    assert captured["explore_context"] == [
        {"tool": "read_file", "args": {"path": "src/pricing.py"},
         "result": "def price(): ...", "is_error": False}]


@pytest.mark.asyncio
async def test_resolve_edit_clears_stale_gate_when_no_waiter(tmp_path: Path):
    """Backend-restart orphan: a persisted EditGate with no live _pending_edit future
    is cleared (+ breadcrumb) instead of no-op'ing, so the UI unwedges."""
    store = ChatThreadStore(tmp_path / "chat.sqlite3")
    thread = store.create_thread(str(tmp_path))
    # Simulate the post-restart state: gate persisted, no in-memory waiter.
    store.set_controller_gate(thread.thread_id, PendingGate(kind="edit", payload={}))
    ctrl = _controller(tmp_path, store)
    assert thread.thread_id not in ctrl._pending_edit

    ok = await ctrl.resolve_edit(thread.thread_id, {"decision": "accept"})
    assert ok is False  # nothing resumed (no waiter)

    refreshed = store.get_thread(thread.thread_id)
    assert refreshed.pending_controller_gate is None  # stale gate cleared
    assert any(
        m.metadata.get("breadcrumb") and "re-send" in m.content.lower()
        for m in refreshed.messages)
