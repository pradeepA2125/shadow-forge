import asyncio

import pytest

from agentd.memory.harness import MemoryHarness


class _SpyConsolidator:
    def __init__(self):
        self.calls = []

    async def consolidate(self, run_id, scope_kind, scope_id, transcript, seq_lo, seq_hi):
        self.calls.append(run_id)
        return 0


@pytest.mark.asyncio
async def test_schedule_consolidation_runs_for_terminal(tmp_path):
    # Terminal/edit triggers have no compactor — schedule_consolidation must still work.
    spy = _SpyConsolidator()
    harness = MemoryHarness(enabled=True, compactor=None, consolidator=spy,
                            scope_kind="workspace", scope_id="/ws")
    harness.schedule_consolidation("task-1", "workspace", "/ws", "final transcript", None, None)
    await asyncio.sleep(0)
    assert spy.calls == ["task-1"]


@pytest.mark.asyncio
async def test_schedule_consolidation_noop_without_consolidator():
    harness = MemoryHarness(enabled=True, compactor=None, consolidator=None,
                            scope_kind="workspace", scope_id="/ws")
    # must not raise even though there is no consolidator
    harness.schedule_consolidation("task-1", "workspace", "/ws", "t", None, None)
    await asyncio.sleep(0)
