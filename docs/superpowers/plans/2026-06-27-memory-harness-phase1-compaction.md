# Memory Harness — Phase 1 (Within-Run Compaction) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the `ControllerLoop` and task `ToolLoop` from degrading when a run outgrows ~65% of the context window, by keeping a **token-bounded** set of recent turns verbatim, folding everything older into a merged "anchored summary" (one LLM call, never regenerated), and persisting the evicted raw turns to SQLite for later (Phase 2) recall.

**Architecture:** A new flag-gated `agentd/memory/` subpackage. `MemoryHarness` is a façade injected into both loops; each iteration the loop calls `harness.prepare_turn(history, run_id)`, which delegates to a `Compactor`. The compactor keeps the newest turns that fit a token budget (`hot_frac × window`, default 0.4; `hot_turns` is a secondary count cap), folds the rest into a per-run anchored summary, and persists the evicted raw turns as `compaction_segments`. Because `hot_frac < trigger_frac (0.65)`, compaction provably reduces the window. Recall is a Phase-2 no-op stub here. The subsystem is off unless `AI_EDITOR_MEMORY_ENABLED` is truthy.

**Tech Stack:** Python 3.13, Pydantic, stdlib `sqlite3`, pytest + pytest-asyncio. Reuses the existing `ScriptedReasoningEngine` testing pattern and the `_finalize_task_narrative` best-effort async pattern.

## Global Constraints

- Python target: 3.13. Use `asyncio.run(...)` or `@pytest.mark.asyncio`, never `get_event_loop().run_until_complete`.
- Strict typing: no `any`, explicit return types. Mirror existing `agentd/` style. All imports at top.
- The harness is **best-effort**: no compaction/store failure may propagate out of a loop iteration. On any internal failure, leave history untouched (or hard-truncate) and continue.
- Master kill switch `AI_EDITOR_MEMORY_ENABLED` (default **off**). When off, `MemoryHarness` is a no-op pass-through and both loops behave byte-identically to today.
- New DB path env `AI_EDITOR_MEMORY_DB_PATH` (default `.agentd/memory.sqlite3`). Separate file from task/chat DBs. Phase 1 creates only `compaction_segments` + `anchored_summaries`.
- **Hot set is token-bounded, not count-bounded.** Keep newest turns that fit `MEMORY_HOT_TOKEN_FRAC × window` (default 0.4), capped at `MEMORY_HOT_TURNS` (default 10). `hot_frac (0.4) < trigger_frac (0.65)` guarantees eviction frees space once triggered. Always keep ≥1 turn; if the single newest turn alone exceeds the hot budget, truncate its in-window copy (head + `…[truncated]…` + tail) and persist the full original as a segment. This is what handles "history ≤ hot_turns but already over budget" and "one giant turn > window".
- **No `tier` (warm/cold) label is written.** Tiering is a Phase-2 read-time concern; Phase 1 persists evicted turns as plain segments.
- **`seq` is run-monotonic, not per-batch.** A run can compact many times; each segment's `seq` continues from the run's current max (`store.next_seq(run_id)`) so ordering is stable across compaction rounds (a per-batch `seq=i` would collide round-over-round and corrupt `get_segments` ordering for Phase-2 recall).
- **Segment granularity is 1 message = 1 segment, verbatim (Phase 1).** No size target / chunking — segments are write-only here (never retrieved until Phase 2), so lossless raw preservation is all that's needed. Token-target chunking is a Phase-2 decision (captured in the spec's open questions).
- Phase-1 simplification (decided, document in code): Phase 1 folds **all** evicted history into the anchor — no information cliff before recall exists.
- Default tuning constants (env-overridable): `MEMORY_COMPACT_TRIGGER_FRAC=0.65`, `MEMORY_HOT_TOKEN_FRAC=0.4`, `MEMORY_HOT_TURNS=10`, `MEMORY_WINDOW_TOKENS=128000`.
- Run the suite with `pytest` and read the actual `FAILED`/summary lines — never trust a piped exit code.

---

## File Structure

- `agentd/memory/__init__.py` — exports `MemoryHarness`, `build_memory_harness`, `NO_OP_HARNESS`.
- `agentd/memory/models.py` — `MemoryKind`, `CompactionSegment`, `AnchoredSummary`, `CompactionResult`, `TurnPreparation`.
- `agentd/memory/config.py` — `MemoryConfig` + `from_env`.
- `agentd/memory/store.py` — `MemoryStore` (SQLite: `compaction_segments`, `anchored_summaries`).
- `agentd/memory/compactor.py` — `Compactor`, `estimate_tokens`, `_select_hot`, `_truncate_to_tokens`, `AnchorSummarizer`, `make_engine_summarizer`.
- `agentd/memory/harness.py` — `MemoryHarness`, `build_memory_harness`, `NO_OP_HARNESS`.
- `agentd/chat/controller_loop.py` — MODIFY: inject + call harness at top of `_iterate`.
- `agentd/tools/loop.py` — MODIFY: inject + call harness at top of the iteration loop.
- Tests under `tests/memory/`.

---

### Task 1: Subpackage scaffold — models + config

**Files:**
- Create: `agentd/memory/__init__.py`, `agentd/memory/models.py`, `agentd/memory/config.py`
- Test: `tests/memory/test_config.py`

**Interfaces:**
- Produces:
  - `MemoryKind(str, Enum)` = `EPISODIC|SEMANTIC|PROCEDURAL` (defined now for Phase-2 forward-compat).
  - `CompactionSegment(BaseModel)`: `id: str, run_id: str, seq: int, content: str, created_at: datetime`.
  - `AnchoredSummary(BaseModel)`: `run_id: str, summary_md: str, version: int, updated_at: datetime`.
  - `CompactionResult(BaseModel)`: `compacted: bool, history: list[dict[str, object]], anchor: str | None = None, degraded: bool = False`.
  - `TurnPreparation(BaseModel)`: `history: list[dict[str, object]], recalled_memories: list[dict[str, object]] = [], compacted: bool = False`.
  - `MemoryConfig(BaseModel)`: `enabled: bool, db_path: str, trigger_frac: float, hot_token_frac: float, hot_turns: int, window_tokens: int`; classmethod `from_env(env: Mapping[str,str]) -> MemoryConfig`.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_config.py
from agentd.memory.config import MemoryConfig

