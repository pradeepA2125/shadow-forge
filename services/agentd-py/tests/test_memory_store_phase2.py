import json
from datetime import UTC, datetime

import sqlite_vec  # noqa: F401  — ensures the dep is installed

from agentd.memory.models import Memory
from agentd.memory.store import MemoryStore


def _mem(mid="m1", content="patch ops in patch/engine.py", entities=("patch/engine.py",)):
    now = datetime(2026, 6, 28, tzinfo=UTC)
    return Memory(
        id=mid, scope_kind="workspace", scope_id="/ws", kind="semantic", content=content,
        entities=list(entities), importance=7, valid_from=now, valid_to=None, superseded_by=None,
        source_kind="consolidation", source_ref="thread-x", source_seq_lo=0, source_seq_hi=8,
        created_at=now,
    )


def test_phase2_tables_created(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite3")
    names = {
        r["name"]
        for r in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        )
    }
    assert {"memories", "memories_fts"} <= names  # always
    if store._vec_enabled:
        assert "vec_memories" in names


def test_vec_enabled_is_bool(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite3")
    assert isinstance(store._vec_enabled, bool)
    if store._vec_enabled:
        assert store._conn.execute("SELECT vec_version() AS v").fetchone()["v"]


def test_store_survives_without_vec(tmp_path, monkeypatch):
    # FIX #1: a sqlite-vec/extension failure must NOT crash the store (Phase 1 uses it too).
    import agentd.memory.store as store_mod

    monkeypatch.setattr(
        store_mod.sqlite_vec, "load",
        lambda c: (_ for _ in ()).throw(RuntimeError("no ext")),
    )
    store = MemoryStore(tmp_path / "m.sqlite3")  # must NOT raise
    assert store._vec_enabled is False
