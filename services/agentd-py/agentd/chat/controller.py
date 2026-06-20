"""ChatController — dynamic agentic chat handler (flag-selected vs ChatAgent).

Mirrors ChatAgent's public surface (handle_message + _store/_broadcaster attrs
the route reads) but runs ONE ControllerLoop per turn instead of the
explore→classify→route pipeline. F1 implements QA + clarify; propose_mode gate
(F2) and the per-edit review gate (F3) build on this.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from agentd.chat.controller_loop import ControllerLoop, ControllerOutcome
from agentd.chat.controller_phase import ControllerPhaseSM
from agentd.chat.edit_session import TurnEditSession
from agentd.chat.models import ChatMessage, PendingGate
from agentd.tools.sources import AggregatingToolRegistry, BuiltinToolSource

if TYPE_CHECKING:
    from agentd.chat.storage import ChatThreadStore
    from agentd.domain.models import DiffEntry
    from agentd.orchestrator.broadcaster import EventBroadcaster
    from agentd.orchestrator.engine import AgentOrchestrator
    from agentd.reasoning.contracts import ReasoningEngine
    from agentd.retrieval.artifact_client import RetrievalArtifactClient

logger = logging.getLogger(__name__)

# Seconds to hold a per-edit review gate open before auto-rejecting (0 = forever).
# Mirrors AI_EDITOR_COMMAND_DECISION_TIMEOUT_SEC; guards against a dropped SSE client
# leaving the turn hung on a future that never resolves.
_EDIT_DECISION_TIMEOUT_ENV = "AI_EDITOR_CHAT_EDIT_DECISION_TIMEOUT_SEC"


def _explore_context_from_history(
    history: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Derive the planner's pre_explored_context from the controller's turn history.

    Walks the verbatim conversation and emits one entry per *tool call* — pairing
    each ``tool_call`` assistant turn with its following ``tool_result`` turn. Edits,
    terminals (answer/clarify/propose_mode/submit_changes), correction/dedup ``{}``
    turns and retrieval-refresh notes have no ``type=="tool_call"`` assistant and are
    naturally excluded. The result is the tool_result's full content — uncapped, since
    the history holds the verbatim output (unlike the 4000-capped tool_events pills).
    ``is_error`` is not carried in the history shape, so it defaults to False; the
    error text, when any, is already in the result content.
    """
    out: list[dict[str, object]] = []
    for index, entry in enumerate(history):
        if entry.get("role") != "assistant":
            continue
        raw = entry.get("content")
        if not isinstance(raw, str):
            continue
        try:
            action = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(action, dict) or action.get("type") != "tool_call":
            continue
        nxt = history[index + 1] if index + 1 < len(history) else None
        result = ""
        if isinstance(nxt, dict) and nxt.get("role") == "tool_result":
            result = str(nxt.get("content", ""))
        out.append({
            "tool": action.get("tool", ""),
            "args": action.get("args", {}),
            "result": result,
            "is_error": False,
        })
    return out


