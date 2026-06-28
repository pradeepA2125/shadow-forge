# Memory Harness Phase 2A — Substrate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the storage substrate for cross-session memory — a shared `Embedder` and a `MemoryStore` extended with an embedded, FTS5-indexed, lifecycle-aware `memories` table.

**Architecture:** Extend the existing Phase-1 `MemoryStore` (SQLite at `memory.sqlite3`) with a `memories` table whose vectors live in a co-located **sqlite-vec** virtual table and whose text is mirrored into an **FTS5** table. A new `Embedder` wraps the existing `bge-small` SentenceTransformer (unit-normalized vectors). No consolidation or recall logic yet — this is the data layer Plans 2B/2C build on.

**Tech Stack:** Python 3.13, SQLite (`sqlite3` stdlib), `sqlite-vec` (vec0 virtual tables), FTS5 (built into SQLite), `sentence-transformers` (`BAAI/bge-small-en-v1.5`, 384-dim), pytest + pytest-asyncio.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-28-memory-harness-phase2-recall-design.md`.
- `MemoryStore` is the ONLY DB-aware unit; everyone else speaks model objects.
- Reuse the existing embedding model — default `BAAI/bge-small-en-v1.5`, env `AI_EDITOR_EMBEDDING_MODEL` (do NOT introduce a new model id). Embedding dim = **384**.
- Embeddings are **unit-normalized** at the `Embedder`; the vec table uses default L2 distance (cosine = 1 − L2²/2 for unit vectors). No reliance on vec0 `distance_metric` flags.
- `global` scope is NEVER written in Phase 2 (schema reserves it).
- All new code lints clean under `ruff` (line length 100) and passes `mypy agentd/memory`.
- Tests use real `tmp_path` SQLite, no mocks of the DB or filesystem (matches existing memory tests). The `Embedder` is tested with an injected fake encoder; the real-model path gets one `@pytest.mark.slow` smoke test.
- Run tests from `services/agentd-py` with the venv active: `source .venv/bin/activate`.

---

### Task 1: Dependencies, models, config

**Files:**
- Modify: `services/agentd-py/pyproject.toml` (add a `memory` optional-dependency group)
- Modify: `services/agentd-py/agentd/memory/models.py`
- Modify: `services/agentd-py/agentd/memory/config.py`
- Test: `services/agentd-py/tests/test_memory_models_phase2.py` (create)

**Interfaces:**
- Produces: `Memory` (pydantic) with fields `id, scope_kind, scope_id, kind, content, entities: list[str], importance: int, valid_from: datetime, valid_to: datetime | None, superseded_by: str | None, source_kind, source_ref, source_seq_lo: int | None, source_seq_hi: int | None, created_at: datetime`. `CandidateMemory` (pydantic) with `kind, content, entities: list[str], importance: int, contradicts: str | None`. `MemoryConfig` gains `dedup_threshold: float, recall_token_budget: int, weights: tuple[float, float, float], graph_grounding: bool, embedding_model: str`.

- [ ] **Step 1: Write the failing test**

```python
# services/agentd-py/tests/test_memory_models_phase2.py
from datetime import UTC, datetime

from agentd.memory.config import MemoryConfig
from agentd.memory.models import CandidateMemory, Memory


def test_memory_model_roundtrips_all_fields():
    m = Memory(
        id="m1", scope_kind="workspace", scope_id="/ws", kind="semantic",
        content="patch ops apply in patch/engine.py", entities=["patch/engine.py"],
        importance=8, valid_from=datetime(2026, 6, 28, tzinfo=UTC), valid_to=None,
        superseded_by=None, source_kind="consolidation", source_ref="thread-x",
        source_seq_lo=0, source_seq_hi=8, created_at=datetime(2026, 6, 28, tzinfo=UTC),
    )
    assert m.kind == "semantic" and m.valid_to is None and m.entities == ["patch/engine.py"]


def test_candidate_memory_defaults_contradicts_none():
    c = CandidateMemory(kind="episodic", content="user rejected plan", entities=[], importance=5)
    assert c.contradicts is None


