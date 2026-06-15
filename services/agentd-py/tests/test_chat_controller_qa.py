from pathlib import Path

import pytest

from agentd.chat.controller import ChatController
from agentd.chat.storage import ChatThreadStore
from agentd.orchestrator.broadcaster import EventBroadcaster
from agentd.orchestrator.scripted_engine import ScriptedReasoningEngine


@pytest.mark.asyncio
async def test_qa_turn_persists_answer(tmp_path: Path):
    store = ChatThreadStore(tmp_path / "chat.sqlite3")
    thread = store.create_thread(str(tmp_path), title="t")
    ctrl = ChatController(
        workspace_path=str(tmp_path),
        reasoning_engine=ScriptedReasoningEngine(
            None, [], controller_step_responses=[
                {"type": "answer", "thought": "t", "answer": "hello"}]),
        thread_store=store, orchestrator=None, broadcaster=EventBroadcaster(),
        retrieval_client=None)
    await ctrl.handle_message(thread.thread_id, "hi", channel_id="c1")
    reloaded = store.get_thread(thread.thread_id)
    assert reloaded is not None
    assert any(m.role == "agent" and "hello" in m.content for m in reloaded.messages)


@pytest.mark.asyncio
async def test_clarify_turn_persists_question(tmp_path: Path):
    store = ChatThreadStore(tmp_path / "chat.sqlite3")
    thread = store.create_thread(str(tmp_path), title="t")
    ctrl = ChatController(
        workspace_path=str(tmp_path),
        reasoning_engine=ScriptedReasoningEngine(
            None, [], controller_step_responses=[
                {"type": "clarify", "thought": "t", "question": "which file?"}]),
        thread_store=store, orchestrator=None, broadcaster=EventBroadcaster(),
        retrieval_client=None)
    await ctrl.handle_message(thread.thread_id, "change the thing", channel_id="c1")
    reloaded = store.get_thread(thread.thread_id)
    assert reloaded is not None
    assert any(m.role == "agent" and "which file?" in m.content for m in reloaded.messages)


@pytest.mark.asyncio
async def test_first_message_sets_thread_title(tmp_path: Path):
    store = ChatThreadStore(tmp_path / "chat.sqlite3")
    thread = store.create_thread(str(tmp_path), title="New Chat")
    ctrl = ChatController(
        workspace_path=str(tmp_path),
        reasoning_engine=ScriptedReasoningEngine(
            None, [], controller_step_responses=[
                {"type": "answer", "thought": "t", "answer": "ok"}]),
        thread_store=store, orchestrator=None, broadcaster=EventBroadcaster(),
        retrieval_client=None)
    await ctrl.handle_message(thread.thread_id, "rename my variables please", channel_id="c1")
    reloaded = store.get_thread(thread.thread_id)
    assert reloaded is not None and reloaded.title.startswith("rename my variables")
