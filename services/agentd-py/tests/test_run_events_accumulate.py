from pathlib import Path

import pytest

from agentd.domain.models import TaskRecord, TaskStatus
from agentd.orchestrator.scripted_engine import ScriptedReasoningEngine
from agentd.orchestrator.engine import AgentOrchestrator
from agentd.patch.engine import PatchEngine
from agentd.storage.sqlite_store import SQLiteTaskStore
from agentd.workspace.shadow import ShadowWorkspaceManager


class _OkValidator:
    async def run(self, _p):
        from agentd.domain.models import ValidationResult
        return ValidationResult(success=True, diagnostics=[], duration_ms=1)


def _make_plan_raw():
    return {
        "analysis": "a",
        "steps": [{"id": "s1", "goal": "create hello", "targets": [{"path": "hello.py", "intent": "new"}], "risk": "low"}],
        "expected_files": ["hello.py"],
        "stop_conditions": ["done"],
    }


def _make_patch_ops():
    return [{"op": "create_file", "file": "hello.py", "content": "x = 1\n", "reason": "seed"}]


@pytest.mark.asyncio
async def test_step_done_event_records_model_step_summary(tmp_path: Path):
    ws = tmp_path / "ws"; ws.mkdir()
    reasoning = ScriptedReasoningEngine(
        plan=_make_plan_raw(),
        patches=[{"candidates": [{"candidate_id": "c1", "patch_ops": _make_patch_ops()}]}],
        tool_step_responses=[
            {"type": "emit_patch", "thought": "create", "patch_ops": _make_patch_ops()},
            {"type": "verify_done", "thought": "ok", "verified": True, "test_output": "1 passed",
             "step_summary": "created hello.py with x=1"},
        ],
    )
    orch = AgentOrchestrator(
        store=SQLiteTaskStore(tmp_path / "db.sqlite3"), reasoning_engine=reasoning,
        validator=_OkValidator(), patch_engine=PatchEngine(),
        workspace_manager=ShadowWorkspaceManager(root_path=tmp_path / "shadows"),
    )
    task = TaskRecord(task_id="t1", goal="create", workspace_path=str(ws))
    await orch._store.create(task)
    await orch.run_task("t1")
    result = await orch.continue_task("t1", feedback=None)
    assert result.status == TaskStatus.READY_FOR_REVIEW
    events = result.execution_state.run_events
    done = [e for e in events if e.kind == "step_done"]
    assert any(e.step_id == "s1" and e.note == "created hello.py with x=1" for e in done)


@pytest.mark.asyncio
async def test_task_narrative_synthesized_at_ready_for_review(tmp_path: Path):
    ws = tmp_path / "ws"; ws.mkdir()
    reasoning = ScriptedReasoningEngine(
        plan=_make_plan_raw(),
        patches=[{"candidates": [{"candidate_id": "c1", "patch_ops": _make_patch_ops()}]}],
        tool_step_responses=[
            {"type": "emit_patch", "thought": "create", "patch_ops": _make_patch_ops()},
            {"type": "verify_done", "thought": "ok", "verified": True, "test_output": "", "step_summary": "created hello.py"},
        ],
        run_narrative={"headline": "Created hello.py", "points": ["added x=1"]},
    )
    orch = AgentOrchestrator(
        store=SQLiteTaskStore(tmp_path / "db.sqlite3"), reasoning_engine=reasoning,
        validator=_OkValidator(), patch_engine=PatchEngine(),
        workspace_manager=ShadowWorkspaceManager(root_path=tmp_path / "shadows"),
    )
    task = TaskRecord(task_id="t2", goal="create", workspace_path=str(ws))
    await orch._store.create(task)
    await orch.run_task("t2")
    result = await orch.continue_task("t2", feedback=None)
    assert result.status == TaskStatus.READY_FOR_REVIEW
    assert result.task_narrative is not None
    assert result.task_narrative.outcome == "succeeded"
    assert result.task_narrative.headline == "Created hello.py"
    stored = await orch._store.get("t2")
    assert stored.task_narrative is not None