def test_from_env_defaults_disabled():
    cfg = MemoryConfig.from_env({})
    assert cfg.enabled is False
    assert cfg.db_path.endswith("memory.sqlite3")
    assert cfg.trigger_frac == 0.65
    assert cfg.hot_token_frac == 0.4
    assert cfg.hot_turns == 10
    assert cfg.window_tokens == 128000

def test_from_env_overrides():
    cfg = MemoryConfig.from_env({
        "AI_EDITOR_MEMORY_ENABLED": "1",
        "AI_EDITOR_MEMORY_DB_PATH": "/tmp/m.sqlite3",
        "AI_EDITOR_MEMORY_COMPACT_TRIGGER_FRAC": "0.5",
        "AI_EDITOR_MEMORY_HOT_TOKEN_FRAC": "0.25",
        "AI_EDITOR_MEMORY_HOT_TURNS": "4",
        "AI_EDITOR_MEMORY_WINDOW_TOKENS": "8000",
    })
    assert cfg.enabled is True
    assert cfg.db_path == "/tmp/m.sqlite3"
    assert cfg.trigger_frac == 0.5
    assert cfg.hot_token_frac == 0.25
    assert cfg.hot_turns == 4
    assert cfg.window_tokens == 8000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/agentd-py && pytest tests/memory/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agentd.memory'`

- [ ] **Step 3: Write minimal implementation**

```python
# agentd/memory/models.py
from __future__ import annotations
from datetime import datetime
from enum import Enum
from pydantic import BaseModel, Field

class MemoryKind(str, Enum):
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"

class CompactionSegment(BaseModel):
    id: str
    run_id: str
    seq: int
    content: str
    created_at: datetime

class AnchoredSummary(BaseModel):
    run_id: str
    summary_md: str
    version: int
    updated_at: datetime

class CompactionResult(BaseModel):
    compacted: bool
    history: list[dict[str, object]]
    anchor: str | None = None
    degraded: bool = False

class TurnPreparation(BaseModel):
    history: list[dict[str, object]]
    recalled_memories: list[dict[str, object]] = Field(default_factory=list)
    compacted: bool = False
```

```python
# agentd/memory/config.py
from __future__ import annotations
from collections.abc import Mapping
from pydantic import BaseModel

_TRUTHY = {"1", "true", "yes", "on"}

class MemoryConfig(BaseModel):
    enabled: bool
    db_path: str
    trigger_frac: float
    hot_token_frac: float
    hot_turns: int
    window_tokens: int

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "MemoryConfig":
        return cls(
            enabled=env.get("AI_EDITOR_MEMORY_ENABLED", "").lower() in _TRUTHY,
            db_path=env.get("AI_EDITOR_MEMORY_DB_PATH", ".agentd/memory.sqlite3"),
            trigger_frac=float(env.get("AI_EDITOR_MEMORY_COMPACT_TRIGGER_FRAC", "0.65")),
            hot_token_frac=float(env.get("AI_EDITOR_MEMORY_HOT_TOKEN_FRAC", "0.4")),
            hot_turns=int(env.get("AI_EDITOR_MEMORY_HOT_TURNS", "10")),
            window_tokens=int(env.get("AI_EDITOR_MEMORY_WINDOW_TOKENS", "128000")),
        )
```

```python
# agentd/memory/__init__.py
from agentd.memory.models import (
    AnchoredSummary,
    CompactionResult,
    CompactionSegment,
    MemoryKind,
    TurnPreparation,
)

__all__ = [
    "AnchoredSummary",
    "CompactionResult",
    "CompactionSegment",
    "MemoryKind",
    "TurnPreparation",
]
```

Create `tests/memory/__init__.py` (empty) only if other `tests/` subdirs use package markers — match the existing convention.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/agentd-py && pytest tests/memory/test_config.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add services/agentd-py/agentd/memory/__init__.py services/agentd-py/agentd/memory/models.py services/agentd-py/agentd/memory/config.py services/agentd-py/tests/memory/
git commit -m "feat(memory): scaffold memory subpackage with models + config"
```

---

### Task 2: MemoryStore (SQLite — segments + anchored summaries)

**Files:**
- Create: `agentd/memory/store.py`
- Test: `tests/memory/test_store.py`

**Interfaces:**
- Consumes: `CompactionSegment`, `AnchoredSummary` (Task 1).
- Produces `MemoryStore`:
  - `__init__(self, db_path: str | Path)` — opens/creates DB, runs migrations.
  - `add_segments(self, segments: list[CompactionSegment]) -> None`
  - `get_segments(self, run_id: str) -> list[CompactionSegment]` — ordered by `seq`.
  - `next_seq(self, run_id: str) -> int` — `MAX(seq)+1` for the run (0 if none); makes `seq` run-monotonic across compaction rounds.
  - `upsert_anchor(self, run_id: str, summary_md: str) -> AnchoredSummary` — version 1 on insert, else bump.
  - `get_anchor(self, run_id: str) -> AnchoredSummary | None`

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_store.py
from datetime import datetime, timezone
from agentd.memory.models import CompactionSegment
from agentd.memory.store import MemoryStore

def _seg(run_id: str, seq: int, content: str) -> CompactionSegment:
    return CompactionSegment(
        id=f"{run_id}-{seq}", run_id=run_id, seq=seq,
        content=content, created_at=datetime.now(timezone.utc),
    )

def test_segments_round_trip_ordered(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite3")
    store.add_segments([_seg("r1", 1, "first"), _seg("r1", 0, "zeroth")])
    got = store.get_segments("r1")
    assert [s.seq for s in got] == [0, 1]
    assert got[0].content == "zeroth"

def test_segments_scoped_by_run(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite3")
    store.add_segments([_seg("r1", 0, "a"), _seg("r2", 0, "b")])
    assert [s.content for s in store.get_segments("r1")] == ["a"]

def test_next_seq_monotonic_across_batches(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite3")
    assert store.next_seq("r1") == 0
    store.add_segments([_seg("r1", store.next_seq("r1"), "a")])
    assert store.next_seq("r1") == 1
    store.add_segments([_seg("r1", 1, "b"), _seg("r1", 2, "c")])
    assert store.next_seq("r1") == 3
    assert store.next_seq("r2") == 0   # scoped per run

def test_anchor_insert_then_bump_version(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite3")
    a1 = store.upsert_anchor("r1", "summary v1")
    assert a1.version == 1 and a1.summary_md == "summary v1"
    a2 = store.upsert_anchor("r1", "summary v2")
    assert a2.version == 2 and a2.summary_md == "summary v2"
    assert store.get_anchor("r1").summary_md == "summary v2"

def test_get_anchor_missing_returns_none(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite3")
    assert store.get_anchor("nope") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/agentd-py && pytest tests/memory/test_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agentd.memory.store'`

