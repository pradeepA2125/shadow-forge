import pytest

from agentd.memory.consolidator import Consolidator, make_engine_consolidator
from agentd.memory.embedder import Embedder
from agentd.memory.store import MemoryStore


class _Engine:
    """Stub transport: returns a fixed candidate set from generate_json."""

    async def generate_json(self, *, model, schema_name, schema, system_instructions,
                            user_payload, on_thinking=None):
        return {"memories": [
            {"kind": "semantic", "content": "memory harness lives in agentd/memory",
             "entities": ["agentd/memory"], "importance": 9, "contradicts": None}]}


@pytest.mark.asyncio
async def test_consolidate_via_engine_distill(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite3")
    emb = Embedder(encoder=lambda ts: [[1.0] + [0.0] * 383 for _ in ts])
    con = Consolidator(store, emb, make_engine_consolidator(_Engine(), "m1"))
    n = await con.consolidate("thread-x", "workspace", "/ws", "we built the memory harness", 0, 5)
    assert n == 1
    live = store.get_live_memories("workspace", "/ws")
    assert live[0].entities == ["agentd/memory"] and live[0].importance == 9
    assert live[0].source_seq_lo == 0 and live[0].source_seq_hi == 5  # A+link span