def test_memory_config_phase2_defaults():
    cfg = MemoryConfig.from_env({})
    assert cfg.dedup_threshold == 0.92
    assert cfg.recall_token_budget == 1500
    assert cfg.weights == (0.5, 0.3, 0.2)
    assert cfg.graph_grounding is True
    assert cfg.embedding_model == "BAAI/bge-small-en-v1.5"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_memory_models_phase2.py -v`
Expected: FAIL — `ImportError: cannot import name 'Memory'` / `CandidateMemory`, and `MemoryConfig` missing attrs.

- [ ] **Step 3: Add the models**

Append to `services/agentd-py/agentd/memory/models.py`:

```python
class Memory(BaseModel):
    """A distilled, retrievable long-term memory (L3 / durable L2)."""

    id: str
    scope_kind: str           # 'workspace' | 'thread' | 'global' (global unwritten in P2)
    scope_id: str
    kind: str                 # 'episodic' | 'semantic' | 'procedural'
    content: str
    entities: list[str]
    importance: int           # LLM-rated salience 1-10
    valid_from: datetime      # event time
    valid_to: datetime | None # None = currently true
    superseded_by: str | None
    source_kind: str          # 'consolidation' | 'agent_tool'
    source_ref: str
    source_seq_lo: int | None  # A+link span into compaction_segments
    source_seq_hi: int | None
    created_at: datetime      # ingestion time


class CandidateMemory(BaseModel):
    """What the consolidator LLM proposes — content-level fields only; Python assigns the rest."""

    kind: str
    content: str
    entities: list[str]
    importance: int
    contradicts: str | None = None
```

- [ ] **Step 4: Extend the config**

In `services/agentd-py/agentd/memory/config.py`, add to the `MemoryConfig` class body:

```python
    dedup_threshold: float
    recall_token_budget: int
    weights: tuple[float, float, float]
    graph_grounding: bool
    embedding_model: str
```

and to `from_env`'s `cls(...)` call add:

```python
            dedup_threshold=float(env.get("AI_EDITOR_MEMORY_DEDUP_THRESHOLD", "0.92")),
            recall_token_budget=int(env.get("AI_EDITOR_MEMORY_RECALL_TOKEN_BUDGET", "1500")),
            weights=tuple(  # type: ignore[arg-type]
                float(x) for x in env.get("AI_EDITOR_MEMORY_WEIGHTS", "0.5,0.3,0.2").split(",")
            ),
            graph_grounding=env.get("AI_EDITOR_MEMORY_GRAPH_GROUNDING", "true").lower() in _TRUTHY,
            embedding_model=env.get("AI_EDITOR_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5"),
```

- [ ] **Step 5: Add the dependency group**

In `services/agentd-py/pyproject.toml` under `[project.optional-dependencies]`, add (follow the existing `semantic` group's style):

```toml
memory = ["sqlite-vec>=0.1.6", "sentence-transformers>=2.2.0", "numpy>=1.24"]
```

Then install: `pip install -e '.[memory]'`

- [ ] **Step 6: Run test to verify it passes**

Run: `python -m pytest tests/test_memory_models_phase2.py -v`
Expected: PASS (3 tests).

- [ ] **Step 7: Commit**

```bash
git add services/agentd-py/agentd/memory/models.py services/agentd-py/agentd/memory/config.py services/agentd-py/pyproject.toml services/agentd-py/tests/test_memory_models_phase2.py
git commit -m "feat(memory): Phase 2 models + config + sqlite-vec dep"
```

---

### Task 2: `Embedder` seam

**Files:**
- Create: `services/agentd-py/agentd/memory/embedder.py`
- Test: `services/agentd-py/tests/test_memory_embedder.py` (create)

**Interfaces:**
- Produces: `Embedder(model_name: str = "BAAI/bge-small-en-v1.5", *, encoder=None)`. `.embed(texts: list[str]) -> list[list[float]]` returns **unit-normalized** 384-dim vectors. `.dim -> int` (384). `.available -> bool` (False if sentence-transformers/model missing). When unavailable, `.embed` returns `[]`. `encoder` is an injectable `Callable[[list[str]], list[list[float]]]` for tests (bypasses model load).

- [ ] **Step 1: Write the failing test**

```python
# services/agentd-py/tests/test_memory_embedder.py
import math

