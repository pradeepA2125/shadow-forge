from __future__ import annotations

import pytest

from agentd.domain.models import PlanStep, PlanTarget, PlanTargetIntent, TaskBudget, TaskUsage
from agentd.memory.compactor import Compactor
from agentd.memory.harness import MemoryHarness
from agentd.memory.store import MemoryStore
from agentd.orchestrator.broadcaster import EventBroadcaster
from agentd.patch.engine import PatchEngine
from agentd.tools.loop import ToolLoop, VerifyResult
from agentd.tools.registry import ToolRegistry


class _EmitPatchEngine:
    async def create_tool_step(
        self, step_context, history, tool_definitions, on_thinking=None,
        state_description="", allowed_action_types=None,
    ):
        if not history:
            return {
                "type": "emit_patch",
                "thought": "patch",
                "patch_ops": [
                    {"op": "search_replace", "file": "a.py", "search": "x",
                     "replace": "y", "reason": "r"}
                ],
            }
        return {"type": "verify_done", "thought": "done", "verified": True, "test_output": ""}

    async def create_patch(self, *args, **kwargs):
        return {}

    async def create_planning_step(self, *args, **kwargs):
        return {}

    async def create_plan(self, *args, **kwargs):
        return {}


@pytest.mark.asyncio
async def test_tool_loop_invokes_harness_with_task_id(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("x = 1\n")

    calls: list[str] = []
    store = MemoryStore(tmp_path / "m.sqlite3")

    async def summ(old: str, evicted: str) -> str:
        return "A"

    class SpyCompactor(Compactor):
        async def maybe_compact(self, history, run_id):
            calls.append(run_id)
            return await super().maybe_compact(history, run_id)

    comp = SpyCompactor(
        store, summ, window_tokens=100000, trigger_frac=0.65, hot_token_frac=0.4, hot_turns=10
    )
    harness = MemoryHarness(enabled=True, compactor=comp)

    registry = ToolRegistry(shadow_root=ws, real_workspace_path=ws)
    loop = ToolLoop(
        _EmitPatchEngine(), registry, EventBroadcaster(), "task-mem",
        patch_engine=PatchEngine(), shadow_path=ws, skip_verify=True,
        memory_harness=harness,
    )
    step = PlanStep(
        id="S1", goal="g",
        targets=[PlanTarget(path="a.py", intent=PlanTargetIntent.EXISTING)],
        risk="low",
    )
    result = await loop.run(step, {}, TaskBudget(), TaskUsage())
    assert isinstance(result, VerifyResult)
    assert calls and all(c == "task-mem" for c in calls)
