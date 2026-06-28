import pytest

from agentd.memory.compactor import Compactor
from agentd.memory.harness import NO_OP_HARNESS, MemoryHarness
from agentd.memory.store import MemoryStore


@pytest.mark.asyncio
async def test_long_run_compacts_and_persists(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite3")

    async def summ(old: str, evicted: str) -> str:
        return (old + " | " if old else "") + f"summarized {len(evicted)} chars"

    comp = Compactor(
        store, summ, window_tokens=200, trigger_frac=0.1, hot_token_frac=0.4, hot_turns=3
    )
    harness = MemoryHarness(enabled=True, compactor=comp)
    history = [{"role": "user", "content": "m" * 50} for _ in range(12)]
    prep = await harness.prepare_turn(history, "run-A")
    assert prep.compacted is True
    assert prep.history[-3:] == history[-3:]  # hot verbatim
    assert len(store.get_segments("run-A")) == 9  # 12 - 3 evicted
    assert store.get_anchor("run-A").version == 1

    history2 = list(prep.history) + [{"role": "user", "content": "n" * 200} for _ in range(6)]
    prep2 = await harness.prepare_turn(history2, "run-A")
    assert prep2.compacted is True
    assert store.get_anchor("run-A").version == 2
    assert "|" in store.get_anchor("run-A").summary_md  # prior anchor carried forward (merge)
    # seq is run-monotonic across both compaction rounds (no collision)
    seqs = [s.seq for s in store.get_segments("run-A")]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))


@pytest.mark.asyncio
async def test_disabled_is_byte_identical():
    history = [{"role": "user", "content": "x" * 9999} for _ in range(50)]
    prep = await NO_OP_HARNESS.prepare_turn(history, "run-A")
    assert prep.history is history and prep.compacted is False
