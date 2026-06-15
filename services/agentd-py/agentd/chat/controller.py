"""ChatController — dynamic agentic chat handler (flag-selected vs ChatAgent).

Mirrors ChatAgent's public surface (handle_message + _store/_broadcaster attrs
the route reads) but runs ONE ControllerLoop per turn instead of the
explore→classify→route pipeline. F1 implements QA + clarify; propose_mode gate
(F2) and the per-edit review gate (F3) build on this.
"""
from __future__ import annotations

import asyncio
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

    def _build_registry(self) -> AggregatingToolRegistry:
        return AggregatingToolRegistry([BuiltinToolSource(
            shadow_root=Path(self._workspace_path),
            real_workspace_path=Path(self._workspace_path),
            semantic_index=getattr(self._retrieval, "_semantic_index", None),
        )])

    def _retrieval_seed(self, thread_id: str, goal: str) -> dict[str, object] | None:
        """Compute the thread's retrieval seed once, then reuse it byte-for-byte so
        the cached payload prefix stays stable across turns (spec §6)."""
        if thread_id in self._seeds:
            return self._seeds[thread_id]
        seed: dict[str, object] | None = None
        if self._retrieval is not None:
            try:
                context, _ = self._retrieval.load_context(self._workspace_path, goal)
                seed = context.as_prompt_payload()
            except Exception:
                logger.debug("[controller] retrieval seed failed", exc_info=True)
        self._seeds[thread_id] = seed
        return seed

    async def handle_message(
        self, thread_id: str, message: str, channel_id: str, step_review: bool | None = None,
    ) -> None:
        thread = self._store.get_thread(thread_id)
        if thread is None:
            raise ValueError(f"Thread {thread_id!r} not found")
        # Auto-name the thread from its first user message (mirrors ChatAgent).
        if not any(m.role == "user" for m in thread.messages):
            title = message.strip().replace("\n", " ")[:50]
            self._store.update_title(thread_id, title)
            self._broadcaster.broadcast(channel_id, {
                "type": "thread_title_updated",
                "payload": {"thread_id": thread_id, "title": title},
            })
        self._store.append_message(thread_id, ChatMessage(role="user", content=message))

        seed = self._histories.get(thread_id, [])
        # On a continued turn (clarify/discuss), append the user's reply to the
        # prior history and replay it as the cache prefix (spec §12 clarify resume).
        seed_history = (seed + [{"role": "user", "content": message}]) if seed else None
        outcome = await self._run_loop(
            thread_id, channel_id, message, seed_history=seed_history, step_review=step_review)
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
        edit_cb = partial(self._edit_decision_cb, thread_id, channel_id) \
            if step_review is True else None
        outcome = await loop.run(
            plan_context, seed_history=seed_history,
            auto_accept_edits=(step_review is not True), edit_decision_cb=edit_cb,
            retrieval_delta_cb=self._retrieval_delta_cb)
        self._histories[thread_id] = outcome.history or []
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
            self._store.append_message(
                thread_id, ChatMessage(role="agent", content=outcome.text))
            self._broadcaster.broadcast(
                channel_id, {"type": "chat_response", "payload": {"chunk": outcome.text}})
            self._broadcaster.broadcast(channel_id, {"type": "chat_done", "payload": {}})
        elif outcome.kind == "submit_changes":
            self._broadcaster.broadcast(channel_id, {"type": "chat_done", "payload": {}})
        elif outcome.kind == "propose_mode":
            await self._present_mode_choice(thread_id, channel_id, outcome)

    async def _present_mode_choice(
        self, thread_id: str, channel_id: str, outcome: ControllerOutcome,
    ) -> None:
        """Class-A gate: set a durable thread gate (/live renders it via LiveSlot,
        survives reload) and END the message stream. No SSE mode event — chat gates
        render purely from the /live poll (CLAUDE.md). Resolved by /mode-decision (F2)."""
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

    async def resolve_edit(self, thread_id: str, decision: dict[str, object]) -> bool:
        """Resolve the per-edit gate (POST /edit-decision). Only fires the future —
        never mutates/persists state during the await (Class-A safety)."""
        fut = self._pending_edit.get(thread_id)
        if fut is None or fut.done():
            return False
        fut.set_result(decision)
        return True

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
        self._store.set_controller_gate(thread_id, None)
        self._broadcaster.broadcast(channel_id, {
            "type": "chat_breadcrumb",
            "payload": {"text": f"▸ Proceeding: {mode}", "task_id": ""}})

        if mode in ("edit", "explain"):
            if mode == "edit" and self._orchestrator is None:
                raise RuntimeError("edit mode requires an orchestrator")
            phase = "EDIT" if mode == "edit" else None
            outcome = await self._run_loop(
                thread_id, channel_id, goal,
                seed_history=self._histories.get(thread_id), step_review=False, phase=phase)
            await self._finish(thread_id, channel_id, outcome, step_review=False)
            return

        if mode == "create_task":
            if self._orchestrator is None:
                raise RuntimeError("create_task mode requires an orchestrator")
            task_id = await self._orchestrator.create_task_from_chat(
                thread_id=thread_id, goal=goal, workspace_path=self._workspace_path,
                explore_context=[], store=self._store)
            self._store.append_message(thread_id, ChatMessage(
                role="agent", content=task_id, type="task_card", task_id=task_id,
                metadata={"taskId": task_id}))
            self._broadcaster.broadcast(
                channel_id, {"type": "task_card", "payload": {"task_id": task_id}})
            await self._orchestrator.await_plan_ready(task_id)
        else:
            # resume is offered only when a resumable recent task exists; that
            # plumbing isn't wired in v1, so degrade gracefully rather than guess.
            logger.warning("[controller] unhandled mode %r — no dispatch", mode)
            self._broadcaster.broadcast(channel_id, {
                "type": "chat_breadcrumb",
                "payload": {"text": f"Mode {mode!r} is not available yet.", "task_id": ""}})
        self._broadcaster.broadcast(channel_id, {"type": "chat_done", "payload": {}})
