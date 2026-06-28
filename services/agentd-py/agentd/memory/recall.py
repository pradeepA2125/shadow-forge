from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Callable
from datetime import UTC, datetime

from agentd.memory.embedder import Embedder
from agentd.memory.models import Memory
from agentd.memory.store import MemoryStore

logger = logging.getLogger(__name__)

W_IMP = 0.3
W_REC = 0.2
HALF_LIFE_DAYS = 14.0


def _minmax(values: list[float]) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def _recency(valid_from: datetime, now: datetime, half_life_days: float) -> float:
    days = max(0.0, (now - valid_from).total_seconds() / 86400.0)
    return math.exp(-days / half_life_days)


def _fuse(
    memories: list[Memory], sem: dict[str, float], lex: dict[str, float],
    struct: dict[str, float], weights: tuple[float, float, float], now: datetime,
) -> list[tuple[Memory, float]]:
    if not memories:
        return []
    w_sem, w_lex, w_struct = weights
    ids = [m.id for m in memories]
    n_sem = dict(zip(ids, _minmax([sem.get(i, 0.0) for i in ids]), strict=True))
    n_lex = dict(zip(ids, _minmax([lex.get(i, 0.0) for i in ids]), strict=True))
    n_str = dict(zip(ids, _minmax([struct.get(i, 0.0) for i in ids]), strict=True))
    n_imp = dict(zip(ids, _minmax([float(m.importance) for m in memories]), strict=True))
    scored: list[tuple[Memory, float]] = []
    for m in memories:
        s = (w_sem * n_sem[m.id] + w_lex * n_lex[m.id] + w_struct * n_str[m.id]
             + W_IMP * n_imp[m.id] + W_REC * _recency(m.valid_from, now, HALF_LIFE_DAYS))
        scored.append((m, s))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


_ENTITY_RE = re.compile(r"[\w./:]+")


def _query_entities(query: str) -> set[str]:
    # path-ish tokens: contain a / . or : (e.g. src/tax.py, foo.py:Bar)
    return {t for t in _ENTITY_RE.findall(query) if any(c in t for c in "/.:")}


class RecallEngine:
    def __init__(
        self, store: MemoryStore, embedder: Embedder, *,
        weights: tuple[float, float, float], candidate_k: int = 30, min_score: float = 0.15,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._weights = weights
        self._cand_k = candidate_k
        self._min_score = min_score  # FIX #7: relevance floor

    async def recall(self, query: str, scope_kind: str, scope_id: str, k: int) -> list[Memory]:
        try:
            return await self._recall(query, scope_kind, scope_id, k)
        except Exception:  # noqa: BLE001 — best-effort: never break the turn
            logger.warning("[memory] recall failed for scope=%s", scope_id)
            return []

    async def _recall(self, query: str, scope_kind: str, scope_id: str, k: int) -> list[Memory]:
        sem: dict[str, float] = {}
        if self._embedder.available:
            # FIX #3: embed off the event loop (sync CPU + first-call model load).
            vecs = await asyncio.to_thread(self._embedder.embed, [query])
            if vecs:
                for mid, dist in self._store.search_semantic(vecs[0], self._cand_k,
                                                             scope_kind, scope_id):
                    sem[mid] = 1.0 - (dist * dist) / 2.0  # cosine from L2 (unit vectors)
        lex: dict[str, float] = {}
        for mid, rank in self._store.search_lexical(query, self._cand_k, scope_kind, scope_id):
            lex[mid] = -rank  # bm25: lower rank = better → negate so higher = better
        qents = _query_entities(query)
        ids = set(sem) | set(lex)
        mems = [m for m in (self._store.get_memory(i) for i in ids) if m is not None]
        struct = {m.id: float(len(qents & set(m.entities))) for m in mems}
        ranked = _fuse(mems, sem, lex, struct, self._weights, datetime.now(UTC))
        # FIX #7: drop weak matches so a no-relevant-memory turn injects nothing.
        return [m for m, score in ranked[:k] if score >= self._min_score]

    async def recall_grounded(
        self, query: str, scope_kind: str, scope_id: str, k: int,
        ground: Callable[[str], str] | None = None,
    ) -> list[str]:
        """Recall + render to lines; optionally ground the top 1-2 in the code graph."""
        mems = await self.recall(query, scope_kind, scope_id, k)
        lines = [f"- ({m.kind}) {m.content}" for m in mems]
        if ground is not None:
            for i, m in enumerate(mems[:2]):  # top 1-2 only (cost-bounded)
                if not m.entities:
                    continue
                try:
                    g = ground(m.entities[0])
                    if g:
                        lines[i] += f"  (grounding: {g[:120]})"
                except Exception:  # noqa: BLE001 — best-effort: grounding never breaks recall
                    logger.warning("[memory] grounding failed for entity=%s", m.entities[0])
        return lines
