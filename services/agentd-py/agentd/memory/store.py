from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import sqlite_vec

from agentd.memory.models import AnchoredSummary, CompactionSegment

logger = logging.getLogger(__name__)

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
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    scope_kind TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    entities TEXT NOT NULL,
    importance INTEGER NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    superseded_by TEXT,
    source_kind TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    source_seq_lo INTEGER,
    source_seq_hi INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope_kind, scope_id, valid_to);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    memory_id UNINDEXED, content, entities
);
"""


class MemoryStore:
    """The only DB-aware unit. SQLite-backed compaction segments + anchored summaries +
    long-term memories (vectors in a co-located sqlite-vec table when available)."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        self._vec_enabled = self._try_load_vec()
        self._conn.executescript(_SCHEMA)  # base tables + FTS5 (always)
        if self._vec_enabled:
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS vec_memories "
                "USING vec0(memory_id TEXT PRIMARY KEY, embedding float[384])"
            )
        self._conn.commit()

    def _try_load_vec(self) -> bool:
        # FIX #1: a missing extension must NOT crash the store — Phase 1 compaction uses it too.
        try:
            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
            self._conn.enable_load_extension(False)
            return True
        except Exception:  # noqa: BLE001 — degrade to FTS5-only
            logger.warning("[memory] sqlite-vec unavailable; semantic search disabled (FTS5-only)")
            return False

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
                id=r["id"],
                run_id=r["run_id"],
                seq=r["seq"],
                content=r["content"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    def next_seq(self, run_id: str) -> int:
        """Run-monotonic seq so ordering is stable across many compaction rounds."""
        r = self._conn.execute(
            "SELECT MAX(seq) AS m FROM compaction_segments WHERE run_id=?", (run_id,)
        ).fetchone()
        return 0 if r["m"] is None else r["m"] + 1

    def upsert_anchor(self, run_id: str, summary_md: str) -> AnchoredSummary:
        now = datetime.now(UTC)
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
        return AnchoredSummary(
            run_id=run_id, summary_md=summary_md, version=version, updated_at=now
        )

    def get_anchor(self, run_id: str) -> AnchoredSummary | None:
        r = self._conn.execute(
            "SELECT * FROM anchored_summaries WHERE run_id=?", (run_id,)
        ).fetchone()
        if r is None:
            return None
        return AnchoredSummary(
            run_id=r["run_id"],
            summary_md=r["summary_md"],
            version=r["version"],
            updated_at=datetime.fromisoformat(r["updated_at"]),
        )