- [ ] **Step 3: Write minimal implementation**

```python
# agentd/memory/store.py
from __future__ import annotations
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from agentd.memory.models import AnchoredSummary, CompactionSegment

_SCHEMA = """
CREATE TABLE IF NOT EXISTS compaction_segments (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_segments_run ON compaction_segments(run_id, seq);
CREATE TABLE IF NOT EXISTS anchored_summaries (
    run_id TEXT PRIMARY KEY,
    summary_md TEXT NOT NULL,
    version INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
"""

class MemoryStore:
    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def add_segments(self, segments: list[CompactionSegment]) -> None:
        self._conn.executemany(
            "INSERT OR REPLACE INTO compaction_segments "
            "(id, run_id, seq, content, created_at) VALUES (?,?,?,?,?)",
            [(s.id, s.run_id, s.seq, s.content, s.created_at.isoformat()) for s in segments],
        )
        self._conn.commit()

    def get_segments(self, run_id: str) -> list[CompactionSegment]:
        rows = self._conn.execute(
            "SELECT * FROM compaction_segments WHERE run_id=? ORDER BY seq", (run_id,)
        ).fetchall()
        return [
            CompactionSegment(
                id=r["id"], run_id=r["run_id"], seq=r["seq"],
                content=r["content"], created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    def next_seq(self, run_id: str) -> int:
        r = self._conn.execute(
            "SELECT MAX(seq) AS m FROM compaction_segments WHERE run_id=?", (run_id,)
        ).fetchone()
        return 0 if r["m"] is None else r["m"] + 1

    def upsert_anchor(self, run_id: str, summary_md: str) -> AnchoredSummary:
        now = datetime.now(timezone.utc)
        existing = self._conn.execute(
            "SELECT version FROM anchored_summaries WHERE run_id=?", (run_id,)
        ).fetchone()
        version = (existing["version"] + 1) if existing else 1
        self._conn.execute(
            "INSERT OR REPLACE INTO anchored_summaries "
            "(run_id, summary_md, version, updated_at) VALUES (?,?,?,?)",
            (run_id, summary_md, version, now.isoformat()),
        )
        self._conn.commit()
        return AnchoredSummary(run_id=run_id, summary_md=summary_md, version=version, updated_at=now)

    def get_anchor(self, run_id: str) -> AnchoredSummary | None:
        r = self._conn.execute(
            "SELECT * FROM anchored_summaries WHERE run_id=?", (run_id,)
        ).fetchone()
        if r is None:
            return None
        return AnchoredSummary(
            run_id=r["run_id"], summary_md=r["summary_md"], version=r["version"],
            updated_at=datetime.fromisoformat(r["updated_at"]),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/agentd-py && pytest tests/memory/test_store.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add services/agentd-py/agentd/memory/store.py services/agentd-py/tests/memory/test_store.py
git commit -m "feat(memory): SQLite store for compaction segments + anchored summaries"
```

---

### Task 3: Compactor — token helpers + hot selection + below-threshold no-op

**Files:**
- Create: `agentd/memory/compactor.py`
- Test: `tests/memory/test_compactor.py`

**Interfaces:**
- Consumes: `MemoryStore` (Task 2), `CompactionResult`, `CompactionSegment` (Task 1).
- Produces:
  - `estimate_tokens(text: str) -> int` — `max(1, len//4)`.
  - `_truncate_to_tokens(text: str, max_tokens: int) -> str` — head + `…[truncated]…` + tail to fit.
  - `_select_hot(history, hot_budget_tokens, hot_turns_cap) -> tuple[list[dict], list[dict], int]` — newest turns within the token budget and count cap; always keeps ≥1; returns `(evicted, hot, hot_used_tokens)`.
  - `AnchorSummarizer = Callable[[str, str], Awaitable[str]]` — `(old_anchor, evicted_text) -> new_anchor`.
  - `Compactor.__init__(self, store, summarize, *, window_tokens, trigger_frac=0.65, hot_token_frac=0.4, hot_turns=10)`
  - `async Compactor.maybe_compact(self, history, run_id) -> CompactionResult`

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_compactor.py
import pytest
from agentd.memory.compactor import Compactor, estimate_tokens, _select_hot, _truncate_to_tokens
from agentd.memory.store import MemoryStore

async def _never(old: str, new: str) -> str:
    raise AssertionError("summarize called below threshold")

def test_estimate_tokens_charsdiv4():
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("") == 1

def test_truncate_keeps_head_and_tail():
    out = _truncate_to_tokens("A" * 100 + "Z" * 100, 10)  # 10 tokens ≈ 40 chars
    assert "[truncated]" in out
    assert out.startswith("A") and out.endswith("Z")
    assert len(out) < 200

def test_select_hot_token_bounded():
    # each msg ~20 tokens (80 chars); budget 45 tokens keeps 2 newest; cap not hit
    hist = [{"role": "user", "content": "x" * 80} for _ in range(5)]
    evicted, hot, used = _select_hot(hist, hot_budget_tokens=45, hot_turns_cap=10)
    assert len(hot) == 2 and hot == hist[-2:]
    assert len(evicted) == 3 and used <= 45

def test_select_hot_count_capped():
    hist = [{"role": "user", "content": "x" * 4} for _ in range(20)]  # tiny msgs
    evicted, hot, _ = _select_hot(hist, hot_budget_tokens=10_000, hot_turns_cap=3)
    assert len(hot) == 3 and hot == hist[-3:]

def test_select_hot_always_keeps_one():
    hist = [{"role": "user", "content": "x" * 4000}]  # one huge msg over any budget
    evicted, hot, used = _select_hot(hist, hot_budget_tokens=10, hot_turns_cap=10)
    assert len(hot) == 1 and evicted == [] and used > 10

