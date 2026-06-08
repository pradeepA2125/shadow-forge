"""Every resolved gate leaves a durable transcript breadcrumb in the chat thread.

The live actionable card is ephemeral (rendered from /live); the breadcrumb is the
permanent record of what the user decided, so chat history reads as a clear narrative.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agentd.chat.storage import ChatThreadStore
from agentd.domain.models import (
    CommandDecision,
    Diagnostic,
    PlanStep,
    ScopePolicy,
    ScopeTrigger,
    StepRunResult,
    TaskRecord,
    TaskStatus,
    ValidationResult,
)
from agentd.domain.state_machine import transition
from agentd.tools.loop import ScopeDecision
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
    async def run(self, workspace_path) -> ValidationResult:
        return ValidationResult(success=True, diagnostics=[], duration_ms=0)


def _make(tmp_path: Path, **kw) -> tuple[AgentOrchestrator, ChatThreadStore, str]:
    chat_store = ChatThreadStore(tmp_path / "chat.db")
    thread = chat_store.create_thread(str(tmp_path))
    orch = AgentOrchestrator(
        store=InMemoryTaskStore(),
        reasoning_engine=_NoopReasoning(),
        validator=_Validator(),
        patch_engine=PatchEngine(),
        workspace_manager=ShadowWorkspaceManager(root_path=tmp_path / "shadows"),
        chat_store=chat_store,
        **kw,
    )
    return orch, chat_store, thread.thread_id


def _breadcrumbs(chat_store: ChatThreadStore, thread_id: str) -> list[str]:
    thread = chat_store.get_thread(thread_id)
    assert thread is not None
    return [m.content for m in thread.messages if m.metadata.get("breadcrumb")]


async def _seed_executing(orch: AgentOrchestrator, thread_id: str, ws: Path) -> TaskRecord:
    task = TaskRecord(task_id="t1", goal="g", workspace_path=str(ws),
                      chat_channel_id=f"chat:{thread_id}")
    for status, reason in [
        (TaskStatus.CONTEXT_READY, "ctx"),
        (TaskStatus.AWAITING_PLAN_APPROVAL, "approval"),
        (TaskStatus.PLANNED, "planned"),
        (TaskStatus.EXECUTING, "executing"),
    ]:
        task = transition(task, status, reason)
    task.execution_state.current_step_id = "s1"
    await orch._store.create(task)
    return task


async def _wait_pending(d: dict, key: str) -> None:
    for _ in range(200):
        await asyncio.sleep(0)
        if key in d:
            return
    raise AssertionError(f"gate future never registered for {key}")


@pytest.mark.asyncio
async def test_command_gate_breadcrumb(tmp_path: Path) -> None:
    ws = tmp_path / "ws"; ws.mkdir()
    orch, chat_store, thread_id = _make(tmp_path)
    task = await _seed_executing(orch, thread_id, ws)
    cb = orch._build_command_approval_callback(task.task_id)
    gate = asyncio.create_task(cb("pytest", ["-q"], str(ws)))
    await _wait_pending(orch._pending_command_decisions, task.task_id)
    orch._pending_command_decisions[task.task_id].set_result(CommandDecision(approve=True))
    await gate
    crumbs = _breadcrumbs(chat_store, thread_id)
    assert any("pytest" in c and "approved" in c.lower() for c in crumbs)


@pytest.mark.asyncio
async def test_scope_gate_breadcrumb(tmp_path: Path) -> None:
    ws = tmp_path / "ws"; ws.mkdir()
    orch, chat_store, thread_id = _make(
        tmp_path, scope_policy=ScopePolicy.ASK, scope_trigger=ScopeTrigger.ANY,
    )
    task = await _seed_executing(orch, thread_id, ws)
    step = PlanStep(id="s1", goal="g", targets=[], risk="low")
    cb = orch._build_scope_callback(task.task_id, "s1", step)
    gate = asyncio.create_task(cb(["extra.py"], "needs helper"))
    await _wait_pending(orch._pending_scope_decisions, task.task_id)
    orch._pending_scope_decisions[task.task_id].set_result(
        ScopeDecision(approve=True, extended_files=["extra.py"])
    )
    await gate
    crumbs = _breadcrumbs(chat_store, thread_id)
    assert any("extra.py" in c for c in crumbs)


@pytest.mark.asyncio
async def test_validation_gate_breadcrumb(tmp_path: Path) -> None:
    ws = tmp_path / "ws"; ws.mkdir()
    orch, chat_store, thread_id = _make(tmp_path)
    task = await _seed_executing(orch, thread_id, ws)
    task = transition(task, TaskStatus.VALIDATING, "validating")
    await orch._store.save(task)
    validation = ValidationResult(
        success=False, duration_ms=1,
        diagnostics=[Diagnostic(source="pytest", message="x", level="error")],
    )
    gate = asyncio.create_task(orch._pause_for_validation_decision(task, validation))
    await _wait_pending(orch._pending_validation_decisions, task.task_id)
    orch._pending_validation_decisions[task.task_id].set_result(True)
    await gate
    crumbs = _breadcrumbs(chat_store, thread_id)
    assert any("validation" in c.lower() and "accept" in c.lower() for c in crumbs)


@pytest.mark.asyncio
async def test_plan_approval_breadcrumb(tmp_path: Path) -> None:
    ws = tmp_path / "ws"; ws.mkdir()
    orch, chat_store, thread_id = _make(tmp_path)
    task = TaskRecord(
        task_id="t1", goal="g", workspace_path=str(ws),
        chat_channel_id=f"chat:{thread_id}", plan_markdown="# Plan\n- do it",
    )
    for status, reason in [
        (TaskStatus.CONTEXT_READY, "ctx"),
        (TaskStatus.AWAITING_PLAN_APPROVAL, "approval"),
    ]:
        task = transition(task, status, reason)
    await orch._store.create(task)
    # The breadcrumb fires at the PLANNED transition, before JSON-plan generation
    # (which the noop reasoner can't satisfy) — so tolerate the later failure.
    try:
        await orch.continue_task("t1", feedback=None)
    except Exception:
        pass
    crumbs = _breadcrumbs(chat_store, thread_id)
    assert any("plan approved" in c.lower() for c in crumbs)


@pytest.mark.asyncio
async def test_step_gate_breadcrumb(tmp_path: Path) -> None:
    ws = tmp_path / "ws"; ws.mkdir()
    shadow = tmp_path / "shadow"; shadow.mkdir()
    orch, chat_store, thread_id = _make(tmp_path)
    task = await _seed_executing(orch, thread_id, ws)
    step = PlanStep(id="s1", goal="add the thing", targets=[], risk="low")
    step_result = StepRunResult(
        step_id="s1",
        outcome="step_completed",
        validation_result="validation_passed",
        attempts_used=1,
        touched_files=[],
    )
    gate = asyncio.create_task(
        orch._pause_for_step_review(task, step, step_result, shadow, ws)
    )
    await _wait_pending(orch._pending_step_decisions, task.task_id)
    orch._pending_step_decisions[task.task_id].set_result("accept")
    await gate
    crumbs = _breadcrumbs(chat_store, thread_id)
    assert any("accept" in c.lower() for c in crumbs)
