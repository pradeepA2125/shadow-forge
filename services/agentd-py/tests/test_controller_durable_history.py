"""Durable controller conversation history (mirrors planner's planning_conversation_history).

The in-memory ``ChatController._histories`` is lost on a backend restart, so the
next turn re-explores cold even though the transcript is on screen. These tests
pin the planner-style fix: persist the verbatim turn history on the thread and
rehydrate it as seed_history on a cache miss.
"""
from pathlib import Path

import pytest

from agentd.chat.controller import ChatController
from agentd.chat.storage import ChatThreadStore
from agentd.orchestrator.broadcaster import EventBroadcaster
from agentd.orchestrator.scripted_engine import ScriptedReasoningEngine


def _qa_controller(store: ChatThreadStore, tmp_path: Path) -> ChatController:
    return ChatController(
        workspace_path=str(tmp_path),
        reasoning_engine=ScriptedReasoningEngine(
            None, [], controller_step_responses=[
                {"type": "tool_call", "thought": "look", "tool": "read_file",
                 "args": {"path": "f.py"}},
                {"type": "answer", "thought": "done", "answer": "x is 1"}]),
        thread_store=store, orchestrator=None, broadcaster=EventBroadcaster(),
        retrieval_client=None)


@pytest.mark.asyncio
async def test_conversation_history_not_exposed_in_thread_get(tmp_path: Path):
    """The seed substrate is backend-internal (potentially large, no UI consumer).
    Leaking it into the thread GET response risks a strict-Zod parse throw on the
    client (the finding-#3 class bug) and ships needless bloat."""
    from httpx import ASGITransport, AsyncClient

    from agentd.chat.app_factory import build_app

    app = build_app(workspace_path=str(tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        thread_id = (
            await client.post("/v1/chat/threads", json={"workspace": str(tmp_path)})
        ).json()["thread_id"]
        get_resp = await client.get(f"/v1/chat/threads/{thread_id}")

    assert "controller_conversation_history" not in get_resp.json(), \
        "internal seed history must not be serialized to the client"


class _RecordingEngine(ScriptedReasoningEngine):
    """Captures the verbatim ``history`` + ``plan_context`` handed to each controller
    step so a test can assert what the loop replayed (real behavior, not a mock)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.histories_seen: list[list[dict]] = []
        self.plan_contexts_seen: list[dict] = []
        self.calls_seen: list[dict] = []

    def reset_recording(self) -> None:
        self.histories_seen.clear()
        self.plan_contexts_seen.clear()
        self.calls_seen.clear()

    async def create_controller_step(self, plan_context, history, tool_definitions,
                                      *, phase, on_thinking=None):
        self.histories_seen.append([dict(h) for h in history])
        self.plan_contexts_seen.append(dict(plan_context))
        self.calls_seen.append({
            "plan_context": dict(plan_context),
            "history": [dict(h) for h in history],
            "tool_definitions": [dict(t) for t in tool_definitions],
            "phase": phase,
        })
        return await super().create_controller_step(
            plan_context, history, tool_definitions, phase=phase, on_thinking=on_thinking)


class _Ctx:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def as_prompt_payload(self) -> dict:
        return self._payload


class _StubRetrieval:
    """Returns a fixed retrieval payload and counts load_context calls so a test can
    prove the seed was rehydrated (not recomputed) after a restart."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.calls = 0

    def load_context(self, workspace_path: str, goal: str):
        _ = (workspace_path, goal)
        self.calls += 1
        return _Ctx(self._payload), []


def _serialized_head(call: dict) -> tuple[str, str]:
    """Build the EXACT cacheable head the engine sends (real builders), and return
    (system_instructions, json-serialized payload-head up through conversation_history).
    The KV prefix matches on these bytes."""
    import json as _json

    from agentd.chat.controller_prompts import (
        build_controller_step_payload,
        format_controller_system_prompt,
    )

    system = format_controller_system_prompt(call["tool_definitions"])
    payload = build_controller_step_payload(
        call["plan_context"], call["history"], call["tool_definitions"],
        phase=call["phase"])
    head: dict = {}
    for key, value in payload.items():
        head[key] = value
        if key == "conversation_history":
            break
    # No sort_keys: insertion order IS what's sent, and order is what KV-prefix bytes
    # depend on. ensure_ascii to mirror a deterministic dump.
    return system, _json.dumps(head, ensure_ascii=False)


@pytest.mark.asyncio
async def test_cacheable_head_byte_identical_across_restart(tmp_path: Path):
    """The actual claim behind the KV-cache hit: the serialized head (system prompt +
    pinned retrieval_seed + replayed conversation_history) that the engine sends on a
    turn is BYTE-IDENTICAL whether or not the backend restarted before that turn."""
    import shutil

    (tmp_path / "f.py").write_text("x = 1\n")
    db = tmp_path / "chat.sqlite3"
    store = ChatThreadStore(db)
    thread = store.create_thread(str(tmp_path), title="t")

    # Controller A: turn 1 (tool_call + answer) builds history + pins the seed.
    engine_a = _RecordingEngine(
        None, [], controller_step_responses=[
            {"type": "tool_call", "thought": "look", "tool": "read_file",
             "args": {"path": "f.py"}},
            {"type": "answer", "thought": "done", "answer": "x is 1"},
            {"type": "answer", "thought": "t", "answer": "still 1"}])
    ctrl_a = ChatController(
        workspace_path=str(tmp_path), reasoning_engine=engine_a,
        thread_store=store, orchestrator=None, broadcaster=EventBroadcaster(),
        retrieval_client=_StubRetrieval({"seed_marker": "v1", "files": ["f.py"]}))
    await ctrl_a.handle_message(thread.thread_id, "what is x", channel_id="c1")

    # Snapshot the DB at the restart point — AFTER turn 1, BEFORE turn 2 re-persists
    # the grown history. A real restart-before-turn-2 reads exactly this state.
    db_after_turn1 = tmp_path / "chat_after_turn1.sqlite3"
    shutil.copy(db, db_after_turn1)

    # No-restart baseline: A continues turn 2 in-memory.
    engine_a.reset_recording()
    await ctrl_a.handle_message(thread.thread_id, "are you sure?", channel_id="c1")
    head_a = _serialized_head(engine_a.calls_seen[0])

    # Restart: fresh controller B on the post-turn-1 DB snapshot, same turn 2 message.
    # Its retrieval client would now return DIFFERENT bytes — the pinned seed must win.
    store_b = ChatThreadStore(db_after_turn1)
    engine_b = _RecordingEngine(
        None, [], controller_step_responses=[
            {"type": "answer", "thought": "t", "answer": "still 1"}])
    ctrl_b = ChatController(
        workspace_path=str(tmp_path), reasoning_engine=engine_b,
        thread_store=store_b, orchestrator=None, broadcaster=EventBroadcaster(),
        retrieval_client=_StubRetrieval({"seed_marker": "v2-CHANGED", "files": ["zzz"]}))
    await ctrl_b.handle_message(thread.thread_id, "are you sure?", channel_id="c2")
    head_b = _serialized_head(engine_b.calls_seen[0])

    assert head_a[0] == head_b[0], "system prompt diverged across restart"
    assert head_a[1] == head_b[1], "payload head (seed + conversation_history) diverged across restart"


@pytest.mark.asyncio
async def test_retrieval_seed_pinned_across_restart(tmp_path: Path):
    """The seed is frozen for the thread's life (deltas append to the history tail,
    never the seed). Pin it durably so a restart replays the SAME bytes — the KV
    prefix matches even if the snapshot was re-indexed meanwhile."""
    store = ChatThreadStore(tmp_path / "chat.sqlite3")
    thread = store.create_thread(str(tmp_path), title="t")

    retr_a = _StubRetrieval({"seed_marker": "v1", "files": ["a.py"]})
    ctrl_a = ChatController(
        workspace_path=str(tmp_path),
        reasoning_engine=ScriptedReasoningEngine(
            None, [], controller_step_responses=[
                {"type": "answer", "thought": "t", "answer": "ok"}]),
        thread_store=store, orchestrator=None, broadcaster=EventBroadcaster(),
        retrieval_client=retr_a)
    await ctrl_a.handle_message(thread.thread_id, "hi", channel_id="c1")
    assert retr_a.calls == 1  # computed once

    # Restart: fresh controller; the retrieval client would now return a DIFFERENT
    # seed (simulating a background re-index). The pinned v1 must still be replayed.
    retr_b = _StubRetrieval({"seed_marker": "v2-CHANGED", "files": ["b.py"]})
    engine_b = _RecordingEngine(
        None, [], controller_step_responses=[
            {"type": "answer", "thought": "t", "answer": "ok"}])
    ctrl_b = ChatController(
        workspace_path=str(tmp_path), reasoning_engine=engine_b,
        thread_store=store, orchestrator=None, broadcaster=EventBroadcaster(),
        retrieval_client=retr_b)
    await ctrl_b.handle_message(thread.thread_id, "again", channel_id="c2")

    seed_used = engine_b.plan_contexts_seen[0].get("retrieval_seed")
    assert seed_used == {"seed_marker": "v1", "files": ["a.py"]}, \
        f"pinned seed not replayed after restart; got {seed_used}"
    assert retr_b.calls == 0, "seed was recomputed instead of rehydrated from the store"


@pytest.mark.asyncio
async def test_fresh_controller_rehydrates_seed_history_from_store(tmp_path: Path):
    """Simulates a backend restart: a brand-new ChatController on the same DB must
    replay the prior turn's history (loaded from the store) as seed_history."""
    (tmp_path / "f.py").write_text("x = 1\n")
    store = ChatThreadStore(tmp_path / "chat.sqlite3")
    thread = store.create_thread(str(tmp_path), title="t")

    # Turn 1 on controller A — populates + persists history.
    await _qa_controller(store, tmp_path).handle_message(
        thread.thread_id, "what is x", channel_id="c1")

    # Restart: a fresh controller (empty in-memory _histories) + recording engine.
    engine_b = _RecordingEngine(
        None, [], controller_step_responses=[
            {"type": "answer", "thought": "t", "answer": "still 1"}])
    ctrl_b = ChatController(
        workspace_path=str(tmp_path), reasoning_engine=engine_b,
        thread_store=store, orchestrator=None, broadcaster=EventBroadcaster(),
        retrieval_client=None)

    await ctrl_b.handle_message(thread.thread_id, "are you sure?", channel_id="c2")

    first_history = engine_b.histories_seen[0]
    assert any("read_file" in str(h.get("content", "")) for h in first_history), \
        f"prior turn not rehydrated as seed_history after restart; got {first_history}"
    # The new user message rides after the replayed prefix.
    assert any(h.get("role") == "user" and "are you sure?" in str(h.get("content", ""))
               for h in first_history)


class _RecordingOrchestrator:
    """Captures the explore_context the controller forwards on a create_task handoff."""

    def __init__(self) -> None:
        self.explore_context: list[dict] | None = None

    async def create_task_from_chat(self, *, thread_id, goal, workspace_path,
                                    explore_context, store, step_review_auto_accept):
        _ = (thread_id, goal, workspace_path, store, step_review_auto_accept)
        self.explore_context = explore_context
        return "task-xyz"

    async def await_plan_ready(self, task_id, timeout_sec: float = 3600.0):
        _ = (task_id, timeout_sec)
        return None


@pytest.mark.asyncio
async def test_create_task_explore_context_derived_from_history_uncapped(tmp_path: Path):
    """The create_task handoff forwards every tool call in the thread's history as
    pre_explored_context — derived from the verbatim (uncapped) conversation, not a
    separate 4000-capped accumulator."""
    # ~8 KB but well under read_file's own caps (500 lines / 100 KB): long lines, few
    # of them, so the tool returns it whole. END_MARKER sits past char 4000, so it
    # survives ONLY if history is forwarded uncapped (the old _explore_by_thread path
    # truncated at 4000 via trace_to_tool_events).
    lines = ["y = 2"] + ["# " + "p" * 98 for _ in range(80)] + ["END_MARKER_ZZZ"]
    (tmp_path / "big.py").write_text("\n".join(lines) + "\n")
    store = ChatThreadStore(tmp_path / "chat.sqlite3")
    thread = store.create_thread(str(tmp_path), title="t")
    orch = _RecordingOrchestrator()
    ctrl = ChatController(
        workspace_path=str(tmp_path),
        reasoning_engine=ScriptedReasoningEngine(
            None, [], controller_step_responses=[
                {"type": "tool_call", "thought": "look", "tool": "read_file",
                 "args": {"path": "big.py"}},
                {"type": "propose_mode", "thought": "t", "plan_sketch": "do the thing",
                 "recommended": "create_task",
                 "options": [
                     {"mode": "create_task", "label": "Plan it", "description": "d"},
                     {"mode": "edit", "label": "Edit", "description": "d"}]}]),
        thread_store=store, orchestrator=orch, broadcaster=EventBroadcaster(),
        retrieval_client=None)

    await ctrl.handle_message(thread.thread_id, "do the thing", channel_id="c1")
    await ctrl.resolve_mode(thread.thread_id, "create_task", channel_id="c1", goal="do the thing")

    ctx = orch.explore_context
    assert ctx, "no explore_context forwarded to create_task"
    read_calls = [e for e in ctx if e.get("tool") == "read_file"]
    assert len(read_calls) == 1, f"expected the read_file tool call; got {ctx}"
    entry = read_calls[0]
    assert entry["args"] == {"path": "big.py"}
    # Full, uncapped result: the end-of-file marker (well past the old 4000-char cap)
    # must be present, proving the history's verbatim result was forwarded.
    assert "END_MARKER_ZZZ" in str(entry["result"]), \
        "result was truncated — must forward uncapped history, not the capped copy"


@pytest.mark.asyncio
async def test_turn_persists_conversation_history_to_store(tmp_path: Path):
    (tmp_path / "f.py").write_text("x = 1\n")
    store = ChatThreadStore(tmp_path / "chat.sqlite3")
    thread = store.create_thread(str(tmp_path), title="t")
    await _qa_controller(store, tmp_path).handle_message(
        thread.thread_id, "what is x", channel_id="c1")

    reloaded = store.get_thread(thread.thread_id)
    assert reloaded is not None
    history = reloaded.controller_conversation_history
    assert history, "conversation history not persisted to the store"
    # The verbatim tool-call turn must be present (not a lossy digest).
    assert any("read_file" in str(entry.get("content", "")) for entry in history), \
        f"tool-call turn missing from persisted history; got {history}"