from agentd.memory.embedder import Embedder


def test_embed_unit_normalizes_injected_vectors():
    emb = Embedder(encoder=lambda texts: [[3.0, 4.0] for _ in texts])  # |[3,4]| = 5
    out = emb.embed(["a", "b"])
    assert len(out) == 2
    assert math.isclose(out[0][0], 0.6) and math.isclose(out[0][1], 0.8)
    assert math.isclose(math.hypot(*out[0]), 1.0)


def test_embed_empty_input_returns_empty():
    emb = Embedder(encoder=lambda texts: [[1.0, 0.0] for _ in texts])
    assert emb.embed([]) == []


def test_unavailable_embedder_returns_empty_and_flags():
    def boom(texts):
        raise RuntimeError("model missing")

    emb = Embedder(encoder=boom)
    assert emb.available is False
    assert emb.embed(["x"]) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_memory_embedder.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agentd.memory.embedder'`.

- [ ] **Step 3: Write the implementation**

```python
# services/agentd-py/agentd/memory/embedder.py
from __future__ import annotations

import logging
import math
from collections.abc import Callable

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
_DIM = 384


def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return v if n == 0 else [x / n for x in v]


class Embedder:
    """Shared bge-small wrapper. Returns unit-normalized vectors so the vec0 L2 store
    ranks identically to cosine. `encoder` is injectable for tests."""

    def __init__(
        self, model_name: str = _DEFAULT_MODEL, *,
        encoder: Callable[[list[str]], list[list[float]]] | None = None,
    ) -> None:
        self._model_name = model_name
        self._encoder = encoder
        self._available = True
        self._model = None  # lazy SentenceTransformer

    @property
    def dim(self) -> int:
        return _DIM

    @property
    def available(self) -> bool:
        # Probe once: a failing encode flips availability off.
        if self._encoder is not None:
            try:
                self._encoder(["probe"])
            except Exception:  # noqa: BLE001
                self._available = False
            return self._available
        return self._available

    def _encode(self, texts: list[str]) -> list[list[float]]:
        if self._encoder is not None:
            return self._encoder(texts)
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # type: ignore[import-untyped]
            self._model = SentenceTransformer(self._model_name)
        return [list(map(float, row)) for row in self._model.encode(texts)]

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            return [_unit(v) for v in self._encode(texts)]
        except Exception:  # noqa: BLE001 — degrade: recall/consolidation handle empty embeddings
            logger.warning("[memory] embedder unavailable for model=%s", self._model_name)
            self._available = False
            return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_memory_embedder.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add services/agentd-py/agentd/memory/embedder.py services/agentd-py/tests/test_memory_embedder.py
git commit -m "feat(memory): shared Embedder seam (unit-normalized bge-small)"
```

---

### Task 3: `MemoryStore` schema — memories + sqlite-vec + FTS5

**Files:**
- Modify: `services/agentd-py/agentd/memory/store.py`
- Test: `services/agentd-py/tests/test_memory_store_phase2.py` (create)

**Interfaces:**
- Consumes: existing `MemoryStore.__init__`.
- Produces: `MemoryStore` now loads sqlite-vec in `__init__` and creates `memories`, `vec_memories` (vec0, `float[384]`), and `memories_fts` (FTS5) tables. New attribute behavior only; methods added in Tasks 4-6.

- [ ] **Step 1: Write the failing test**

```python
# services/agentd-py/tests/test_memory_store_phase2.py
import sqlite_vec  # noqa: F401  — ensures the dep is installed

from agentd.memory.store import MemoryStore


def test_phase2_tables_created(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite3")
    names = {
        r["name"]
        for r in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        )
    }
    assert {"memories", "vec_memories", "memories_fts"} <= names


def test_sqlite_vec_loaded(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite3")
    # vec_version() only resolves when the extension is loaded.
    row = store._conn.execute("SELECT vec_version() AS v").fetchone()
    assert row["v"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_memory_store_phase2.py -v`
