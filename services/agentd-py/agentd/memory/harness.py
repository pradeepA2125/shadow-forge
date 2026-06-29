from __future__ import annotations

import asyncio
import json
import logging
import re
import threading

from agentd.memory.compactor import AnchorSummarizer, Compactor
from agentd.memory.config import MemoryConfig
from agentd.memory.models import History, TurnPreparation
from agentd.memory.store import MemoryStore
from agentd.providers.contracts import ModelJsonTransport

logger = logging.getLogger(__name__)

# Running-memory prompt. Modelled on the Claude Code conversation-summarization template
# (sectioned, distill-don't-copy), tightened against two failure modes seen live on a
# weaker local model: (1) the model echoed its JSON input payload back verbatim, (2) the
# anchor ballooned because raw file/tool output was carried in. The <summary> delimiter lets
# us extract the answer and detect echoes. Carry-forward is goal-relevant, not strictly
# lossless: a fully-superseded file may be dropped to keep the note bounded (recency-triage
# is accepted by design — a durable file-ledger outside the LLM summary is the deferred fix).
_SUMMARY_SYSTEM = (
    "You are the assistant in an ongoing AI coding session, writing a note to your future "
    "self. You will be given your PREVIOUS memory note and the NEW messages that are about "
    "to scroll out of view. The raw messages will be discarded and replaced by your note, so "
    "the note must carry everything you need to keep working as if you had read it all.\n"
    "\n"
    "Write the note under these headings. Skip any heading that has nothing to record:\n"
    "1. Goal: what the user is ultimately trying to achieve, in their own words.\n"
    "2. Key concepts: the frameworks, patterns, and conventions in play.\n"
    "3. Files and code: every file, function, class, or symbol read or changed — give the "
    "exact name and a one-line note on what it is for. Keep a short code snippet only when a "
    "later step depends on its exact shape. Never paste whole files.\n"
    "4. Errors and fixes: problems hit and how they were resolved, including failures still "
    "open.\n"
    "5. Decisions: approaches chosen and why, trade-offs weighed, and options ruled out.\n"
    "6. User instructions: the user's explicit requests and feedback, kept close to their "
    "wording.\n"
    "7. Open threads: unfinished work, known bugs, and questions still to answer.\n"
    "8. Current work: what was happening immediately before this note was written.\n"
    "9. Next step: the single action that follows directly from the user's latest request.\n"
    "\n"
    "Rules:\n"
    "- Carry forward the facts, decisions, and identifiers from the PREVIOUS memory note that "
    "are still relevant to the current goal. A file or detail fully superseded by later work "
    "may be dropped to keep the note focused.\n"
    "- Keep identifiers exact: file paths, function names, and error text verbatim. Summarize "
    "everything else and prefer the shortest wording that keeps the fact.\n"
    "- Do not copy raw messages, tool output, or file contents — keep only what is needed to "
    "continue.\n"
    "- Write plain prose under the headings. Do not output JSON, key/value pairs, or the "
    "input you were given, and do not repeat these instructions.\n"
    "- Put the entire note inside one <summary>...</summary> block and write nothing outside "
    "it."
)

_SUMMARY_RE = re.compile(r"<summary>(.*)</summary>", re.DOTALL | re.IGNORECASE)


class SummarizerEchoError(RuntimeError):
    """The summarizer returned its input payload (or empty/JSON) instead of a real summary."""


def _extract_summary(raw: str) -> str:
    """Pull the text inside the <summary>...</summary> block; fall back to the stripped whole."""
    match = _SUMMARY_RE.search(raw)
    return (match.group(1) if match else raw).strip()


def _is_echo(text: str) -> bool:
    """True when the candidate is empty or a JSON object — both signal a failed summary.

    A genuine prose summary never parses as a JSON object; the live failure was the model
    parroting its `{"prior_summary": ..., "evicted_messages": ...}` payload back verbatim.
    """
    stripped = text.strip()
    if not stripped:
        return True
    try:
        return isinstance(json.loads(stripped), dict)
    except (ValueError, TypeError):
        return False


def _render_transcript(old_anchor: str, evicted_text: str) -> str:
    """One plain-text field — a JSON-shaped multi-key payload is what the model echoed."""
    prior = old_anchor.strip() or "(none yet — this is the first memory note)"
    return f"PREVIOUS MEMORY NOTE:\n{prior}\n\nNEW MESSAGES TO FOLD IN:\n{evicted_text}"


