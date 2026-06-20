"""EDIT-mode clarify: the controller may ask a clarifying question while editing,
and the user's reply RESUMES the loop in EDIT (not a DECIDE restart that would
force re-picking the mode). Mirrors the DECIDE clarify≈feedback-resume, but the
phase is preserved via a per-thread stash."""
from pathlib import Path

import pytest

from agentd.chat.controller import ChatController
from agentd.chat.storage import ChatThreadStore
from agentd.orchestrator.broadcaster import EventBroadcaster
from agentd.orchestrator.engine import AgentOrchestrator
from agentd.orchestrator.scripted_engine import ScriptedReasoningEngine
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


class _PhaseRecordingEngine(ScriptedReasoningEngine):
    """Records the `phase` each controller step ran in, in call order."""

    def __init__(self, responses):
        super().__init__(None, [], controller_step_responses=responses)
        self.phases: list[str] = []

    async def create_controller_step(
        self, plan_context, history, tool_definitions, *, phase, on_thinking=None):
        self.phases.append(phase)
        return await super().create_controller_step(
            plan_context, history, tool_definitions, phase=phase, on_thinking=on_thinking)


def _orchestrator(tmp_path: Path) -> AgentOrchestrator:
    return AgentOrchestrator(
        store=InMemoryTaskStore(),
        reasoning_engine=_NoopReasoning(),
        validator=_Validator(),
        patch_engine=PatchEngine(),
        workspace_manager=ShadowWorkspaceManager(root_path=tmp_path / "shadows"),
    )


@pytest.mark.asyncio
async def test_clarify_in_edit_mode_resumes_in_edit(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    store = ChatThreadStore(tmp_path / "c.sqlite3")
    th = store.create_thread(str(ws), title="t")
    chan = f"chat:{th.thread_id}"

    eng = _PhaseRecordingEngine([
        # turn 1 (DECIDE): propose edit
        {"type": "propose_mode", "thought": "t", "plan_sketch": "add clamp() to util.py",
         "reason": "r", "recommended": "edit", "options": [
             {"mode": "edit", "label": "Edit inline now", "description": "d"}]},
        # mode pick → EDIT: agent is blocked, asks a question
        {"type": "clarify", "thought": "t", "question": "clamp to what range?"},
        # user replies → MUST resume in EDIT: emit the edit, then submit
        {"type": "edit", "thought": "t", "patch_ops": [
            {"op": "create_file", "file": "util.py",
             "content": "def clamp(x):\n    return max(0, min(1, x))\n", "reason": "add"}]},
        {"type": "submit_changes", "thought": "done", "summary": "added clamp"},
    ])
    ctrl = ChatController(
        workspace_path=str(ws), reasoning_engine=eng, thread_store=store,
        orchestrator=_orchestrator(tmp_path), broadcaster=EventBroadcaster(),
        retrieval_client=None)

    # turn 1 → propose_mode gate
    await ctrl.handle_message(th.thread_id, "add a clamp helper", channel_id=chan)
    # pick edit → EDIT loop emits clarify (question to the user)
    await ctrl.resolve_mode(th.thread_id, "edit", channel_id=chan, goal="add a clamp helper")
    # the EDIT clarify is stashed so the next message resumes EDIT
    assert th.thread_id in ctrl._edit_clarify_pending
    # user answers → the resumed turn runs in EDIT (NOT DECIDE), emits the edit + submit
    await ctrl.handle_message(th.thread_id, "[0, 1]", channel_id=chan)

    # Phases: turn1=DECIDE, mode-pick=EDIT(clarify), resumed turn=EDIT,EDIT (edit+submit).
    assert eng.phases == ["DECIDE", "EDIT", "EDIT", "EDIT"]
    # The edit was actually applied to the real workspace (instant-promote).
    assert (ws / "util.py").read_text().startswith("def clamp(")
    # Stash cleared once the EDIT turn terminated cleanly.
    assert th.thread_id not in ctrl._edit_clarify_pending


@pytest.mark.asyncio
async def test_decide_clarify_does_not_set_edit_resume(tmp_path: Path):
    """A plain DECIDE-phase clarify must NOT mark the thread for EDIT resume —
    only an EDIT-phase clarify does."""
    ws = tmp_path / "ws"
    ws.mkdir()
    store = ChatThreadStore(tmp_path / "c.sqlite3")
    th = store.create_thread(str(ws), title="t")
    eng = _PhaseRecordingEngine([
        {"type": "clarify", "thought": "t", "question": "which thing?"},
    ])
    ctrl = ChatController(
        workspace_path=str(ws), reasoning_engine=eng, thread_store=store,
        orchestrator=_orchestrator(tmp_path), broadcaster=EventBroadcaster(),
        retrieval_client=None)
    await ctrl.handle_message(th.thread_id, "fix it", channel_id=f"chat:{th.thread_id}")
    assert th.thread_id not in ctrl._edit_clarify_pending