Expected: FAIL — `vec_version()` unknown / `memories` table absent.

- [ ] **Step 3: Load sqlite-vec and extend the schema**

In `services/agentd-py/agentd/memory/store.py`, add `import sqlite_vec` at the top. Replace the `__init__` body's connection setup so it loads the extension before running the schema:

```python
    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
```

Extend `_SCHEMA` (append before the closing `"""`):

```python
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    scope_kind TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    entities TEXT NOT NULL,          -- JSON array
    importance INTEGER NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,                   -- NULL = live
    superseded_by TEXT,
    source_kind TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    source_seq_lo INTEGER,
    source_seq_hi INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope_kind, scope_id, valid_to);
CREATE VIRTUAL TABLE IF NOT EXISTS vec_memories USING vec0(
    memory_id TEXT PRIMARY KEY,
    embedding float[384]
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    memory_id UNINDEXED, content, entities
);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_memory_store_phase2.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Run the Phase-1 store tests to confirm no regression**

Run: `python -m pytest tests/test_memory_store.py -v`
Expected: PASS (all existing).

- [ ] **Step 6: Commit**

```bash
git add services/agentd-py/agentd/memory/store.py services/agentd-py/tests/test_memory_store_phase2.py
git commit -m "feat(memory): memories + sqlite-vec + FTS5 schema in MemoryStore"
```

---

### Task 4: `insert_memory` / `get_memory` (atomic across 3 tables)

**Files:**
- Modify: `services/agentd-py/agentd/memory/store.py`
- Test: `services/agentd-py/tests/test_memory_store_phase2.py` (extend)

**Interfaces:**
- Consumes: `Memory` (Task 1), the schema (Task 3).
- Produces: `MemoryStore.insert_memory(self, memory: Memory, embedding: list[float]) -> None` — one transaction writing `memories` + `memories_fts` + `vec_memories`. `MemoryStore.get_memory(self, memory_id: str) -> Memory | None`. An empty `embedding` (`[]`) inserts the row + FTS but skips the vec row (degraded-embedder path).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_memory_store_phase2.py
import json
from datetime import UTC, datetime

from agentd.memory.models import Memory


def _mem(mid="m1", content="patch ops in patch/engine.py", entities=("patch/engine.py",)):
    now = datetime(2026, 6, 28, tzinfo=UTC)
    return Memory(
        id=mid, scope_kind="workspace", scope_id="/ws", kind="semantic", content=content,
        entities=list(entities), importance=7, valid_from=now, valid_to=None, superseded_by=None,
        source_kind="consolidation", source_ref="thread-x", source_seq_lo=0, source_seq_hi=8,
        created_at=now,
    )


def test_insert_and_get_memory_roundtrip(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite3")
    store.insert_memory(_mem(), [0.1] * 384)
    got = store.get_memory("m1")
    assert got is not None and got.content == "patch ops in patch/engine.py"
    assert got.entities == ["patch/engine.py"] and got.importance == 7
    # FTS + vec rows landed
    fts = store._conn.execute("SELECT count(*) c FROM memories_fts WHERE memory_id='m1'").fetchone()
    vec = store._conn.execute("SELECT count(*) c FROM vec_memories WHERE memory_id='m1'").fetchone()
    assert fts["c"] == 1 and vec["c"] == 1


def test_insert_with_empty_embedding_skips_vec(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite3")
    store.insert_memory(_mem("m2"), [])
    vec = store._conn.execute("SELECT count(*) c FROM vec_memories WHERE memory_id='m2'").fetchone()
    assert vec["c"] == 0
    assert store.get_memory("m2") is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_memory_store_phase2.py -k "roundtrip or empty_embedding" -v`
Expected: FAIL — `AttributeError: 'MemoryStore' object has no attribute 'insert_memory'`.

- [ ] **Step 3: Write the implementation**

Add to `MemoryStore` in `store.py` (add `import json` at top):

