"""Tests for AgentOrchestrator.run_inline_change() and related helpers."""
from __future__ import annotations
import pytest
from pathlib import Path
from agentd.orchestrator.broadcaster import EventBroadcaster
from agentd.orchestrator.engine import AgentOrchestrator
from agentd.patch.engine import PatchEngine
from agentd.storage.in_memory import InMemoryTaskStore
from agentd.workspace.shadow import ShadowWorkspaceManager


class _EmitPatchEngine:
    """Scripted reasoning engine: emits a search_replace patch immediately."""
    def __init__(self, file: str, search: str, replace: str) -> None:
        self._file = file
        self._search = search
        self._replace = replace

    async def create_tool_step(self, step_context, history, tool_definitions, on_thinking=None, state_description="", allowed_action_types=None):
        in_verify = any(
            isinstance(msg.get("content"), str) and "Patch applied successfully" in msg["content"]
            for msg in history
        )
        if in_verify:
            return {"type": "verify_done", "thought": "done", "verified": True, "test_output": ""}
        return {
            "type": "emit_patch",
            "thought": "patching",
            "patch_ops": [{"op": "search_replace", "file": self._file,
                           "search": self._search, "replace": self._replace, "reason": "r"}],
        }

    async def create_patch(self, *a, **kw):
        return {}

    async def create_planning_step(self, *a, **kw):
        return {}

    async def create_plan(self, *a, **kw):
        return {}


class _AlwaysPassValidator:
    async def run(self, workspace_path): ...
    async def run_touched(self, workspace_path, touched_files): ...


class _NullStore:
    """Minimal store stub that silently absorbs append_message calls."""
    def append_message(self, thread_id: str, message: object) -> None:
        pass


def _make_orchestrator(tmp_path: Path, reasoning_engine) -> AgentOrchestrator:
    return AgentOrchestrator(
        store=InMemoryTaskStore(),
        reasoning_engine=reasoning_engine,
        validator=_AlwaysPassValidator(),
        patch_engine=PatchEngine(),
        workspace_manager=ShadowWorkspaceManager(tmp_path / "shadows"),
    )


@pytest.mark.asyncio
async def test_run_inline_change_broadcasts_diff_ready(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("x = 1\n")

    engine = _EmitPatchEngine("a.py", "x = 1", "x = 2")
    orch = _make_orchestrator(tmp_path, engine)
    queue = orch.broadcaster.subscribe("chat:t1")

    explore_context = [{"tool": "read_file", "args": {"path": "a.py"}, "result": "x = 1"}]
    await orch.run_inline_change(
        thread_id="t1",
        goal="change x to 2",
        workspace_path=str(ws),
        plan_markdown="- change x to 2",
        explore_context=explore_context,
        channel_id="chat:t1",
        store=_NullStore(),
    )

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())

    types = [e["type"] for e in events]
    assert "patch_applied" in types, f"expected patch_applied in {types}"
    assert "diff_ready" in types, f"expected diff_ready in {types}"
    assert "chat_done" in types, f"expected chat_done in {types}"

    diff_event = next(e for e in events if e["type"] == "diff_ready")
    assert diff_event["payload"]["task_id"].startswith("inline-")
    diff_entries = diff_event["payload"]["diff_entries"]
    assert len(diff_entries) == 1
    assert diff_entries[0]["path"] == "a.py"
    assert diff_entries[0]["additions"] > 0 or diff_entries[0]["deletions"] > 0


@pytest.mark.asyncio
async def test_promote_inline_change_writes_to_real_workspace(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("x = 1\n")

    engine = _EmitPatchEngine("a.py", "x = 1", "x = 99")
    orch = _make_orchestrator(tmp_path, engine)
    orch.broadcaster.subscribe("chat:t2")

    explore_context = [{"tool": "read_file", "args": {"path": "a.py"}, "result": "x = 1"}]
    await orch.run_inline_change(
        thread_id="t2",
        goal="x to 99",
        workspace_path=str(ws),
        plan_markdown="",
        explore_context=explore_context,
        channel_id="chat:t2",
        store=_NullStore(),
    )

    # Find the inline_task_id
    assert len(orch._inline_shadows) == 1
    inline_task_id = next(iter(orch._inline_shadows))

    await orch.promote_inline_change(inline_task_id)

    assert (ws / "a.py").read_text() == "x = 99\n"
    assert inline_task_id not in orch._inline_shadows


@pytest.mark.asyncio
async def test_discard_inline_change_removes_shadow(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("x = 1\n")

    engine = _EmitPatchEngine("a.py", "x = 1", "x = 42")
    orch = _make_orchestrator(tmp_path, engine)
    orch.broadcaster.subscribe("chat:t3")

    explore_context = [{"tool": "read_file", "args": {"path": "a.py"}, "result": "x = 1"}]
    await orch.run_inline_change(
        thread_id="t3",
        goal="x to 42",
        workspace_path=str(ws),
        plan_markdown="",
        explore_context=explore_context,
        channel_id="chat:t3",
        store=_NullStore(),
    )

    inline_task_id = next(iter(orch._inline_shadows))
    shadow_path = Path(str(orch._inline_shadows[inline_task_id]["shadow_path"]))
    assert shadow_path.exists()

    await orch.discard_inline_change(inline_task_id)

    assert not shadow_path.exists()
    assert (ws / "a.py").read_text() == "x = 1\n"  # real file unchanged