class ChatController:
    def __init__(
        self,
        *,
        workspace_path: str,
        reasoning_engine: ReasoningEngine,
        thread_store: ChatThreadStore,
        orchestrator: AgentOrchestrator | None,
        broadcaster: EventBroadcaster,
        retrieval_client: RetrievalArtifactClient | None = None,
    ) -> None:
        self._workspace_path = workspace_path
        self._reasoning = reasoning_engine
        self._store = thread_store
        self._orchestrator = orchestrator
        self._broadcaster = broadcaster
        self._retrieval = retrieval_client
        # Per-thread controller conversation history — the cache prefix replayed as
        # seed_history on the next turn (clarify/discuss resume, spec §12).
        # TODO(controller): unbounded per-thread growth; eviction/compaction is owned
        # by the future agent-memory module (spec §6 defers it). Fine for v1.
        self._histories: dict[str, list[dict[str, object]]] = {}
        # Per-thread retrieval seed — computed once, never rewritten (spec §6 cache
        # discipline: a frozen pointer-set placed before history).
        self._seeds: dict[str, dict[str, object] | None] = {}
        # Per-thread per-edit review future (held-open gate; mirrors the engine's
        # _pending_step_decisions). resolve_edit fires it.
        self._pending_edit: dict[str, asyncio.Future[dict[str, object]]] = {}
        # Per-thread "Review each edit" toggle from the message that opened the mode
        # gate — read back in resolve_mode so the edit re-entry honors it (the
        # /mode-decision POST carries no step_review; smoke-found gap #4).
        self._step_review_by_thread: dict[str, bool | None] = {}
        # Threads whose last turn ended on an EDIT-phase clarify: the next user message
        # is the answer and must RESUME the loop in EDIT (not restart at DECIDE, which
        # would force re-picking the mode). In-memory like _step_review_by_thread — a
        # backend restart between the question and the reply degrades gracefully to a
        # DECIDE turn (the agent re-proposes the mode from rehydrated history).
        self._edit_clarify_pending: set[str] = set()
        # In-memory registry of the one detached turn per thread (mirrors the
        # orchestrator's _running_tasks). Earns its keep three ways: the in-flight
        # 409 guard (routes), the durable `turn_active` input signal (/live), and the
        # task handle stop_turn cancels. A backend restart clears it — the orphaned
        # turn is dead anyway (the transcript + pending_controller_gate survive in sqlite).
        self._active_turns: dict[str, asyncio.Task] = {}

    def launch_turn(
        self, thread_id: str, coro, *, channel_id: str | None = None,
    ) -> asyncio.Task:
        """Detach a turn: create the task, register it, return the handle.

        create_task + the dict assignment have no `await` between them, so the
        in-flight guard (routes: `thread_id in _active_turns`) is race-safe in
        asyncio — same posture as the task routes' `_in_flight_*` guards."""
        task = asyncio.create_task(self._run_turn(thread_id, coro, channel_id))
        self._active_turns[thread_id] = task
        return task

    async def _run_turn(
        self, thread_id: str, coro, channel_id: str | None = None,
    ) -> None:
        """Run a turn coroutine and unconditionally clear its registry entry.

        The `finally` fires on normal completion, on error, AND on cancellation
        (stop_turn) — the single owner releasing its own slot so the thread never
        stays falsely `turn_active`. An unexpected exception is swallowed + logged
        and a failsafe chat_done is broadcast so the detached relay never hangs
        (a crashed turn that emitted no chat_done)."""
        try:
            await coro
        except asyncio.CancelledError:
            raise  # stop_turn / shutdown — re-raise so the task is marked cancelled
        except Exception:
            logger.exception("[controller] turn failed (thread=%s)", thread_id)
            if channel_id is not None:
                self._broadcaster.broadcast(
                    channel_id, {"type": "chat_done", "payload": {}})
        finally:
            self._active_turns.pop(thread_id, None)

    def _build_registry(self) -> AggregatingToolRegistry:
        return AggregatingToolRegistry([BuiltinToolSource(
            shadow_root=Path(self._workspace_path),
            real_workspace_path=Path(self._workspace_path),
            semantic_index=getattr(self._retrieval, "_semantic_index", None),
        )])

    def _seed_for(self, thread_id: str) -> list[dict[str, object]]:
        """The thread's prior controller turn history to replay as seed_history.

        In-memory cache first; on a miss (e.g. a backend restart cleared it) rehydrate
        from the durable store and re-cache — so the conversation the transcript still
        shows is not lost from the model's context (mirrors the planner replaying
        TaskRecord.planning_conversation_history on a feedback round)."""
        cached = self._histories.get(thread_id)
        if cached is not None:
            return cached
        thread = self._store.get_thread(thread_id)
        history = (thread.controller_conversation_history if thread else None) or []
        self._histories[thread_id] = history
        return history

    def _retrieval_seed(self, thread_id: str, goal: str) -> dict[str, object] | None:
        """Compute the thread's retrieval seed once, then reuse it byte-for-byte so
        the cached payload prefix stays stable across turns (spec §6) AND across a
        backend restart: the seed is pinned durably and replayed verbatim, mirroring
        the planner's planning_initial_context. Retrieval changes ride the history
        tail as delta notes, so the seed itself is frozen for the thread's life — a
        re-indexed snapshot must NOT recompute it (that would break the KV prefix)."""
        if thread_id in self._seeds:
            return self._seeds[thread_id]
        # Rehydrate a pinned seed from the store (the restart path) before recomputing.
        thread = self._store.get_thread(thread_id)
        if thread is not None and thread.controller_retrieval_seed is not None:
            self._seeds[thread_id] = thread.controller_retrieval_seed
            return thread.controller_retrieval_seed
        seed: dict[str, object] | None = None
        if self._retrieval is not None:
            try:
                context, _ = self._retrieval.load_context(self._workspace_path, goal)
                seed = context.as_prompt_payload()
            except Exception:
                logger.debug("[controller] retrieval seed failed", exc_info=True)
        self._seeds[thread_id] = seed
        # Pin on first compute so a later restart replays these exact bytes.
        self._store.set_controller_seed(thread_id, seed)
        return seed

    async def handle_message(
        self, thread_id: str, message: str, channel_id: str, step_review: bool | None = None,
    ) -> None:
        thread = self._store.get_thread(thread_id)
        if thread is None:
            raise ValueError(f"Thread {thread_id!r} not found")
        # A new turn can never leave a stale gate rendered: clear it at the start so a
        # late decision on a superseded card hits `gate is None` and no-ops (resolve_mode/
        # resolve_edit already guard on this). A clarify sets no gate, so this is a no-op
        # on the clarify/EDIT-clarify resume path — no conflict.
        self._store.set_controller_gate(thread_id, None)
        # Auto-name the thread from its first user message (mirrors ChatAgent).
        if not any(m.role == "user" for m in thread.messages):
            title = message.strip().replace("\n", " ")[:50]
            self._store.update_title(thread_id, title)
            self._broadcaster.broadcast(channel_id, {
                "type": "thread_title_updated",
                "payload": {"thread_id": thread_id, "title": title},
            })
        self._store.append_message(thread_id, ChatMessage(role="user", content=message))
        # Remember this turn's review toggle so a propose_mode → "edit" re-entry
        # (resolved via /mode-decision, which carries no step_review) honors it.
        self._step_review_by_thread[thread_id] = step_review

        seed = self._seed_for(thread_id)
        # On a continued turn (clarify/discuss), append the user's reply to the
        # prior history and replay it as the cache prefix (spec §12 clarify resume).
        seed_history = (seed + [{"role": "user", "content": message}]) if seed else None
        # If the prior turn ended on an EDIT-phase clarify, this reply is the answer:
        # resume in EDIT so the agent keeps editing rather than re-proposing the mode.
        # Requires the orchestrator (the edit session needs it); without one we can't
        # rebuild EDIT, so fall back to DECIDE.
        resume_phase = (
            "EDIT"
            if thread_id in self._edit_clarify_pending and self._orchestrator is not None
            else None
        )
        self._edit_clarify_pending.discard(thread_id)
        outcome = await self._run_loop(
            thread_id, channel_id, message, seed_history=seed_history,
            step_review=step_review, phase=resume_phase)
        await self._finish(thread_id, channel_id, outcome, step_review)

    async def _run_loop(
        self, thread_id: str, channel_id: str, goal: str, *,
        seed_history: list[dict[str, object]] | None, step_review: bool | None,
        phase: str | None = None,
    ) -> ControllerOutcome:
        sm = ControllerPhaseSM()
        # Edits only happen in EDIT phase (entered via /mode-decision). A DECIDE turn
        # never reaches the edit branch, so the session — which needs the orchestrator's
        # workspace_manager/patch_engine — is built lazily only when editing.
        edit = None
        if phase == "EDIT":
            sm.enter_edit_mode()
            if self._orchestrator is not None:
                edit = TurnEditSession(
                    turn_id=thread_id, real_path=Path(self._workspace_path),
                    workspace_manager=self._orchestrator._workspace_manager,
                    patch_engine=self._orchestrator._patch_engine)
        loop = ControllerLoop(
            self._reasoning, self._build_registry(), self._broadcaster,
            channel_id=channel_id, phase_sm=sm, edit_session=edit)
        plan_context: dict[str, object] = {
            "goal": goal, "workspace_path": self._workspace_path}
        seed = self._retrieval_seed(thread_id, goal)
        if seed:
            plan_context["retrieval_seed"] = seed
        # "Review each edit" on → hold each patch for a decision; off → instant promote.
        is_review = step_review is True
        edit_cb = partial(self._edit_decision_cb, thread_id, channel_id) \
            if is_review else None
        # Single durable-record writer for every edit resolution (both modes): persists
        # an inert diff_card, + a breadcrumb (review) or a live render (auto-accept).
        record_cb = partial(self._edit_record_cb, thread_id, channel_id, is_review)
        outcome = await loop.run(
            plan_context, seed_history=seed_history,
            auto_accept_edits=(not is_review), edit_decision_cb=edit_cb,
            edit_record_cb=record_cb, retrieval_delta_cb=self._retrieval_delta_cb)
        self._histories[thread_id] = outcome.history or []
        # Durably persist the verbatim turn history so a backend restart rehydrates
        # seed_history instead of re-exploring cold (mirrors the planner persisting
        # planning_conversation_history on the TaskRecord).
        self._store.set_controller_history(thread_id, outcome.history or [])
        # Mark/clear EDIT-clarify resume: a clarify emitted while in EDIT must resume
        # in EDIT on the user's reply. Any other terminal (submit/edit-then-submit,
        # answer) clears it. sm.phase reflects the phase the loop ran in (EDIT is
        # one-way, never transitions back).
        if outcome.kind == "clarify" and sm.phase == "EDIT":
            self._edit_clarify_pending.add(thread_id)
        else:
            self._edit_clarify_pending.discard(thread_id)
        return outcome

    async def _retrieval_delta_cb(self, touched: list[str]) -> str | None:
        """Append-only retrieval delta after an accepted edit (spec §6).

        v1 returns a compact pointer note rather than recomputed neighbors: the
        edits are instant-promoted to real, so the live tools (read_file/search_code/
        query_graph) are the always-current source; a real neighbor recompute would
        need a fresh snapshot, which the self-updating watcher rebuilds async. The
        note never touches `retrieval_seed` (cache-prefix immutability)."""
        if not touched:
            return None
        return (
            f"Workspace changed: edited {touched}. These edits are live on the real "
            "workspace — use read_file/search_code for current contents and query_graph "
            "for updated neighbors. (The retrieval seed is from session start.)")

    async def _finish(
        self, thread_id: str, channel_id: str, outcome: ControllerOutcome,
        step_review: bool | None,
    ) -> None:
        if outcome.kind in ("answer", "clarify"):
            # Persist the turn's tool pills + thinking onto the message so they survive
            # a reload (live SSE pills/thinking die) — mirrors ChatAgent's metadata.
            self._store.append_message(thread_id, ChatMessage(
                role="agent", content=outcome.text, metadata=self._turn_metadata(outcome)))
            self._broadcaster.broadcast(
                channel_id, {"type": "chat_response", "payload": {"chunk": outcome.text}})
            self._broadcaster.broadcast(channel_id, {"type": "chat_done", "payload": {}})
        elif outcome.kind == "submit_changes":
            # Persist the EDIT turn's summary + exploration pills/thinking. The per-edit
            # diff_cards are already durable (edit_record_cb); without this closing
            # message the turn's pills/thinking vanish on reload (smoke-found gap #3).
            summary = outcome.text or ""
            metadata = self._turn_metadata(outcome)
            if summary or metadata:
                self._store.append_message(
                    thread_id, ChatMessage(role="agent", content=summary, metadata=metadata))
                if summary:
                    self._broadcaster.broadcast(
                        channel_id, {"type": "chat_response", "payload": {"chunk": summary}})
            self._broadcaster.broadcast(channel_id, {"type": "chat_done", "payload": {}})
        elif outcome.kind == "propose_mode":
            await self._present_mode_choice(thread_id, channel_id, outcome)

    @staticmethod
    def _turn_metadata(outcome: ControllerOutcome) -> dict[str, object]:
        """Durable pills + thinking for a turn's agent message (reload survival)."""
        metadata: dict[str, object] = {}
        if outcome.tool_events:
            metadata["tool_events"] = outcome.tool_events
        if outcome.thinking_log:
            metadata["thinking_log"] = outcome.thinking_log
        return metadata

    async def _present_mode_choice(
        self, thread_id: str, channel_id: str, outcome: ControllerOutcome,
    ) -> None:
        """Class-A gate: set a durable thread gate (/live renders it via LiveSlot,
        survives reload) and END the message stream. No SSE mode event — chat gates
        render purely from the /live poll (CLAUDE.md). Resolved by /mode-decision (F2)."""
        # Persist the exploration pills + thinking as a durable record (mirrors ChatAgent
        # writing a pills-only message before task cards) so they survive a reload; the
        # gate itself is durable via pending_controller_gate.
        metadata = self._turn_metadata(outcome)
        if metadata:
            self._store.append_message(thread_id, ChatMessage(
                role="agent", content="", metadata=metadata))
        self._store.set_controller_gate(
            thread_id, PendingGate(kind="mode", payload=outcome.payload or {}))
        self._broadcaster.broadcast(channel_id, {"type": "chat_done", "payload": {}})

    async def _edit_decision_cb(
        self, thread_id: str, channel_id: str, diff: list[DiffEntry],
    ) -> dict[str, object]:
        """Hold the SSE stream open while a per-edit review gate is pending.

        Sets the durable `edit` thread gate (/live renders the diff), creates the
        decision future, and awaits it — mirroring _pause_for_step_review. On a
        dropped client (no decision) it auto-rejects after the timeout so the loop
        unwinds cleanly. The gate clears in place in the finally (Class-A)."""
        self._store.set_controller_gate(thread_id, PendingGate(kind="edit", payload={
            "diff_entries": [
                {"path": d.path, "additions": d.additions,
                 "deletions": d.deletions, "unified_diff": d.unified_diff}
                for d in diff]}))
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[dict[str, object]] = loop.create_future()
        self._pending_edit[thread_id] = fut
        timeout = float(os.environ.get(_EDIT_DECISION_TIMEOUT_ENV, "0") or "0")
        try:
            if timeout > 0:
                return await asyncio.wait_for(fut, timeout=timeout)
            return await fut
        except TimeoutError:
            return {"decision": "reject", "reason": "decision timed out"}
        finally:
            self._pending_edit.pop(thread_id, None)
            self._store.set_controller_gate(thread_id, None)

    async def _edit_record_cb(
        self, thread_id: str, channel_id: str, was_review: bool,
        diff: list[DiffEntry], decision: str, reason: str,
    ) -> None:
        """Durably record a resolved edit (the loop's single transcript writer).

        Persists an inert diff_card (renders Applied/Discarded on reload, never
        interactive — mirrors engine._write_chat_step_diff_record). temp_path is
        omitted: the edit is instant-promoted (shadow==real) so a native diff is
        meaningless, and the turn-shadow is rmtree'd at turn end. In review mode the
        live EditGate already showed the diff, so we add a breadcrumb (the card
        materializes on reload); in auto-accept there was no gate, so we render the
        inert card live too."""
        diff_payload = [
            {"path": d.path, "additions": d.additions,
             "deletions": d.deletions, "unified_diff": d.unified_diff}
            for d in diff]
        resolved = "applied" if decision == "accept" else "discarded"
        self._store.append_message(thread_id, ChatMessage(
            role="agent", content="", type="diff_card",
            metadata={"diff_entries": diff_payload, "resolved": resolved}))
        # Render the inert card live in BOTH modes so the accepted/rejected diff stays
        # in the transcript without waiting for a reload. In review mode the live
        # EditGate (pinned /live slot) has already cleared by now, so this fills the
        # hole it leaves; `resolved` is set so the card is inert (no dead buttons).
        self._broadcaster.broadcast(channel_id, {
            "type": "diff_ready",
            "payload": {"diff_entries": diff_payload, "resolved": resolved}})
        files = ", ".join(d.path for d in diff) or "(no files)"
        if was_review:
            if decision == "accept":
                text = f"✓ Edit accepted: {files}"
            else:
                text = f"✗ Edit rejected: {files}"
                if reason:
                    text += f" — {reason}"  # surface the user's reason in the record
            self._write_breadcrumb(thread_id, channel_id, text)

    async def resolve_edit(self, thread_id: str, decision: dict[str, object]) -> bool:
        """Resolve the per-edit gate (POST /edit-decision). Fires the future when a
        live waiter exists (never mutates/persists during the await — Class-A safety).

        Backend-restart orphan: when the EditGate persisted in sqlite but the in-memory
        waiter is gone (`thread_id not in _pending_edit`), clear the stale gate + write a
        breadcrumb so the UI unwedges (turn_active is already False post-restart → input
        re-enables). The user re-issues the edit. Matches the orphaned-task degradation."""
        fut = self._pending_edit.get(thread_id)
        if fut is None or fut.done():
            # No live waiter. If a stale edit gate persists (restart orphan), clear it.
            thread = self._store.get_thread(thread_id)
            gate = thread.pending_controller_gate if thread is not None else None
            if gate is not None and gate.kind == "edit":
                self._store.set_controller_gate(thread_id, None)
                self._write_breadcrumb(
                    thread_id, f"chat:{thread_id}",
                    "Previous turn ended — please re-send your request.")
            return False
        fut.set_result(decision)
        return True

    async def stop_turn(self, thread_id: str) -> bool:
        """Cancel a detached turn (POST /stop) — a slimmer cousin of task /abort.

        Cancels the asyncio.Task; the turn's own finally chain does the cleanup:
        _run_turn pops _active_turns, ControllerLoop.run's finally closes the turn-
        shadow, and a held-open EditGate's _edit_decision_cb finally clears the gate +
        pops _pending_edit. Then broadcast chat_done so the relay closes, and write a
        durable ✗ Stopped breadcrumb. Benign no-op (False) if no active turn."""
        task = self._active_turns.get(thread_id)
        if task is None or task.done():
            return False
        task.cancel()
        try:
            await task  # let the cancellation unwind (finally chain runs)
        except asyncio.CancelledError:
            pass
        channel_id = f"chat:{thread_id}"
        self._write_breadcrumb(thread_id, channel_id, "✗ Stopped")
        self._broadcaster.broadcast(channel_id, {"type": "chat_done", "payload": {}})
        return True

    def _write_breadcrumb(self, thread_id: str, channel_id: str, text: str) -> None:
        """Persist a durable transcript breadcrumb AND broadcast it live (mirror
        engine.write_chat_breadcrumb). The live mode/edit gate is ephemeral; this is
        the permanent record of the user's decision so history reads as a narrative."""
        self._store.append_message(thread_id, ChatMessage(
            role="agent", content=text, type="text", metadata={"breadcrumb": True}))
        self._broadcaster.broadcast(channel_id, {
            "type": "chat_breadcrumb", "payload": {"text": text, "task_id": ""}})

    async def resolve_mode(
        self, thread_id: str, mode: str, *, channel_id: str, goal: str,
    ) -> None:
        """Resolve the mode gate (POST /mode-decision). Clears the gate in place
        (Class-A), writes a breadcrumb, then dispatches: edit/explain re-enter the
        loop (a new streamed turn), create_task/resume hand off to the orchestrator."""
        # Precondition + idempotency guard: only a pending `mode` gate may resolve.
        # The read→clear pair has no `await` between it (sqlite is sync), so two
        # concurrent /mode-decision posts can't both dispatch (which would double-
        # create a task). The second finds the gate already cleared and no-ops.
        thread = self._store.get_thread(thread_id)
        gate = thread.pending_controller_gate if thread is not None else None
        if gate is None or gate.kind != "mode":
            logger.info("[controller] resolve_mode no-op: no pending mode gate (thread=%s)",
                        thread_id)
            return
        # Friendly record of the choice — read the option label from the gate BEFORE
        # clearing it so the breadcrumb reads "▸ You chose: Edit inline now" not a raw mode.
        label = mode
        for opt in (gate.payload.get("options") or []):
            if isinstance(opt, dict) and opt.get("mode") == mode:
                label = str(opt.get("label") or mode)
                break
        self._store.set_controller_gate(thread_id, None)
        # PERSIST + broadcast (mirror engine.write_chat_breadcrumb): a bare broadcast
        # dies on reload, leaving no record of what the user chose.
        self._write_breadcrumb(thread_id, channel_id, f"▸ You chose: {label}")

        if mode in ("edit", "explain"):
            if mode == "edit" and self._orchestrator is None:
                raise RuntimeError("edit mode requires an orchestrator")
            phase = "EDIT" if mode == "edit" else None
            # Honor the "Review each edit" toggle from the message that opened this
            # gate (explain has no edits, so the value is inert there).
            review = self._step_review_by_thread.get(thread_id)
            outcome = await self._run_loop(
                thread_id, channel_id, goal,
                seed_history=self._seed_for(thread_id), step_review=review, phase=phase)
            await self._finish(thread_id, channel_id, outcome, step_review=review)
            return

        if mode == "create_task":
            if self._orchestrator is None:
                raise RuntimeError("create_task mode requires an orchestrator")
            # Thread the "Review each step" toggle through to the task (matches the
            # edit path + the old ChatAgent large_change handoff): True → gate each
            # step, None → env default.
            review = self._step_review_by_thread.get(thread_id)
            # Forward every tool call in the thread's history as the planner's
            # pre_explored_context (parity with ChatAgent large_change) so it doesn't
            # re-explore cold. Derived from the verbatim (uncapped) conversation —
            # restart-durable via _seed_for, one source of truth with seed_history.
            explore_context = _explore_context_from_history(self._seed_for(thread_id))
            # Use the agent's plan_sketch as the task goal, NOT the bare last message.
            # The sketch is an LLM synthesis of the WHOLE conversation (explored +
            # seed_history), so it survives clarify/refine turns where the last message
            # ("keep it minimal") is meaningless standalone. Fall back to the raw goal
            # if the model omitted a sketch.
            sketch = str(gate.payload.get("plan_sketch") or "").strip()
            task_goal = sketch or goal
            task_id = await self._orchestrator.create_task_from_chat(
                thread_id=thread_id, goal=task_goal, workspace_path=self._workspace_path,
                explore_context=explore_context, store=self._store,
                step_review_auto_accept=(not review) if review is not None else None)
            self._store.append_message(thread_id, ChatMessage(
                role="agent", content=task_id, type="task_card", task_id=task_id,
                metadata={"taskId": task_id}))
            self._broadcaster.broadcast(
                channel_id, {"type": "task_card", "payload": {"task_id": task_id}})
            await self._orchestrator.await_plan_ready(task_id)
        else:
            # resume is offered only when a resumable recent task exists; that
            # plumbing isn't wired in v1, so degrade gracefully rather than guess.
            # Persist (not broadcast-only) so the note survives a reload like every
            # other decision record — no live-only crumb (the bug class we're fixing).
            logger.warning("[controller] unhandled mode %r — no dispatch", mode)
            self._write_breadcrumb(
                thread_id, channel_id, f"Mode {mode!r} is not available yet.")
        self._broadcaster.broadcast(channel_id, {"type": "chat_done", "payload": {}})
