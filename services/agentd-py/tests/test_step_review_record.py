"""Step reviews leave a durable diff_card record; auto-accept leaves a breadcrumb."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agentd.chat.storage import ChatThreadStore
from agentd.domain.models import PlanStep, StepRunResult, TaskRecord, TaskStatus
from agentd.domain.state_machine import transition
from agentd.orchestrator.engine import AgentOrchestrator
from agentd.patch.engine import PatchEngine
from agentd.storage.in_memory import InMemoryTaskStore
from agentd.workspace.shadow import ShadowWorkspaceManager


class _NoopReasoning:
    async def create_plan(self, *a, **k): raise NotImplementedError
    async def create_patch(self, *a, **k): raise NotImplementedError
    async def create_tool_step(self, *a, **k): raise NotImplementedError
    async def create_planning_step(self, *a, **k): raise NotImplementedError


class _Validator:
    async def run(self, workspace_path): raise NotImplementedError


def _make(tmp_path: Path) -> tuple[AgentOrchestrator, ChatThreadStore, str]:
    chat_store = ChatThreadStore(tmp_path / "chat.db")
    thread = chat_store.create_thread(str(tmp_path))
    orch = AgentOrchestrator(
        store=InMemoryTaskStore(),
        reasoning_engine=_NoopReasoning(),
        validator=_Validator(),
        patch_engine=PatchEngine(),
        workspace_manager=ShadowWorkspaceManager(root_path=tmp_path / "shadows"),
        chat_store=chat_store,
    )
    return orch, chat_store, thread.thread_id


async def _seed_executing(orch, thread_id: str, ws: Path) -> TaskRecord:
    task = TaskRecord(task_id="t1", goal="g", workspace_path=str(ws),
                      chat_channel_id=f"chat:{thread_id}")
    for status, reason in [
        (TaskStatus.CONTEXT_READY, "ctx"),
        (TaskStatus.AWAITING_PLAN_APPROVAL, "approval"),
        (TaskStatus.PLANNED, "planned"),
        (TaskStatus.EXECUTING, "executing"),
    ]:
        task = transition(task, status, reason)
    await orch._store.create(task)
    return task


async def _wait_pending(d: dict, key: str) -> None:
    for _ in range(200):
        await asyncio.sleep(0)
        if key in d:
            return
    raise AssertionError("gate future never registered")


@pytest.mark.asyncio
async def test_step_review_accept_persists_diff_record(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "a.py").write_text("x = 2\n")
    (ws / "a.py").write_text("x = 1\n")

    orch, chat_store, thread_id = _make(tmp_path)
    task = await _seed_executing(orch, thread_id, ws)
    step = PlanStep(id="s1", goal="bump x", targets=[], risk="low")
    step_result = StepRunResult(
        step_id="s1", outcome="step_completed", validation_result="validation_passed",
        attempts_used=1, touched_files=["a.py"],
    )

    gate = asyncio.create_task(
        orch._pause_for_step_review(task, step, step_result, shadow, ws)
    )
    await _wait_pending(orch._pending_step_decisions, task.task_id)
    orch._pending_step_decisions[task.task_id].set_result("accept")
    await gate

    thread = chat_store.get_thread(thread_id)
    cards = [m for m in thread.messages if m.type == "diff_card"]
    assert len(cards) == 1
    card = cards[0]
    assert card.metadata["resolved"] == "applied"
    assert card.metadata["step_id"] == "s1"
    [entry] = card.metadata["diff_entries"]
    assert entry["path"] == "a.py"
    assert "+x = 2" in entry["unified_diff"]
    # Record precedes the breadcrumb in the transcript.
    crumb_idx = next(i for i, m in enumerate(thread.messages)
                     if m.metadata.get("breadcrumb") and "accepted" in m.content)
    card_idx = thread.messages.index(card)
    assert card_idx < crumb_idx


@pytest.mark.asyncio
async def test_step_review_discard_persists_discarded_record(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "a.py").write_text("x = 2\n")

    orch, chat_store, thread_id = _make(tmp_path)
    task = await _seed_executing(orch, thread_id, ws)
    step = PlanStep(id="s1", goal="bump x", targets=[], risk="low")
    step_result = StepRunResult(
        step_id="s1", outcome="step_completed", validation_result="validation_passed",
        attempts_used=1, touched_files=["a.py"],
    )

    gate = asyncio.create_task(
        orch._pause_for_step_review(task, step, step_result, shadow, ws)
    )
    await _wait_pending(orch._pending_step_decisions, task.task_id)
    orch._pending_step_decisions[task.task_id].set_result("discard")
    await gate

    thread = chat_store.get_thread(thread_id)
    [card] = [m for m in thread.messages if m.type == "diff_card"]
    assert card.metadata["resolved"] == "discarded"


@pytest.mark.asyncio
async def test_auto_accept_writes_step_completed_breadcrumb(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    orch, chat_store, thread_id = _make(tmp_path)
    task = await _seed_executing(orch, thread_id, ws)
    step = PlanStep(id="s1", goal="bump x in calculator", targets=[], risk="low")

    orch._write_step_completed_breadcrumb(task, step)

    thread = chat_store.get_thread(thread_id)
    crumbs = [m.content for m in thread.messages if m.metadata.get("breadcrumb")]
    assert any("Step completed" in c and "bump x" in c for c in crumbs)


@pytest.mark.asyncio
async def test_auto_accept_diff_record_persists_and_broadcasts(tmp_path: Path) -> None:
    """Auto-accept (no review gate) still leaves a resolved diff_card in the
    transcript AND broadcasts it live so it renders without a reload."""
    ws = tmp_path / "ws"
    ws.mkdir()
    orch, chat_store, thread_id = _make(tmp_path)
    task = await _seed_executing(orch, thread_id, ws)

    diff_entries = [{
        "path": "a.py", "additions": 1, "deletions": 1,
        "temp_path": "", "unified_diff": "+x = 2\n-x = 1\n",
    }]
    orch._write_chat_step_diff_record(
        task, "s1", "bump x", diff_entries, "accept", broadcast_live=True,
    )

    # Durable, inert (resolved=applied) record.
    thread = chat_store.get_thread(thread_id)
    [card] = [m for m in thread.messages if m.type == "diff_card"]
    assert card.metadata["resolved"] == "applied"
    assert card.metadata["step_id"] == "s1"
    assert card.metadata["diff_entries"][0]["path"] == "a.py"

    # Live broadcast on the task channel (where the chat stream is bridged).
    events = list(orch.broadcaster._replay[task.task_id])
    diff_ready = [e for e in events if e["type"] == "diff_ready"]
    assert len(diff_ready) == 1
    payload = diff_ready[0]["payload"]
    assert payload["resolved"] == "applied"
    assert payload["task_id"] == task.task_id
    assert payload["diff_entries"][0]["path"] == "a.py"


@pytest.mark.asyncio
async def test_step_review_record_no_live_broadcast_by_default(tmp_path: Path) -> None:
    """The review path's durable record must NOT broadcast diff_ready — the live
    StepGate already showed the diff (broadcast_live defaults False)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    orch, chat_store, thread_id = _make(tmp_path)
    task = await _seed_executing(orch, thread_id, ws)

    orch._write_chat_step_diff_record(
        task, "s1", "bump x",
        [{"path": "a.py", "additions": 1, "deletions": 0,
          "temp_path": "", "unified_diff": "+x = 2\n"}],
        "accept",
    )

    events = list(orch.broadcaster._replay[task.task_id])
    assert not [e for e in events if e["type"] == "diff_ready"]
