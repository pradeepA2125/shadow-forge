import pytest

from agentd.memory.embedder import Embedder
from agentd.memory.recall import RecallEngine
from agentd.memory.store import MemoryStore
from tests.test_memory_store_phase2 import _mem


def _engine(store):
    emb = Embedder(encoder=lambda ts: [[1.0] + [0.0] * 383 for _ in ts])
    return RecallEngine(store, emb, weights=(0.5, 0.3, 0.2), min_score=0.0)


@pytest.mark.asyncio
async def test_grounding_appended_best_effort(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite3")
    emb = Embedder(encoder=lambda ts: [[1.0] + [0.0] * 383 for _ in ts])
    store.insert_memory(_mem("a", content="patch ops", entities=("patch/engine.py",)),
                        emb.embed(["patch ops"])[0])
    eng = RecallEngine(store, emb, weights=(0.5, 0.3, 0.2), min_score=0.0)
    grounded = await eng.recall_grounded("patch", "workspace", "/ws", k=1,
                                         ground=lambda entity: f"callers of {entity}")
    assert "grounding" in grounded[0].lower()


@pytest.mark.asyncio
async def test_grounding_swallows_errors(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite3")
    emb = Embedder(encoder=lambda ts: [[1.0] + [0.0] * 383 for _ in ts])
    store.insert_memory(_mem("a", content="patch ops", entities=("patch/engine.py",)),
                        emb.embed(["patch ops"])[0])
    eng = RecallEngine(store, emb, weights=(0.5, 0.3, 0.2), min_score=0.0)

    def boom(entity):
        raise RuntimeError("no snapshot")

    out = await eng.recall_grounded("patch", "workspace", "/ws", k=1, ground=boom)
    assert out  # still returns the memory line, grounding skipped


@pytest.mark.asyncio
async def test_grounding_none_is_plain(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite3")
    eng = _engine(store)
    emb = Embedder(encoder=lambda ts: [[1.0] + [0.0] * 383 for _ in ts])
    store.insert_memory(_mem("a", content="x", entities=("p.py",)), emb.embed(["x"])[0])
    out = await eng.recall_grounded("x", "workspace", "/ws", k=1, ground=None)
    assert out and "grounding" not in out[0].lower()
