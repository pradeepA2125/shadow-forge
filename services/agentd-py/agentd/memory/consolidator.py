from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import uuid4

from agentd.memory.embedder import Embedder
from agentd.memory.models import CandidateMemory, Memory
from agentd.memory.store import MemoryStore

logger = logging.getLogger(__name__)

DistillFn = Callable[[str, list[Memory]], Awaitable[list[CandidateMemory]]]

CANDIDATE_MEMORY_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "memories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string",
                             "enum": ["episodic", "semantic", "procedural"]},
                    "content": {"type": "string"},
                    "entities": {"type": "array", "items": {"type": "string"}},
                    "importance": {"type": "integer"},
                    "contradicts": {"type": ["string", "null"]},
                },
                "required": ["kind", "content", "entities", "importance"],
            },
        }
    },
    "required": ["memories"],
}

_CONSOLIDATION_SYSTEM = (
    "You are the memory keeper for an AI coding agent. From the recent transcript you EXTRACT a "
    "few discrete, durable notes your future self would want when returning to this project. You "
    "are also given the EXISTING MEMORIES (each with an id) so you don't repeat them. Accuracy "
    "matters more than coverage — propose only what is genuinely worth recalling later.\n"
    "\n"
    "Each note has a kind:\n"
    "- episodic: a specific thing that happened this session "
    "(e.g. \"User rejected the first plan and asked to keep the change minimal\"). "
    "Episodic notes are immutable — NEVER set contradicts on them.\n"
    "- semantic: a durable fact about the code/project/user "
    "(e.g. \"Patch ops are applied in patch/engine.py; it supports 7 op types\").\n"
    "- procedural: a reusable how-to / process "
    "(e.g. \"Run the backend via start-backend.sh, always quoting --workspace\"). "
    "Procedural is the hardest to spot — only record a genuinely reusable method.\n"
    "\n"
    "Rules:\n"
    "- Extract discrete, atomic facts — one self-contained idea per note, written as a short, "
    "present-tense, declarative statement. Strip filler (\"ok\", \"I think\", \"let's\").\n"
    "- Keep entities exact: list the verbatim file paths and path:Symbol tokens the note is "
    "about.\n"
    "- importance: rate 1-10 how much this would help a future session (a project-wide fact or "
    "decision = high; a one-off detail = low).\n"
    "- contradicts: set to an EXISTING MEMORY id only when your note directly conflicts with it "
    "(a fact that changed). Never for episodic.\n"
    "- Do NOT record: ephemeral chit-chat, tool mechanics (file reads/searches), restating code "
    "that is obvious from the repo, or anything already in EXISTING MEMORIES. If nothing is worth "
    "keeping, return an empty list.\n"
    "\n"
    "EXAMPLE (format only — NEVER output these example notes themselves):\n"
    "Transcript:\n"
    "  user: read services/tax.py and walk me through compute_vat\n"
    "  assistant: compute_vat in services/tax.py applies the standard rate from config.RATES\n"
    "  user: from now on always run ruff before you commit\n"
    "  user: actually we switched the embedder to bge-small, not openai\n"
    "Good notes:\n"
    "  - semantic: \"compute_vat in services/tax.py reads the rate from config.RATES\" "
    "entities=[services/tax.py:compute_vat] importance=6\n"
    "  - procedural: \"Run ruff before every commit\" entities=[] importance=7\n"
    "  - semantic (contradicts the existing 'uses openai embeddings' note): "
    "\"The project uses the bge-small embedder\" entities=[] importance=8\n"
    "You would NOT note the file-read request — that is mechanics."
)


def _render_existing(existing: list[Memory]) -> str:
    if not existing:
        return "(none)"
    return "\n".join(f"[{m.id}] ({m.kind}) {m.content}" for m in existing)


def _parse_candidate(item: object) -> CandidateMemory | None:
    # Per-item validation: a single malformed candidate must not discard the good ones.
    if not isinstance(item, dict):
        return None
    try:
        c = CandidateMemory.model_validate(item)
    except Exception:  # noqa: BLE001
        return None
    c.importance = max(1, min(10, c.importance))  # clamp so out-of-range never skews recall
    return c


def make_engine_consolidator(transport: object, model: str) -> DistillFn:
    async def _distill(transcript: str, existing: list[Memory]) -> list[CandidateMemory]:
        payload: dict[str, object] = {
            "transcript": f"{transcript}\n\nEXISTING MEMORIES (with ids):\n"
                          f"{_render_existing(existing)}"
        }
        try:
            raw = await transport.generate_json(  # type: ignore[attr-defined]
                model=model, schema_name="consolidated_memories",
                schema=CANDIDATE_MEMORY_SCHEMA, system_instructions=_CONSOLIDATION_SYSTEM,
                user_payload=payload,
            )
        except Exception:  # noqa: BLE001 — best-effort: never break the turn
            logger.warning("[memory] consolidation distill failed for model=%s", model)
            return []
        items = raw.get("memories", []) if isinstance(raw, dict) else []
        out: list[CandidateMemory] = []
        for it in items if isinstance(items, list) else []:
            parsed = _parse_candidate(it)
            if parsed is not None:
                out.append(parsed)
        return out

    return _distill