```python
    def insert_memory(self, memory: Memory, embedding: list[float]) -> None:
        m = memory
        with self._conn:  # one transaction across the 3 tables
            self._conn.execute(
                "INSERT INTO memories (id, scope_kind, scope_id, kind, content, entities, "
                "importance, valid_from, valid_to, superseded_by, source_kind, source_ref, "
                "source_seq_lo, source_seq_hi, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    m.id, m.scope_kind, m.scope_id, m.kind, m.content, json.dumps(m.entities),
                    m.importance, m.valid_from.isoformat(),
                    m.valid_to.isoformat() if m.valid_to else None, m.superseded_by,
                    m.source_kind, m.source_ref, m.source_seq_lo, m.source_seq_hi,
                    m.created_at.isoformat(),
                ),
            )
            self._conn.execute(
                "INSERT INTO memories_fts (memory_id, content, entities) VALUES (?,?,?)",
                (m.id, m.content, " ".join(m.entities)),
            )
            if embedding:
                self._conn.execute(
                    "INSERT INTO vec_memories (memory_id, embedding) VALUES (?, ?)",
                    (m.id, sqlite_vec.serialize_float32(embedding)),
                )

    def get_memory(self, memory_id: str) -> Memory | None:
        r = self._conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        return self._row_to_memory(r) if r else None

    @staticmethod
    def _row_to_memory(r: sqlite3.Row) -> Memory:
        return Memory(
            id=r["id"], scope_kind=r["scope_kind"], scope_id=r["scope_id"], kind=r["kind"],
            content=r["content"], entities=json.loads(r["entities"]), importance=r["importance"],
            valid_from=datetime.fromisoformat(r["valid_from"]),
            valid_to=datetime.fromisoformat(r["valid_to"]) if r["valid_to"] else None,
            superseded_by=r["superseded_by"], source_kind=r["source_kind"],
            source_ref=r["source_ref"], source_seq_lo=r["source_seq_lo"],
            source_seq_hi=r["source_seq_hi"], created_at=datetime.fromisoformat(r["created_at"]),
        )
```

Add `from agentd.memory.models import AnchoredSummary, CompactionSegment, Memory` (extend the existing import).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_memory_store_phase2.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add services/agentd-py/agentd/memory/store.py services/agentd-py/tests/test_memory_store_phase2.py
git commit -m "feat(memory): insert_memory/get_memory atomic across rows+fts+vec"
```

---

### Task 5: query methods — live filter, semantic, lexical, similar

**Files:**
- Modify: `services/agentd-py/agentd/memory/store.py`
- Test: `services/agentd-py/tests/test_memory_store_phase2.py` (extend)

**Interfaces:**
- Consumes: `insert_memory` (Task 4).
- Produces:
  - `get_live_memories(scope_kind: str, scope_id: str) -> list[Memory]` — `valid_to IS NULL` + scope.
  - `search_semantic(query_embedding: list[float], k: int, scope_kind: str, scope_id: str) -> list[tuple[str, float]]` — `(memory_id, l2_distance)`, live + scope, nearest first.
  - `search_lexical(query: str, k: int, scope_kind: str, scope_id: str) -> list[tuple[str, float]]` — `(memory_id, bm25_rank)`, live + scope (bm25 lower = better).
  - `similar_memories(embedding: list[float], kind: str, scope_kind: str, scope_id: str, k: int) -> list[tuple[Memory, float]]` — top-k live same-kind+scope by L2 distance (dedup context for 2B).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_memory_store_phase2.py
def test_get_live_memories_filters_scope_and_validity(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite3")
    store.insert_memory(_mem("live"), [0.1] * 384)
    retired = _mem("dead")
    retired = retired.model_copy(update={"valid_to": retired.valid_from})
    store.insert_memory(retired, [0.1] * 384)
    other = _mem("other").model_copy(update={"scope_id": "/elsewhere"})
    store.insert_memory(other, [0.1] * 384)
    live = store.get_live_memories("workspace", "/ws")
    assert {m.id for m in live} == {"live"}


def test_search_semantic_orders_by_distance(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite3")
    near = [1.0] + [0.0] * 383
    far = [0.0, 1.0] + [0.0] * 382
    store.insert_memory(_mem("near"), near)
    store.insert_memory(_mem("far"), far)
    hits = store.search_semantic(near, k=2, scope_kind="workspace", scope_id="/ws")
    assert hits[0][0] == "near"  # closest first


def test_search_lexical_matches_entities(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite3")
    store.insert_memory(_mem("a", content="auth flow lives here", entities=("src/auth.py",)), [0.1] * 384)
    store.insert_memory(_mem("b", content="tax compute", entities=("src/tax.py",)), [0.1] * 384)
    hits = store.search_lexical("auth", k=5, scope_kind="workspace", scope_id="/ws")
    assert hits and hits[0][0] == "a"


def test_similar_memories_same_kind_scope(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite3")
    store.insert_memory(_mem("s1"), [1.0] + [0.0] * 383)
    out = store.similar_memories([1.0] + [0.0] * 383, kind="semantic",
                                 scope_kind="workspace", scope_id="/ws", k=3)
    assert out and out[0][0].id == "s1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_memory_store_phase2.py -k "live_memories or semantic or lexical or similar" -v`