class MemoryHarness:
    """The only memory unit the loops see. Compaction (P1) + recall/consolidation (P2)."""

    def __init__(
        self, *, enabled: bool, compactor: Compactor | None,
        consolidator: object | None = None, recall_engine: object | None = None,
        scope_kind: str = "workspace", scope_id: str = "", recall_token_budget: int = 1500,
    ) -> None:
        self._enabled = enabled
        self._compactor = compactor
        self._consolidator = consolidator
        self._recall_engine = recall_engine
        self._scope_kind = scope_kind
        self._scope_id = scope_id
        self._recall_token_budget = recall_token_budget
        # Read segments for the evicted-slice transcript; derived from the compactor's store.
        self._store = getattr(compactor, "_store", None) if compactor is not None else None
        self._bg_tasks: set[asyncio.Task[None]] = set()  # FIX #5: hold refs so tasks aren't GC'd
        self._recall_cache: dict[str, list[str]] = {}  # per-run rendered recall
        self._recall_key: str | None = None  # last (run_id::query) recalled, to dedup per turn

    async def prepare_turn(
        self, history: History, run_id: str, query: str = "",
    ) -> TurnPreparation:
        if not self._enabled:
            return TurnPreparation(history=history, recalled_memories=[], compacted=False)
        # Compaction (only when a compactor is wired) — best-effort.
        result = None
        if self._compactor is not None:
            try:
                result = await self._compactor.maybe_compact(history, run_id)
            except Exception:  # best-effort: memory must never break a loop iteration
                logger.warning("[memory] compaction failed for run=%s", run_id, exc_info=True)
        prep = TurnPreparation(
            history=result.history if result else history,
            recalled_memories=[],
            compacted=bool(result and result.compacted),
            evicted_count=result.evicted_count if result else 0,
            anchor_version=result.anchor_version if result else 0,
            evicted_seq_lo=result.evicted_seq_lo if result else None,
            evicted_seq_hi=result.evicted_seq_hi if result else None,
        )
        if (result and result.compacted and self._consolidator is not None
                and result.evicted_seq_hi is not None):
            self.schedule_consolidation(
                run_id, self._scope_kind, self._scope_id,
                transcript=self._render_segments(run_id, result.evicted_seq_lo,
                                                 result.evicted_seq_hi),
                seq_lo=result.evicted_seq_lo, seq_hi=result.evicted_seq_hi,
            )
        # Recall (every turn, cached per query) — best-effort.
        if self._recall_engine is not None:
            prep.recalled_memories = await self._fill_recall(history, run_id, query)
        return prep

    async def _fill_recall(self, history: History, run_id: str, query: str = "") -> list[str]:
        # The current user message is in the loop's plan_context (`goal`), NOT in `history` on
        # the first turn — so prefer the explicit query; fall back to the last user msg in history.
        query = query.strip() or self._recall_query(history)
        if not query:
            return self._recall_cache.get(run_id, [])
        key = f"{run_id}::{query}"
        if key != self._recall_key:
            try:
                mems = await self._recall_engine.recall(  # type: ignore[union-attr]
                    query, self._scope_kind, self._scope_id, k=8)
                self._recall_cache[run_id] = self._render_recall(mems)
            except Exception:  # noqa: BLE001 — best-effort
                logger.warning("[memory] recall failed for run=%s", run_id)
                self._recall_cache[run_id] = []
            self._recall_key = key
        return self._recall_cache.get(run_id, [])

    @staticmethod
    def _recall_query(history: History) -> str:
        for m in reversed(history):
            if m.get("role") == "user":
                return str(m.get("content", ""))[:500]
        return ""

    def _render_recall(self, mems: list[object]) -> list[str]:
        out: list[str] = []
        budget = self._recall_token_budget
        for m in mems:
            line = f"- ({getattr(m, 'kind', '?')}) {getattr(m, 'content', '')}"
            budget -= max(1, len(line) // 4)
            if budget < 0:
                break
            out.append(line)
        return out

    def schedule_consolidation(
        self, run_id: str, scope_kind: str, scope_id: str, transcript: str,
        seq_lo: int | None, seq_hi: int | None,
    ) -> None:
        # FIX #5: (a) no-op if no consolidator or no running loop (sync caller) — never crash;
        # (b) hold a strong ref or the loop may GC the task mid-flight.
        if self._consolidator is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("[memory] no running loop; skipping consolidation for run=%s", run_id)
            return

        async def _run() -> None:
            try:
                await self._consolidator.consolidate(  # type: ignore[union-attr]
                    run_id, scope_kind, scope_id, transcript, seq_lo, seq_hi)
            except Exception:  # noqa: BLE001 — best-effort
                logger.warning("[memory] scheduled consolidation failed for run=%s", run_id)

        task = loop.create_task(_run())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _render_segments(self, run_id: str, seq_lo: int | None, seq_hi: int | None) -> str:
        if self._store is None or seq_lo is None or seq_hi is None:
            return ""
        segs = [s for s in self._store.get_segments(run_id) if seq_lo <= s.seq <= seq_hi]
        return "\n".join(s.content for s in segs)

    def memory_tool_source(self) -> object | None:
        """A MemoryToolSource (remember + recall) for the controller registry, or None when
        memory has no consolidator wired (disabled / compaction-only)."""
        if self._consolidator is None:
            return None
        from agentd.memory.tool_source import MemoryToolSource
        return MemoryToolSource(
            self._consolidator, self._scope_kind, self._scope_id,
            recall_engine=self._recall_engine, store=self._store,
        )

    async def recall(self, query: str, run_id: str) -> History:
        return []  # Phase 2 (recall slot filled in Plan 2C)


NO_OP_HARNESS = MemoryHarness(enabled=False, compactor=None)


def make_engine_summarizer(transport: ModelJsonTransport, model: str) -> AnchorSummarizer:
    async def _summarize(old_anchor: str, evicted_text: str) -> str:
        payload: dict[str, object] = {"transcript": _render_transcript(old_anchor, evicted_text)}
        # One retry: a weaker model intermittently echoes its input instead of summarizing.
        # On the second echo we raise so the Compactor degrades (keeps the prior anchor)
        # rather than persisting garbage as the new anchor.
        for attempt in range(2):
            raw = await transport.generate_text(
                model=model, system_instructions=_SUMMARY_SYSTEM, user_payload=payload
            )
            summary = _extract_summary(raw)
            if not _is_echo(summary):
                return summary
            logger.warning(
                "[memory] summarizer echoed input (attempt %d/2) for model=%s", attempt + 1, model
            )
        raise SummarizerEchoError("summarizer returned no usable summary after retry")

    return _summarize


def build_memory_harness(
    config: MemoryConfig, transport: ModelJsonTransport, model: str,
    *, workspace_path: str = "",
) -> MemoryHarness:
    if not config.enabled:
        return NO_OP_HARNESS
    store = MemoryStore(config.db_path)
    compactor = Compactor(
        store,
        make_engine_summarizer(transport, model),
        window_tokens=config.window_tokens,
        trigger_frac=config.trigger_frac,
        hot_token_frac=config.hot_token_frac,
        hot_turns=config.hot_turns,
    )
    consolidator: object | None = None
    recall_engine: object | None = None
    if workspace_path:  # memory needs a workspace scope; without one, compaction-only
        from agentd.memory.consolidator import Consolidator, make_engine_consolidator
        from agentd.memory.embedder import Embedder
        from agentd.memory.recall import RecallEngine
        embedder = Embedder(config.embedding_model)  # shared by write + read paths (one load)
        consolidator = Consolidator(
            store, embedder, make_engine_consolidator(transport, model),
            dedup_threshold=config.dedup_threshold,
        )
        recall_engine = RecallEngine(store, embedder, weights=config.weights)
        # FIX #3: warm the model in a background thread so the first real turn doesn't eat the
        # ~130MB load. A daemon thread works regardless of event-loop state at construction.
        threading.Thread(target=embedder.warmup, daemon=True).start()
    return MemoryHarness(
        enabled=True, compactor=compactor, consolidator=consolidator,
        recall_engine=recall_engine, scope_kind="workspace", scope_id=workspace_path,
        recall_token_budget=config.recall_token_budget,
    )