def _cosine_from_l2(distance: float) -> float:
    # unit vectors: ||a-b||^2 = 2 - 2cos  =>  cos = 1 - d^2/2
    return 1.0 - (distance * distance) / 2.0


class Consolidator:
    """Async write path: LLM proposes candidates; Python disposes (embed/dedupe/supersede)."""

    def __init__(
        self, store: MemoryStore, embedder: Embedder, distill: DistillFn,
        *, similar_k: int = 5, dedup_threshold: float = 0.92,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._distill = distill
        self._k = similar_k
        self._dedup = dedup_threshold

    async def consolidate(
        self, run_id: str, scope_kind: str, scope_id: str, transcript: str,
        seq_lo: int | None, seq_hi: int | None,
    ) -> int:
        # FIX #4: bound the existing-context fed to the LLM — ALL live memories would grow the
        # prompt without limit on a mature workspace until consolidation fails every time.
        existing = self._bounded_existing(scope_kind, scope_id)
        candidates = await self._distill(transcript, existing)
        inserted = 0
        for c in candidates:
            emb = await self._embed(c.content)  # FIX #3: off the event loop
            if self._dispose(c, emb, run_id, scope_kind, scope_id, "consolidation",
                             seq_lo, seq_hi):
                inserted += 1
        return inserted

    def _bounded_existing(self, scope_kind: str, scope_id: str, cap: int = 20) -> list[Memory]:
        live = self._store.get_live_memories(scope_kind, scope_id)
        # most-important first, then most-recent — the set most likely to be contradicted.
        live.sort(key=lambda m: (m.importance, m.valid_from), reverse=True)
        return live[:cap]

    async def _embed(self, text: str) -> list[float]:
        # FIX #3: sentence-transformers encode is sync CPU (and the first call downloads the
        # model). Running it on the asyncio loop freezes every turn + all SSE. Offload to a thread.
        vec = await asyncio.to_thread(self._embedder.embed, [text])
        return vec[0] if vec else []

    async def write_explicit(
        self, content: str, kind: str, entities: list[str], scope_kind: str, scope_id: str,
    ) -> str:
        c = CandidateMemory(kind=kind, content=content, entities=entities, importance=8)
        emb = await self._embed(content)  # FIX #3
        mem = self._build_memory(c, run_id="", scope_kind=scope_kind, scope_id=scope_id,
                                 source_kind="agent_tool", seq_lo=None, seq_hi=None)
        self._store.insert_memory(mem, emb)
        return mem.id

    def _dispose(
        self, c: CandidateMemory, emb: list[float], run_id: str, scope_kind: str,
        scope_id: str, source_kind: str, seq_lo: int | None, seq_hi: int | None,
    ) -> bool:
        # CONCURRENCY INVARIANT: _dispose (and similar_memories/insert_memory/supersede) MUST be
        # await-free — it runs as a background task sharing ONE sqlite3 connection with the
        # foreground turn; the only safe yield point is BEFORE this, in _distill/_embed.
        if emb:  # Dedupe: drop a near-identical live memory of same kind+scope.
            for _mem, dist in self._store.similar_memories(emb, c.kind, scope_kind, scope_id,
                                                           self._k):
                if _cosine_from_l2(dist) >= self._dedup:
                    return False
        new = self._build_memory(c, run_id, scope_kind, scope_id, source_kind, seq_lo, seq_hi)
        # Supersede: only when the LLM flagged a conflict AND the kind is not episodic.
        if c.kind != "episodic" and c.contradicts and self._store.get_memory(c.contradicts):
            self._store.supersede(c.contradicts, new, emb)
            return True
        self._store.insert_memory(new, emb)
        return True

    def _build_memory(
        self, c: CandidateMemory, run_id: str, scope_kind: str, scope_id: str,
        source_kind: str, seq_lo: int | None, seq_hi: int | None,
    ) -> Memory:
        now = datetime.now(UTC)
        return Memory(
            id=uuid4().hex, scope_kind=scope_kind, scope_id=scope_id, kind=c.kind,
            content=c.content, entities=c.entities, importance=c.importance,
            valid_from=now, valid_to=None, superseded_by=None, source_kind=source_kind,
            source_ref=run_id, source_seq_lo=seq_lo, source_seq_hi=seq_hi, created_at=now,
        )