Expected: FAIL — methods not defined.

- [ ] **Step 3: Write the implementation**

Add to `MemoryStore`:

```python
    def get_live_memories(self, scope_kind: str, scope_id: str) -> list[Memory]:
        rows = self._conn.execute(
            "SELECT * FROM memories WHERE valid_to IS NULL AND scope_kind=? AND scope_id=?",
            (scope_kind, scope_id),
        ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    def search_semantic(
        self, query_embedding: list[float], k: int, scope_kind: str, scope_id: str
    ) -> list[tuple[str, float]]:
        if not query_embedding:
            return []
        rows = self._conn.execute(
            "SELECT v.memory_id AS mid, v.distance AS dist "
            "FROM vec_memories v JOIN memories m ON m.id = v.memory_id "
            "WHERE v.embedding MATCH ? AND m.valid_to IS NULL "
            "AND m.scope_kind=? AND m.scope_id=? ORDER BY v.distance LIMIT ?",
            (sqlite_vec.serialize_float32(query_embedding), scope_kind, scope_id, k),
        ).fetchall()
        return [(r["mid"], r["dist"]) for r in rows]

    def search_lexical(
        self, query: str, k: int, scope_kind: str, scope_id: str
    ) -> list[tuple[str, float]]:
        rows = self._conn.execute(
            "SELECT f.memory_id AS mid, bm25(memories_fts) AS rank "
            "FROM memories_fts f JOIN memories m ON m.id = f.memory_id "
            "WHERE memories_fts MATCH ? AND m.valid_to IS NULL "
            "AND m.scope_kind=? AND m.scope_id=? ORDER BY rank LIMIT ?",
            (query, scope_kind, scope_id, k),
        ).fetchall()
        return [(r["mid"], r["rank"]) for r in rows]

    def similar_memories(
        self, embedding: list[float], kind: str, scope_kind: str, scope_id: str, k: int
    ) -> list[tuple[Memory, float]]:
        if not embedding:
            return []
        rows = self._conn.execute(
            "SELECT m.*, v.distance AS dist "
            "FROM vec_memories v JOIN memories m ON m.id = v.memory_id "
            "WHERE v.embedding MATCH ? AND m.valid_to IS NULL AND m.kind=? "
            "AND m.scope_kind=? AND m.scope_id=? ORDER BY v.distance LIMIT ?",
            (sqlite_vec.serialize_float32(embedding), kind, scope_kind, scope_id, k),
        ).fetchall()
        return [(self._row_to_memory(r), r["dist"]) for r in rows]
```