@pytest.mark.asyncio
async def test_below_threshold_is_noop(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite3")
    comp = Compactor(store, _never, window_tokens=10000, trigger_frac=0.65, hot_token_frac=0.4, hot_turns=10)
    history = [{"role": "user", "content": "xxxx"} for _ in range(3)]
    result = await comp.maybe_compact(history, "r1")
    assert result.compacted is False
    assert result.history == history
    assert store.get_anchor("r1") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/agentd-py && pytest tests/memory/test_compactor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agentd.memory.compactor'`

- [ ] **Step 3: Write minimal implementation**

```python
# agentd/memory/compactor.py
from __future__ import annotations
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from agentd.memory.models import CompactionResult, CompactionSegment
from agentd.memory.store import MemoryStore

logger = logging.getLogger(__name__)

AnchorSummarizer = Callable[[str, str], Awaitable[str]]

def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)

def _history_tokens(history: list[dict]) -> int:
    return sum(estimate_tokens(str(m.get("content", ""))) for m in history)

def _render(messages: list[dict]) -> str:
    return "\n".join(f"{m.get('role', '')}: {m.get('content', '')}" for m in messages)

def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    max_chars = max(8, max_tokens * 4)
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head
    return text[:head] + "\n…[truncated]…\n" + text[-tail:]

def _select_hot(
    history: list[dict], hot_budget_tokens: int, hot_turns_cap: int
) -> tuple[list[dict], list[dict], int]:
    """Newest turns that fit the token budget and the count cap. Always keeps ≥1 turn."""
    hot: list[dict] = []
    used = 0
    for m in reversed(history):
        t = estimate_tokens(str(m.get("content", "")))
        if hot and (used + t > hot_budget_tokens or len(hot) >= hot_turns_cap):
            break
        hot.insert(0, m)
        used += t
    evicted = history[: len(history) - len(hot)]
    return evicted, hot, used

def _anchor_message(text: str) -> dict:
    return {
        "role": "user",
        "content": f"[MEMORY] Summary of earlier conversation that was compacted:\n{text}",
    }

class Compactor:
    def __init__(
        self,
        store: MemoryStore,
        summarize: AnchorSummarizer,
        *,
        window_tokens: int,
        trigger_frac: float = 0.65,
        hot_token_frac: float = 0.4,
        hot_turns: int = 10,
    ) -> None:
        self._store = store
        self._summarize = summarize
        self._window_tokens = window_tokens
        self._trigger_frac = trigger_frac
        self._hot_token_frac = hot_token_frac
        self._hot_turns = hot_turns

    async def maybe_compact(self, history: list[dict], run_id: str) -> CompactionResult:
        # Pure token-trigger check (no count short-circuit: a short history of oversized
        # turns can be over budget and must still compact).
        if _history_tokens(history) < self._window_tokens * self._trigger_frac:
            anchor = self._store.get_anchor(run_id)
            return CompactionResult(
                compacted=False, history=history,
                anchor=anchor.summary_md if anchor else None,
            )
        # Compaction logic added in Task 4.
        return CompactionResult(compacted=False, history=history)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/agentd-py && pytest tests/memory/test_compactor.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add services/agentd-py/agentd/memory/compactor.py services/agentd-py/tests/memory/test_compactor.py
git commit -m "feat(memory): compactor token helpers, hot selection, below-threshold no-op"
```

---

### Task 4: Compactor — over-threshold compaction (evict → persist + merge, with truncation backstop)

**Files:**
- Modify: `agentd/memory/compactor.py` (replace the Task-3 placeholder in `maybe_compact`)
- Test: `tests/memory/test_compactor.py` (add cases)

**Interfaces:**
- Consumes: `MemoryStore.next_seq` (Task 2) for run-monotonic segment ordering.
- Produces: `maybe_compact` over threshold returns `compacted=True` with `history = [anchor_message] + hot`, persists evicted as segments (`seq` continues from `store.next_seq(run_id)`), merges into the anchor via the injected summarizer. Single oversize newest turn ⇒ truncated in-window + full original persisted (`degraded=True`). Empty-evicted (truncation made room alone) ⇒ summarizer not called.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_compactor.py  (append)
@pytest.mark.asyncio
async def test_over_threshold_compacts(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite3")
    captured = {}
    async def summ(old: str, evicted: str) -> str:
        captured["old"], captured["evicted"] = old, evicted
        return "MERGED ANCHOR"
    comp = Compactor(store, summ, window_tokens=100, trigger_frac=0.1,
                     hot_token_frac=0.4, hot_turns=2)  # hot_budget=40 tokens
    history = [{"role": "user", "content": "z" * 80} for _ in range(6)]  # ~20 tok each
    result = await comp.maybe_compact(history, "r1")
    assert result.compacted is True
    assert result.history[-2:] == history[-2:]            # last 2 verbatim (count cap)
    assert result.history[0]["content"].startswith("[MEMORY]")
    assert "MERGED ANCHOR" in result.history[0]["content"]
    assert len(store.get_segments("r1")) == 4             # first 4 evicted
    assert store.get_anchor("r1").summary_md == "MERGED ANCHOR"

@pytest.mark.asyncio
async def test_anchor_merges_not_regenerates(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite3")
    store.upsert_anchor("r1", "PRIOR")
    seen = {}
    async def summ(old: str, evicted: str) -> str:
        seen["old"] = old
        return old + " + NEW"
    comp = Compactor(store, summ, window_tokens=100, trigger_frac=0.1,
                     hot_token_frac=0.4, hot_turns=2)
    history = [{"role": "user", "content": "z" * 80} for _ in range(6)]
    await comp.maybe_compact(history, "r1")
    assert seen["old"] == "PRIOR"                          # prior anchor fed back in
    assert store.get_anchor("r1").summary_md == "PRIOR + NEW"
    assert store.get_anchor("r1").version == 2

@pytest.mark.asyncio
async def test_single_oversize_message_is_truncated(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite3")
    async def summ(old: str, evicted: str) -> str:
        raise AssertionError("summarize should not run when nothing is evicted")
    comp = Compactor(store, summ, window_tokens=100, trigger_frac=0.1,
                     hot_token_frac=0.4, hot_turns=10)  # hot_budget=40 tok=160 chars
    history = [{"role": "user", "content": "q" * 4000}]   # ~1000 tok, sole newest turn
    result = await comp.maybe_compact(history, "r1")
    assert result.compacted is True and result.degraded is True
    assert len(result.history) == 1
    assert len(result.history[0]["content"]) < 4000
    assert "[truncated]" in result.history[0]["content"]
    assert len(store.get_segments("r1")) == 1
    assert store.get_segments("r1")[0].content == "q" * 4000  # full original persisted
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/agentd-py && pytest tests/memory/test_compactor.py -v`
Expected: FAIL — `test_over_threshold_compacts` asserts `compacted is True` but the placeholder returns `False`.

- [ ] **Step 3: Write minimal implementation**

Replace the `# Compaction logic added in Task 4.` line and its `return` with:

```python
        now = datetime.now(timezone.utc)
        ms = int(now.timestamp() * 1000)
        hot_budget = int(self._window_tokens * self._hot_token_frac)
        evicted, hot, hot_used = _select_hot(history, hot_budget, self._hot_turns)
        base = self._store.next_seq(run_id)  # run-monotonic seq across compaction rounds
        degraded = False
        extra: list[CompactionSegment] = []
        # Backstop: a single newest turn that alone busts the hot budget must be truncated
        # in-window, else compaction cannot get us back under the window.
        if hot_used > hot_budget and len(hot) == 1:
            full = str(hot[0].get("content", ""))
            extra.append(CompactionSegment(
                id=f"{run_id}-{base + len(evicted)}-{ms}", run_id=run_id, seq=base + len(evicted),
                content=full, created_at=now,
            ))
            hot = [{**hot[0], "content": _truncate_to_tokens(full, hot_budget)}]
            degraded = True
        segments = [
            CompactionSegment(
                id=f"{run_id}-{base + i}-{ms}", run_id=run_id, seq=base + i,
                content=str(m.get("content", "")), created_at=now,
            )
            for i, m in enumerate(evicted)
        ]
        if segments or extra:
            self._store.add_segments(segments + extra)  # persist BEFORE summarize → lossless
        old = self._store.get_anchor(run_id)
        old_text = old.summary_md if old else ""
        if not evicted:
            keep = [_anchor_message(old_text)] if old_text else []
            return CompactionResult(
                compacted=True, history=[*keep, *hot], anchor=old_text or None, degraded=degraded,
            )
        new_anchor = await self._summarize(old_text, _render(evicted))
        self._store.upsert_anchor(run_id, new_anchor)
        return CompactionResult(
            compacted=True, history=[_anchor_message(new_anchor), *hot],
            anchor=new_anchor, degraded=degraded,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/agentd-py && pytest tests/memory/test_compactor.py -v`
Expected: PASS (9 passed)

- [ ] **Step 5: Commit**

```bash
git add services/agentd-py/agentd/memory/compactor.py services/agentd-py/tests/memory/test_compactor.py
git commit -m "feat(memory): compactor evicts+merges over-threshold history, truncates oversize tail"
```

---

### Task 5: Compactor — summarizer-failure fallback

**Files:**
- Modify: `agentd/memory/compactor.py` (wrap the trailing summarize call)
- Test: `tests/memory/test_compactor.py` (add case)

**Interfaces:**
- Produces: on summarizer exception, `maybe_compact` returns `compacted=True, degraded=True` with the prior anchor (if any) + hot; evicted stays persisted; never raises.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_compactor.py  (append)
@pytest.mark.asyncio
async def test_summarizer_failure_falls_back(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite3")
    async def boom(old: str, evicted: str) -> str:
        raise RuntimeError("provider down")
    comp = Compactor(store, boom, window_tokens=100, trigger_frac=0.1,
                     hot_token_frac=0.4, hot_turns=2)
    history = [{"role": "user", "content": "y" * 80} for _ in range(6)]
    result = await comp.maybe_compact(history, "r1")
    assert result.degraded is True and result.compacted is True
    assert result.history[-2:] == history[-2:]            # hot preserved
    assert len(store.get_segments("r1")) == 4             # evicted still persisted (lossless)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/agentd-py && pytest tests/memory/test_compactor.py::test_summarizer_failure_falls_back -v`
Expected: FAIL — `RuntimeError: provider down` propagates.

- [ ] **Step 3: Write minimal implementation**

Wrap the trailing summarize call (the lines after the `if not evicted:` block). Persist already happens before summarize, so a failure is still lossless. Replace:

```python
        new_anchor = await self._summarize(old_text, _render(evicted))
        self._store.upsert_anchor(run_id, new_anchor)
        return CompactionResult(
            compacted=True, history=[_anchor_message(new_anchor), *hot],
            anchor=new_anchor, degraded=degraded,
        )
```

with:

```python
        try:
            new_anchor = await self._summarize(old_text, _render(evicted))
        except Exception:  # best-effort: never fail a loop iteration
            logger.warning("[memory] anchor summarize failed for run=%s; degrading", run_id, exc_info=True)
            keep = (
                [{"role": "user", "content": f"[MEMORY] (earlier context summary unavailable)\n{old_text}"}]
                if old_text else []
            )
            return CompactionResult(
                compacted=True, history=[*keep, *hot], anchor=old_text or None, degraded=True,
            )
        self._store.upsert_anchor(run_id, new_anchor)
        return CompactionResult(
            compacted=True, history=[_anchor_message(new_anchor), *hot],
            anchor=new_anchor, degraded=degraded,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/agentd-py && pytest tests/memory/test_compactor.py -v`
Expected: PASS (10 passed)

- [ ] **Step 5: Commit**

```bash
git add services/agentd-py/agentd/memory/compactor.py services/agentd-py/tests/memory/test_compactor.py
git commit -m "feat(memory): compactor degrades gracefully on summarizer failure"
```

---

### Task 6: MemoryHarness façade + no-op default + build factory + engine summarizer

**Files:**
- Create: `agentd/memory/harness.py`
- Modify: `agentd/memory/__init__.py` (export `MemoryHarness`, `build_memory_harness`, `NO_OP_HARNESS`)
- Modify: `agentd/memory/compactor.py` (add `make_engine_summarizer`)
- Test: `tests/memory/test_harness.py`

**Interfaces:**
- Consumes: `Compactor`, `MemoryStore`, `MemoryConfig`, `TurnPreparation`.
- Produces:
  - `MemoryHarness.__init__(self, *, enabled: bool, compactor: Compactor | None)`
  - `async MemoryHarness.prepare_turn(self, history, run_id) -> TurnPreparation` — disabled/no compactor ⇒ history untouched; else delegate; swallow internal errors.
  - `async MemoryHarness.recall(self, query: str, run_id: str) -> list[dict]` — Phase-2 stub, returns `[]`.
  - `NO_OP_HARNESS: MemoryHarness` — module singleton (`enabled=False`), the default injected into both loops.
  - `make_engine_summarizer(reasoning_engine) -> AnchorSummarizer`
  - `build_memory_harness(config: MemoryConfig, reasoning_engine) -> MemoryHarness`

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_harness.py
import pytest
from agentd.memory.harness import MemoryHarness, NO_OP_HARNESS
from agentd.memory.compactor import Compactor
from agentd.memory.store import MemoryStore

@pytest.mark.asyncio
async def test_disabled_harness_is_passthrough():
    history = [{"role": "user", "content": "hi"}]
    prep = await NO_OP_HARNESS.prepare_turn(history, "r1")
    assert prep.history is history and prep.compacted is False and prep.recalled_memories == []

@pytest.mark.asyncio
async def test_enabled_harness_delegates(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite3")
    async def summ(old, evicted): return "A"
    comp = Compactor(store, summ, window_tokens=100, trigger_frac=0.1, hot_token_frac=0.4, hot_turns=2)
    harness = MemoryHarness(enabled=True, compactor=comp)
    history = [{"role": "user", "content": "q" * 80} for _ in range(6)]
    prep = await harness.prepare_turn(history, "r1")
    assert prep.compacted is True and prep.history[0]["content"].startswith("[MEMORY]")

@pytest.mark.asyncio
async def test_prepare_turn_swallows_errors(tmp_path):
    class Boom:
        async def maybe_compact(self, history, run_id):
            raise RuntimeError("kaboom")
    harness = MemoryHarness(enabled=True, compactor=Boom())  # type: ignore[arg-type]
    history = [{"role": "user", "content": "x"}]
    prep = await harness.prepare_turn(history, "r1")
    assert prep.history is history and prep.compacted is False

@pytest.mark.asyncio
async def test_recall_stub_returns_empty():
    assert await NO_OP_HARNESS.recall("anything", "r1") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/agentd-py && pytest tests/memory/test_harness.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agentd.memory.harness'`

- [ ] **Step 3: Write minimal implementation**

```python
# agentd/memory/harness.py
from __future__ import annotations
import logging
from agentd.memory.compactor import Compactor, make_engine_summarizer
from agentd.memory.config import MemoryConfig
from agentd.memory.models import TurnPreparation
from agentd.memory.store import MemoryStore

logger = logging.getLogger(__name__)

class MemoryHarness:
    def __init__(self, *, enabled: bool, compactor: Compactor | None) -> None:
        self._enabled = enabled
        self._compactor = compactor

    async def prepare_turn(self, history: list[dict], run_id: str) -> TurnPreparation:
        if not self._enabled or self._compactor is None:
            return TurnPreparation(history=history, recalled_memories=[], compacted=False)
        try:
            result = await self._compactor.maybe_compact(history, run_id)
        except Exception:  # best-effort: memory must never break a loop
            logger.warning("[memory] prepare_turn failed for run=%s", run_id, exc_info=True)
            return TurnPreparation(history=history, recalled_memories=[], compacted=False)
        return TurnPreparation(history=result.history, recalled_memories=[], compacted=result.compacted)

    async def recall(self, query: str, run_id: str) -> list[dict]:
        return []  # Phase 2

NO_OP_HARNESS = MemoryHarness(enabled=False, compactor=None)

def build_memory_harness(config: MemoryConfig, reasoning_engine: object) -> MemoryHarness:
    if not config.enabled:
        return NO_OP_HARNESS
    store = MemoryStore(config.db_path)
    compactor = Compactor(
        store, make_engine_summarizer(reasoning_engine),
        window_tokens=config.window_tokens, trigger_frac=config.trigger_frac,
        hot_token_frac=config.hot_token_frac, hot_turns=config.hot_turns,
    )
    return MemoryHarness(enabled=True, compactor=compactor)
```

Add to `agentd/memory/compactor.py`:

```python
_SUMMARY_SYSTEM = (
    "You maintain a running summary of an AI coding session. Merge the PRIOR SUMMARY and the "
    "NEW EVICTED MESSAGES into one updated summary. Preserve goals, decisions, file/symbol names, "
    "and unresolved threads. Do not drop facts from the prior summary. Be concise but lossless on "
    "decisions and identifiers. Return only the updated summary."
)

def make_engine_summarizer(reasoning_engine: object) -> AnchorSummarizer:
    async def _summarize(old_anchor: str, evicted_text: str) -> str:
        prompt = f"PRIOR SUMMARY:\n{old_anchor or '(none)'}\n\nNEW EVICTED MESSAGES:\n{evicted_text}"
        # Uses the engine's plain-text generation (same entrypoint ChatAgent uses for QA answers).
        return await reasoning_engine.generate_text(  # type: ignore[attr-defined]
            system_instructions=_SUMMARY_SYSTEM, user_payload=prompt,
        )
    return _summarize
```

> **Wiring note:** confirm the exact text-generation method/signature on the reasoning engine (grep `generate_text` in `agentd/reasoning/` and `agentd/chat/agent.py`) and adjust this call to match. Unit tests inject their own summarizer and don't exercise this adapter — verify it live in Task 9.

Update `agentd/memory/__init__.py` to also export `MemoryHarness`, `build_memory_harness`, `NO_OP_HARNESS`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/agentd-py && pytest tests/memory/ -v`
Expected: PASS (all memory tests green)

- [ ] **Step 5: Commit**

```bash
git add services/agentd-py/agentd/memory/harness.py services/agentd-py/agentd/memory/__init__.py services/agentd-py/agentd/memory/compactor.py services/agentd-py/tests/memory/test_harness.py
git commit -m "feat(memory): MemoryHarness facade + build factory + engine summarizer"
```

---

### Task 7: Wire MemoryHarness into ControllerLoop

**Files:**
- Modify: `agentd/chat/controller_loop.py` (constructor + top of `_iterate` loop) and `agentd/chat/controller.py` (construction site)
- Test: `tests/memory/test_controller_loop_compaction.py`

**Interfaces:**
- Consumes: `MemoryHarness`, `NO_OP_HARNESS`.
- Produces: `ControllerLoop.__init__` gains `memory_harness: MemoryHarness = NO_OP_HARNESS` (keyword, defaulted — existing constructions unaffected). At the top of each `for iteration` in `_iterate`, before `create_controller_step`:
  ```python
  run_id = str(plan_context.get("run_id", "chat"))
  _prep = await self._memory_harness.prepare_turn(history, run_id)
  history[:] = _prep.history
  ```
  `history[:]` mutates the same list `partial_history()` and downstream `.append()` calls reference.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_controller_loop_compaction.py
import pytest
from agentd.memory.harness import MemoryHarness
from agentd.memory.compactor import Compactor
from agentd.memory.store import MemoryStore

@pytest.mark.asyncio
async def test_controller_loop_invokes_harness(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite3")
    calls = []
    async def summ(old, evicted): return "A"
    class SpyCompactor(Compactor):
        async def maybe_compact(self, history, run_id):
            calls.append((len(history), run_id))
            return await super().maybe_compact(history, run_id)
    comp = SpyCompactor(store, summ, window_tokens=100, trigger_frac=0.1, hot_token_frac=0.4, hot_turns=2)
    harness = MemoryHarness(enabled=True, compactor=comp)
    # Construct ControllerLoop with the project's existing scripted fixtures (copy from an existing
    # tests/test_controller_loop*.py), passing memory_harness=harness and
    # plan_context={"run_id": "thread-x", ...}; drive one run() with seed_history of >hot_turns
    # oversized messages; then assert calls is non-empty and calls[0][1] == "thread-x".
    assert harness is not None  # replace with the real loop drive
```

> **Implementer:** replace the placeholder assert with the standard `ControllerLoop` construction copied from an existing `tests/test_controller_loop*.py`, injecting `memory_harness=harness` and `plan_context["run_id"]="thread-x"`, driving one `run()` whose `seed_history` has > `hot_turns` oversized messages; assert `calls` non-empty and `calls[0][1] == "thread-x"`.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/agentd-py && pytest tests/memory/test_controller_loop_compaction.py -v`
Expected: FAIL — `ControllerLoop.__init__` has no `memory_harness` param.

- [ ] **Step 3: Write minimal implementation**

In `agentd/chat/controller_loop.py`:
1. Import: `from agentd.memory.harness import MemoryHarness, NO_OP_HARNESS`.
2. `__init__`: add `memory_harness: MemoryHarness = NO_OP_HARNESS,`; store `self._memory_harness = memory_harness`.
3. At the very top of `for iteration in range(max_iters + 1):` in `_iterate` (before the `if iteration == 0:` block):

```python
            run_id = str(plan_context.get("run_id", "chat"))
            _prep = await self._memory_harness.prepare_turn(history, run_id)
            history[:] = _prep.history
```

In `agentd/chat/controller.py`: grep `ControllerLoop(` for the construction site; pass `memory_harness=self._memory_harness` (threaded from `build_memory_harness` at app startup) and set `plan_context["run_id"] = thread_id` before `loop.run(...)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/agentd-py && pytest tests/memory/test_controller_loop_compaction.py -v && pytest tests/ -k controller -q`
Expected: PASS (new) and existing controller tests still green.

- [ ] **Step 5: Commit**

```bash
git add services/agentd-py/agentd/chat/controller_loop.py services/agentd-py/agentd/chat/controller.py services/agentd-py/tests/memory/test_controller_loop_compaction.py
git commit -m "feat(memory): wire MemoryHarness compaction into ControllerLoop"
```

---

### Task 8: Wire MemoryHarness into the task ToolLoop

**Files:**
- Modify: `agentd/tools/loop.py` (constructor ~line 205 + top of `for iteration in range(total_budget):` ~line 359) and the `ToolLoop(` construction site in `agentd/orchestrator/engine.py`
- Test: `tests/memory/test_tool_loop_compaction.py`

**Interfaces:**
- Consumes: `MemoryHarness`, `NO_OP_HARNESS`.
- Produces: `ToolLoop.__init__` gains `memory_harness: MemoryHarness = NO_OP_HARNESS` (keyword, defaulted). At the top of the iteration loop (before `history_tail`/`create_tool_step`), compact in place with `run_id = str(self._task_id)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_tool_loop_compaction.py
import pytest
from agentd.memory.harness import MemoryHarness
from agentd.memory.compactor import Compactor
from agentd.memory.store import MemoryStore

@pytest.mark.asyncio
async def test_tool_loop_invokes_harness(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite3")
    calls = []
    async def summ(old, evicted): return "A"
    class SpyCompactor(Compactor):
        async def maybe_compact(self, history, run_id):
            calls.append(run_id)
            return await super().maybe_compact(history, run_id)
    comp = SpyCompactor(store, summ, window_tokens=100, trigger_frac=0.1, hot_token_frac=0.4, hot_turns=2)
    harness = MemoryHarness(enabled=True, compactor=comp)
    # Construct ToolLoop with existing scripted fixtures (copy from tests/test_tool_loop*.py),
    # inject memory_harness=harness, run one step whose history grows beyond hot_turns,
    # assert calls is non-empty and each entry == the task id.
    assert harness is not None  # replace with the real loop drive
```

> **Implementer:** replace the placeholder with the standard `ToolLoop` construction from an existing `tests/test_tool_loop*.py`, inject `memory_harness=harness`, drive a step, assert `calls` non-empty.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/agentd-py && pytest tests/memory/test_tool_loop_compaction.py -v`
Expected: FAIL — `ToolLoop.__init__` has no `memory_harness` param.

- [ ] **Step 3: Write minimal implementation**

In `agentd/tools/loop.py`:
1. Import: `from agentd.memory.harness import MemoryHarness, NO_OP_HARNESS`.
2. `__init__` (near `broadcast_key`/`skip_verify`): add `memory_harness: MemoryHarness = NO_OP_HARNESS,`; store `self._memory_harness = memory_harness`.
3. At the top of `for iteration in range(total_budget):` (before `history_tail=history[-8:]` is built):

```python
            _prep = await self._memory_harness.prepare_turn(history, str(self._task_id))
            history[:] = _prep.history
```

In `agentd/orchestrator/engine.py`: grep `ToolLoop(`; pass `memory_harness=...` (the same `build_memory_harness` instance from startup). Pass it **only** to the step-execution `ToolLoop`, not the inline-change loop (mirror how `abort` is scoped).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/agentd-py && pytest tests/memory/test_tool_loop_compaction.py -v && pytest tests/ -k tool_loop -q`
Expected: PASS (new) and existing tool-loop tests still green.

- [ ] **Step 5: Commit**

```bash
git add services/agentd-py/agentd/tools/loop.py services/agentd-py/agentd/orchestrator/engine.py services/agentd-py/tests/memory/test_tool_loop_compaction.py
git commit -m "feat(memory): wire MemoryHarness compaction into task ToolLoop"
```

---

### Task 9: Integration test + kill-switch parity + live manual check

**Files:**
- Test: `tests/memory/test_integration_compaction.py`

**Interfaces:**
- Consumes: everything above.
- Produces: an acceptance test proving (a) a long run crosses the threshold, persists segments, versions the anchor, keeps hot verbatim, and the anchor carries prior content forward across two compactions; (b) `enabled=False` leaves history untouched.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_integration_compaction.py
import pytest
from agentd.memory.harness import MemoryHarness, NO_OP_HARNESS
from agentd.memory.compactor import Compactor
from agentd.memory.store import MemoryStore

@pytest.mark.asyncio
async def test_long_run_compacts_and_persists(tmp_path):
    store = MemoryStore(tmp_path / "m.sqlite3")
    async def summ(old, evicted):
        return (old + " | " if old else "") + f"summarized {len(evicted)} chars"
    comp = Compactor(store, summ, window_tokens=200, trigger_frac=0.1, hot_token_frac=0.4, hot_turns=3)
    harness = MemoryHarness(enabled=True, compactor=comp)
    history = [{"role": "user", "content": "m" * 50} for _ in range(12)]
    prep = await harness.prepare_turn(history, "run-A")
    assert prep.compacted is True
    assert prep.history[-3:] == history[-3:]            # hot verbatim
    assert len(store.get_segments("run-A")) == 9        # 12 - 3 evicted
    assert store.get_anchor("run-A").version == 1
    history2 = list(prep.history) + [{"role": "user", "content": "n" * 200} for _ in range(6)]
    prep2 = await harness.prepare_turn(history2, "run-A")
    assert prep2.compacted is True
    assert store.get_anchor("run-A").version == 2
    assert "|" in store.get_anchor("run-A").summary_md  # prior anchor carried forward (merge)

@pytest.mark.asyncio
async def test_disabled_is_byte_identical():
    history = [{"role": "user", "content": "x" * 9999} for _ in range(50)]
    prep = await NO_OP_HARNESS.prepare_turn(history, "run-A")
    assert prep.history is history and prep.compacted is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/agentd-py && pytest tests/memory/test_integration_compaction.py -v`
Expected: PASS immediately if Tasks 1–6 are correct (this exercises the harness directly). If an assertion fails, read it — most likely the anchor carry-forward.

- [ ] **Step 3: Fix any failure**

No new source unless an assertion fails; then fix the responsible unit.

- [ ] **Step 4: Run full suite + live manual check**

```bash
cd services/agentd-py && pytest -q          # whole suite green (read FAILED lines, not exit code)
mypy agentd/memory                          # types clean
ruff check agentd/memory                    # lint clean
```

Live check of the production summarizer adapter (the one path unit tests don't cover):
```bash
export $(cat .env | grep -v "^#" | grep "=" | sed 's/"//g' | xargs)
AI_EDITOR_MEMORY_ENABLED=1 AI_EDITOR_MEMORY_WINDOW_TOKENS=4000 AI_EDITOR_MEMORY_HOT_TURNS=4 \
AI_EDITOR_MEMORY_HOT_TOKEN_FRAC=0.4 \
  bash scripts/stress/start-backend.sh --backend gemini --workspace "$PWD/workspaces/shadow-forge-stress" --validation-profile none
# Drive a long chat turn; confirm compaction fires in logs and the DB fills:
sqlite3 workspaces/shadow-forge-stress/.agentd/memory.sqlite3 \
  "SELECT run_id, version, length(summary_md) FROM anchored_summaries;"
sqlite3 workspaces/shadow-forge-stress/.agentd/memory.sqlite3 \
  "SELECT run_id, count(*) FROM compaction_segments GROUP BY run_id;"
```
Expected: ≥1 `anchored_summaries` row (`version >= 1`); `compaction_segments` populated; the turn completes coherently on the compacted history.

- [ ] **Step 5: Commit**

```bash
git add services/agentd-py/tests/memory/test_integration_compaction.py
git commit -m "test(memory): end-to-end compaction + kill-switch parity"
```

---

## Self-Review

**Spec coverage (Phase 1 scope only):**
- §1 component boundaries → Tasks 1–6 (one unit per file; store is the only DB-aware unit). ✓
- §2 data model (`compaction_segments` with **no** `tier`/`embedding`, `anchored_summaries`, run-monotonic `seq` via `next_seq`) → Task 2, asserted by `test_next_seq_monotonic_across_batches`. `memories` table is Phase 2 — correctly absent. ✓
- §5 compaction (0.65 trigger; **token-bounded** hot set + count cap; single-message truncation backstop; anchored merge not regenerate; all-evicted folded; fallback) → Tasks 3/4/5, asserted by `test_select_hot_*`, `test_over_threshold_compacts`, `test_anchor_merges_not_regenerates`, `test_single_oversize_message_is_truncated`, `test_summarizer_failure_falls_back`. The token bound (not count bound) is what guarantees the post-compaction window fits — proven by the oversize + count-cap tests. ✓
- §7 error handling (kill switch, prepare_turn swallows, summarize/truncation degrade) → Task 6 (`test_prepare_turn_swallows_errors`) + Task 5 + Task 9 parity. ✓
- §9 phasing (Phase 1 standalone, no `sqlite-vec`/embeddings) → no embedding column, no vec dependency. ✓
- Recall stub present for the Phase-2 seam (Task 6). KV-cache guard is a Phase-2 concern (no recall tail exists yet) — noted in spec §8, not a gap. ✓

**Placeholder scan:** the two loop-wiring tests (Tasks 7/8) deliberately defer fixture construction to implementation time, with explicit instructions to copy existing `tests/test_controller_loop*.py` / `tests/test_tool_loop*.py` wiring — the exact fixture signatures must be read from the codebase, not guessed. Every source step contains complete code.

**Type consistency:** `prepare_turn → TurnPreparation.history`; `maybe_compact → CompactionResult.history`; `summarize(old, evicted) -> str`; `_select_hot(history, hot_budget_tokens, hot_turns_cap) -> (evicted, hot, used)`; `run_id: str` everywhere; `memory_harness: MemoryHarness = NO_OP_HARNESS` identical in both loops; `CompactionSegment` fields (`id, run_id, seq, content, created_at`) consistent across store + compactor. ✓

**One known soft spot:** `make_engine_summarizer` calls `reasoning_engine.generate_text(...)` — the one codebase method the plan hasn't pinned; confirm against `agentd/reasoning/` during Task 6 (flagged inline) and verify via Task 9's live check rather than unit tests.