Note: vec0 KNN requires the `MATCH` + `ORDER BY distance LIMIT k` form shown; the join filters to live + scope after the ANN.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_memory_store_phase2.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add services/agentd-py/agentd/memory/store.py services/agentd-py/tests/test_memory_store_phase2.py
git commit -m "feat(memory): live/semantic/lexical/similar query methods"
```

---

### Task 6: supersede transaction

**Files:**
- Modify: `services/agentd-py/agentd/memory/store.py`
- Test: `services/agentd-py/tests/test_memory_store_phase2.py` (extend)

**Interfaces:**
- Consumes: `insert_memory` (Task 4), `get_memory` (Task 4).
- Produces: `MemoryStore.supersede(old_id: str, new_memory: Memory, new_embedding: list[float]) -> None` — one transaction that sets the old row's `valid_to=new_memory.valid_from` and `superseded_by=new_memory.id`, then inserts the new memory (row + FTS + vec). After it, `get_memory(old_id).valid_to` is set and `superseded_by == new.id`; the new row is live.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_memory_store_phase2.py
def test_supersede_retires_old_and_inserts_new_atomically(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite3")
    store.insert_memory(_mem("old", content="uses openai embeddings"), [0.1] * 384)
    new = _mem("new", content="uses bge-small embeddings")
    store.supersede("old", new, [0.2] * 384)

    old = store.get_memory("old")
    assert old is not None and old.valid_to is not None and old.superseded_by == "new"
    live = store.get_live_memories("workspace", "/ws")
    assert {m.id for m in live} == {"new"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_memory_store_phase2.py -k supersede -v`
Expected: FAIL — `supersede` not defined.

- [ ] **Step 3: Write the implementation**

Add to `MemoryStore`:

```python
    def supersede(self, old_id: str, new_memory: Memory, new_embedding: list[float]) -> None:
        with self._conn:  # atomic: retire old + insert new together
            self._conn.execute(
                "UPDATE memories SET valid_to=?, superseded_by=? WHERE id=?",
                (new_memory.valid_from.isoformat(), new_memory.id, old_id),
            )
            self.insert_memory(new_memory, new_embedding)
```

Note: `insert_memory` opens its own `with self._conn`; nested `with` on the same sqlite3 connection is a no-op savepoint-wise but commits once at the outer exit — acceptable here because both run on one connection in one process. If a test reveals double-commit issues, inline the inserts instead of calling `insert_memory`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_memory_store_phase2.py -v`
Expected: PASS (all).

- [ ] **Step 5: Run the full memory suite + lint + types**

Run:
```bash
python -m pytest tests/ -k memory -q
ruff check agentd/memory/
mypy agentd/memory
```
Expected: all pass; ruff clean; mypy clean for `agentd/memory`.

- [ ] **Step 6: Commit**

```bash
git add services/agentd-py/agentd/memory/store.py services/agentd-py/tests/test_memory_store_phase2.py
git commit -m "feat(memory): atomic supersede transaction"
```

---

## Self-Review

**Spec coverage (§1 data model + substrate):**
- `memories` table with all spec fields incl. `importance` (D1) + bitemporal `valid_from`/`created_at` (D3) + A+link `source_seq_lo/hi` → Task 1 (model) + Task 3 (schema).
- sqlite-vec embedding + FTS5 mirror → Task 3.
- Shared `Embedder` reusing bge-small → Task 2.
- Live/scope/`valid_to` filtering → Task 5. Top-K-similar dedup context (D2) → Task 5 `similar_memories`.
- Supersede txn (close window + insert new, atomic) → Task 6.
- Config env vars (dedup threshold, weights, recall budget, graph grounding, embedding model) → Task 1.
- Deferred to 2B/2C (correctly absent here): consolidation, recall scoring/fusion, injection, graph grounding, tools, prompt.

**Placeholder scan:** none — every step carries runnable code/commands.

**Type consistency:** `Memory`/`CandidateMemory` fields used identically across Tasks 1/4/5/6; `insert_memory(memory, embedding)`, `search_semantic`/`search_lexical`/`similar_memories` signatures match their consumers' descriptions; `sqlite_vec.serialize_float32` used consistently for all vec writes/queries.

**Note for implementer:** if the installed `sqlite-vec` version rejects the `embedding MATCH ? ORDER BY distance LIMIT ?` KNN form, switch to the `WHERE embedding MATCH ? AND k = ?` form documented for that version — verify against the installed `sqlite-vec` docs before adjusting tests.
