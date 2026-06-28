"""ControllerLoop — the agentic chat-turn ReAct loop (mirrors PlanningLoop).

Reads always hit the real workspace (no shadow-read flip). Terminal actions own
their own teardown. E1 implements explore (tool_call) + answer; clarify/propose_mode/
edit/submit_changes are added in E2/E3.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentd.chat.todo_ledger import TodoLedger
from agentd.chat.tool_events import trace_to_tool_events
from agentd.domain.models import AgentToolTrace, ToolCall, ToolResult
from agentd.memory.harness import NO_OP_HARNESS, MemoryHarness
from agentd.reasoning.react_common import MALFORMED_CORRECTION, assistant_turn, dedup_key

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agentd.chat.controller_phase import ControllerPhaseSM
    from agentd.chat.edit_session import TurnEditSession
    from agentd.domain.models import DiffEntry
    from agentd.orchestrator.broadcaster import EventBroadcaster
    from agentd.reasoning.contracts import ReasoningEngine
    from agentd.tools.sources import AggregatingToolRegistry

    EditDecisionCb = Callable[[list[DiffEntry]], Awaitable[dict[str, object]]]
    # Persist+render an edit's resolution (diff, decision: "accept"|"reject", reason).
    # The controller owns the durable diff_card record + transcript broadcast; the
    # loop no longer broadcasts diff_ready itself (Class-A: the record cb is the one
    # writer, so every edit survives a reload — smoke-found gap #2/#5).
    EditRecordCb = Callable[[list[DiffEntry], str, str], Awaitable[None]]
    # Given the files an accepted edit touched, return a compact retrieval-refresh
    # note (pointers only, no bodies) to append to history — or None.
    RetrievalDeltaCb = Callable[[list[str]], Awaitable[str | None]]
    # Persist the in-flight turn's pills incrementally (tool_events, thinking_log) so a
    # thread switch / panel reopen mid-turn reconstructs them durably (finding 5).
    PillsUpdateCb = Callable[[list[dict], list[str]], Awaitable[None]]


logger = logging.getLogger(__name__)


class ControllerLoopExhausted(Exception):
    """Raised when the controller emits too many consecutive malformed responses.

    Mirrors PlanningLoop's PlanningBudgetExceededError malformed cap.
    """


# The modes propose_mode may offer; resolution routes on these exact strings
# (resolve_mode: edit/explain re-enter the loop, create_task/resume hand off).
_VALID_MODES = frozenset({"edit", "create_task", "resume", "explain"})

# Tools that mutate the workspace. They are barred in the DECIDE phase (read-only
# exploration before mode selection) so the model cannot write source files via the
# shell (`cat >`/`tee`/`touch`), bypassing the EditGate. Enforced at the dispatch
# guard only — the advertised tool list (system prompt) is unchanged, keeping the
# cached prefix byte-stable across the DECIDE→EDIT transition.
_STATE_CHANGING_TOOLS = frozenset({"run_command"})

STATE_CHANGING_DECIDE_CORRECTION = (
    "run_command is not available while deciding how to proceed — it can mutate the "
    "workspace, which must go through review. In this phase use only read-only tools "
    "(search_code / read_file / list_directory / read_env_profile). To make changes, "
    "emit propose_mode and let the user pick edit mode; run_command becomes available "
    "once editing has started."
)


def _decide_state_change_correction(resp: dict[str, object], phase: str) -> str | None:
    """Reject a state-changing tool_call in DECIDE; None otherwise (inert for other
    phases and non-tool_call actions)."""
    if phase != "DECIDE" or str(resp.get("type", "")) != "tool_call":
        return None
    if str(resp.get("tool", "")) in _STATE_CHANGING_TOOLS:
        return STATE_CHANGING_DECIDE_CORRECTION
    return None


PROPOSE_MODE_CORRECTION = (
    "Your propose_mode was rejected: each option MUST be an object "
    '{"mode": <m>, "label": <short button text>, "description": <one line>} where '
    "<m> is one of edit | create_task | resume | explain, and the top-level "
    '"recommended" MUST be one of those same values. You used an invalid mode name '
    "or the wrong keys (e.g. \"type\" instead of \"mode\"). Re-emit propose_mode with "
    "valid modes — typically offer BOTH edit (make the change inline now) and "
    "create_task (plan it as a reviewed task), plus explain."
)


def _propose_mode_correction(
    resp: dict[str, object], allowed_modes: frozenset[str] = _VALID_MODES
) -> str | None:
    """Return None if the propose_mode OPTIONS are well-formed AND every offered mode is in
    `allowed_modes`, else a correction.

    Enforces the mode vocabulary the same way the phase SM enforces action types: a weak model
    that invents modes ("create") or wrong keys (options[].type) gets corrected and retried
    rather than surfacing an unusable gate. `allowed_modes` is {edit, explain} when the task
    subsystem is OFF (default) — so a model that offers create_task/resume despite the prompt
    omission gets corrected, not dispatched. `recommended` is a non-blocking hint — it's
    normalized in the emit branch, not required here (weak models reliably emit good options
    but drop the recommended field)."""
    options = resp.get("options")
    if not isinstance(options, list) or not options:
        return PROPOSE_MODE_CORRECTION
    for opt in options:
        if not isinstance(opt, dict) or opt.get("mode") not in allowed_modes:
            return PROPOSE_MODE_CORRECTION
    return None


def _empty_action_correction(resp: dict[str, object], atype: str) -> str | None:
    """Return a correction if a REQUIRED field for `atype` is empty, else None.

    The flat union schema (Gemini-compat, no oneOf) cannot enforce per-type required
    fields at the grammar level, so a weak model can satisfy it with a bare
    {"type":"answer"} (text dumped into the discarded 'thought') or a tool_call with no
    tool/args. Without this guard the loop returns an empty turn or executes "" — the
    empty-answer / empty-tool-call class. Treat these like a malformed action: correct +
    retry (bounded by the same _MAX_MALFORMED cap). submit_changes is NOT here — its empty
    summary is handled with a deterministic fallback in the emit branch (the edits are
    already done; retrying-to-exhaustion would discard real work)."""
    def _blank(key: str) -> bool:
        v = resp.get(key)
        return not (isinstance(v, str) and v.strip())

    if atype == "answer" and _blank("answer"):
        return (
            "Your 'answer' was empty. The COMPLETE response goes in the 'answer' field — "
            "'thought' is discarded. Re-emit type='answer' with a non-empty 'answer', or "
            "type='clarify' if you genuinely cannot answer."
        )
    if atype == "clarify" and _blank("question"):
        return "Your 'question' was empty. Re-emit type='clarify' with a concrete question."
    if atype == "tool_call":
        if _blank("tool"):
            return (
                "Your tool_call had no 'tool'. Re-emit type='tool_call' with a tool name from "
                "AVAILABLE TOOLS and a non-empty 'args' object."
            )
        args = resp.get("args")
        if not isinstance(args, dict) or not args:
            return (
                f"Your tool_call for '{resp.get('tool')}' had empty 'args'. Re-emit with that "
                'tool\'s arguments (e.g. {"path": ...} for read_file, '
                '{"pattern": ...} for search_code).'
            )
    return None


def _normalized_recommended(resp: dict[str, object]) -> str:
    """The model's recommended mode if valid, else the first option's mode (a hint,
    never blocks the gate — see _propose_mode_correction)."""
    rec = resp.get("recommended")
    if rec in _VALID_MODES:
        return str(rec)
    options = resp.get("options")
    if isinstance(options, list) and options and isinstance(options[0], dict):
        return str(options[0].get("mode", ""))
    return ""


@dataclass
class ControllerOutcome:
    kind: str  # "answer" | "clarify" | "propose_mode" | "submit_changes"
    text: str = ""
    payload: dict[str, object] | None = None
    history: list[dict[str, object]] | None = None
    # Durable tool pills (ToolEventView shape) for the turn — persisted onto the
    # agent message so they survive a reload (live SSE pills die). None until the
    # loop finalizes it in run().
    tool_events: list[dict[str, object]] | None = None
    # Durable thinking entries (tool labels) for the turn — persisted alongside the
    # pills so the ThinkingBlock reconstructs on reload (mirrors agent.py/ToolLoop).
    thinking_log: list[str] | None = None


class ControllerLoop:
    def __init__(
        self,
        reasoning: ReasoningEngine,
        registry: AggregatingToolRegistry,
        broadcaster: EventBroadcaster,
        *,
        channel_id: str,
        phase_sm: ControllerPhaseSM,
        edit_session: TurnEditSession | None = None,
        todo_ledger: TodoLedger | None = None,
        task_subsystem_enabled: bool = False,
        memory_harness: MemoryHarness = NO_OP_HARNESS,
    ) -> None:
        self._reasoning = reasoning
        self._registry = registry
        self._broadcaster = broadcaster
        self._channel_id = channel_id
        self._sm = phase_sm
        self._edit = edit_session
        self._ledger = todo_ledger or TodoLedger()
        self._memory_harness = memory_harness
        # OFF (default): only edit/explain may be offered — the controller handles changes
        # inline; a model that proposes create_task/resume anyway gets corrected.
        self._allowed_modes = (
            _VALID_MODES if task_subsystem_enabled else frozenset({"edit", "explain"}))
        self._calls: list[ToolCall] = []
        self._results: list[ToolResult] = []
        self._thinking: list[str] = []
        # The live conversation list `_iterate` mutates — exposed via partial_history() so a
        # caller can persist what a CANCELLED turn (/stop) accumulated before the cancel
        # raised, instead of losing the turn's exploration + already-promoted edits (Q2).
        self._history: list[dict[str, object]] = []
        # Whether any edit has been applied this turn — half of the `edit_entry` signal (the
        # other half is "no todo list yet"). While both hold, the payload builder shows the
        # clean EDIT-ENTRY hint (write_todos-as-tool_call) instead of the mid-turn reconcile
        # hint, so the first-action-after-inline case isn't mis-routed.
        self._edit_applied = False

    def partial_history(self) -> list[dict[str, object]]:
        """The verbatim conversation accumulated so far this turn. Meaningful after a
        cancel: run()'s normal return persists history itself, but a CancelledError unwinds
        before that — the caller reads this to persist the partial."""
        return self._history

    async def run(
        self,
        plan_context: dict[str, object],
        *,
        max_iters: int = 32,
        seed_history: list[dict[str, object]] | None = None,
        auto_accept_edits: bool = False,
        edit_decision_cb: EditDecisionCb | None = None,
        edit_record_cb: EditRecordCb | None = None,
        retrieval_delta_cb: RetrievalDeltaCb | None = None,
        on_pills_update: PillsUpdateCb | None = None,
    ) -> ControllerOutcome:
        tool_defs = [d.model_dump() for d in self._registry.definitions()]
        history = [dict(m) for m in seed_history] if seed_history else []
        # Expose the live list NOW (before _iterate can raise) so a cancel mid-turn still
        # leaves the caller a readable partial (Q2). _iterate mutates this same object.
        self._history = history
        seen: dict[str, int] = {}
        # Bail only after this many CONSECUTIVE malformed responses (mirror PlanningLoop).
        _MAX_MALFORMED = 3
        consecutive_malformed = 0
        plan_context = {**plan_context, "max_iters": max_iters}
        # Tool trace + thinking accumulated across the turn → persisted as durable
        # pills + thinking entries (reload). Live SSE copies die on reload.
        self._calls = []
        self._results = []
        self._thinking = []
        self._edit_applied = False
        try:
            outcome = await self._iterate(
                plan_context, history, tool_defs, seen, max_iters,
                _MAX_MALFORMED, consecutive_malformed,
                auto_accept_edits=auto_accept_edits,
                edit_decision_cb=edit_decision_cb,
                edit_record_cb=edit_record_cb,
                retrieval_delta_cb=retrieval_delta_cb,
                on_pills_update=on_pills_update,
            )
            if outcome.tool_events is None and self._calls:
                outcome.tool_events = trace_to_tool_events(
                    AgentToolTrace(step_id="chat", calls=self._calls, results=self._results),
                    "execution")
            if outcome.thinking_log is None and self._thinking:
                outcome.thinking_log = list(self._thinking)
            return outcome
        finally:
            # The per-turn shadow is discarded at turn end on ANY exit (submit, budget
            # exhaustion, exhaustion-raise, or a patch crash) — no shadow leak.
            if self._edit is not None:
                await self._edit.close()

    async def _iterate(
        self,
        plan_context: dict[str, object],
        history: list[dict[str, object]],
        tool_defs: list[dict[str, object]],
        seen: dict[str, int],
        max_iters: int,
        _MAX_MALFORMED: int,
        consecutive_malformed: int,
        *,
        auto_accept_edits: bool,
        edit_decision_cb: EditDecisionCb | None,
        edit_record_cb: EditRecordCb | None,
        retrieval_delta_cb: RetrievalDeltaCb | None,
        on_pills_update: PillsUpdateCb | None = None,
    ) -> ControllerOutcome:
        def _on_thinking(chunk: str) -> None:
            # Stream the model's reasoning live so the chat thinking pane updates
            # during a model call (the FE maps tool_thinking_chunk). Raw token
            # chunks are live-only; durable thinking_log gets compact tool labels.
            self._broadcaster.broadcast(self._channel_id, {
                "type": "tool_thinking_chunk", "payload": {"chunk": chunk}})

        for iteration in range(max_iters + 1):
            # Live "thinking" status so the chat UI isn't blank during the first model
            # call (the frontend maps chat_agent_thinking → the thinking pane). Only the
            # first iteration: subsequent activity is conveyed by tool pills + the live
            # work-bar timer, so re-emitting each turn would just spam duplicate entries.
            if iteration == 0:
                self._broadcaster.broadcast(self._channel_id, {
                    "type": "chat_agent_thinking", "payload": {"message": "Thinking…"}})
            # Memory middleware: compact the live history in place before the model call
            # (no-op unless AI_EDITOR_MEMORY_ENABLED). history[:] keeps the same list object
            # partial_history() and downstream .append() calls reference.
            run_id = str(plan_context.get("run_id", "chat"))
            _prep = await self._memory_harness.prepare_turn(history, run_id)
            history[:] = _prep.history
            # Recalled long-term memories → the payload tail (KV-safe). Empty list omits it.
            plan_context["recalled_memories"] = _prep.recalled_memories
            if _prep.compacted:
                # Observability: surface the compaction so the chat UI can show it fired.
                self._broadcaster.broadcast(self._channel_id, {
                    "type": "memory_compacted",
                    "payload": {
                        "evicted": _prep.evicted_count,
                        "anchor_version": _prep.anchor_version,
                    },
                })
            # Re-surface the live todo ledger into the payload tail every iteration so the
            # model re-reads its own contract (the detail that makes discretion stick). Empty
            # string when no list exists -> build_controller_step_payload omits it.
            plan_context["todo_status"] = self._ledger.render()
            # EDIT-entry signal: first action after inline-edit was chosen, nothing started yet
            # (no list, no edit applied). The payload builder swaps the clean entry hint
            # (write_todos-as-tool_call) for the mid-turn reconcile hint once this clears —
            # which it does the moment a list exists OR an edit lands, so the entry hint persists
            # through an empty-edit fumble (keeps steering the right first move).
            plan_context["edit_entry"] = (
                self._sm.phase == "EDIT" and not self._ledger.items
                and not self._edit_applied and not plan_context.get("edit_is_resume"))
            resp = await self._reasoning.create_controller_step(
                plan_context=plan_context, history=history,
                tool_definitions=tool_defs, phase=self._sm.phase,
                on_thinking=_on_thinking,
            )
            atype = str(resp.get("type", ""))
            logger.info("[controller] iter=%d phase=%s action=%s", iteration, self._sm.phase, atype)
            # Reject BEFORE dispatching: wrong action type for the phase, a propose_mode with
            # invalid mode vocabulary, OR a well-typed action with an empty REQUIRED field (the
            # flat schema permits {"type":"answer"} / empty tool_call — see
            # _empty_action_correction). Each is corrected + retried, bounded by _MAX_MALFORMED.
            correction = (
                MALFORMED_CORRECTION
                if atype not in self._sm.allowed_types()
                else _propose_mode_correction(resp, self._allowed_modes) if atype == "propose_mode"
                else _decide_state_change_correction(resp, self._sm.phase)
                or _empty_action_correction(resp, atype)
            )
            if correction is not None:
                if atype == "propose_mode":
                    logger.warning(
                        "[controller] propose_mode REJECTED: recommended=%r options=%r",
                        resp.get("recommended"), resp.get("options"))
                else:
                    logger.info(
                        "[controller] %s REJECTED (empty/invalid required field) — correcting",
                        atype)
                consecutive_malformed += 1
                if consecutive_malformed > _MAX_MALFORMED:
                    raise ControllerLoopExhausted(
                        f"Controller returned {consecutive_malformed} consecutive malformed "
                        f"responses (last type={atype!r})")
                history.append(assistant_turn(resp))
                history.append({"role": "tool_result", "tool": "", "content": correction})
                continue
            consecutive_malformed = 0
            if atype == "answer":
                history.append(assistant_turn(resp))
                return ControllerOutcome(
                    kind="answer", text=str(resp.get("answer", "")), history=history)
            if atype == "tool_call":
                if iteration >= max_iters:
                    return ControllerOutcome(
                        kind="answer", text="(step budget exhausted)", history=history)
                tool = str(resp.get("tool", ""))
                raw_args = resp.get("args")
                args: dict[str, object] = raw_args if isinstance(raw_args, dict) else {}
                key = dedup_key(tool, args)
                if key in seen:
                    history.append({"role": "assistant", "content": "{}"})
                    history.append({
                        "role": "tool_result", "tool": tool,
                        "content": f"DUPLICATE BLOCKED (iter {seen[key]}); do differently.",
                    })
                    continue
                seen[key] = iteration + 1
                # Observability: log every tool call/result (ToolLoop does the same).
                # Without this, controller turns are invisible in logs — you can't tell
                # whether a turn explored or emitted straight from seed_history.
                logger.info("[controller] tool_call phase=%s iter=%d tool=%s args=%s",
                            self._sm.phase, iteration, tool, str(args)[:200])
                # Live tool pill: tool_call before execute, tool_result after. The
                # frontend pairs these by source ("execution") into a pill with thought.
                # call_index = the position this call will occupy in the trace (it's
                # appended below), which equals the persisted pill id (trace_to_tool_events
                # uses enumerate index). The FE uses it as the pill id so a switch-back
                # resume dedups replayed pills against the loaded in-flight message.
                call_index = len(self._calls)
                self._broadcaster.broadcast(self._channel_id, {
                    "type": "tool_call",
                    "payload": {"tool": tool, "thought": str(resp.get("thought", "")),
                                "args": args, "call_index": call_index}})
                out = await self._registry.execute(tool, args)
                # The model reconciled the ledger — clear the post-edit checkpoint marker so
                # the next turn's instruction stops naming the (now-answered) edit/item (Q1).
                if tool == "write_todos":
                    plan_context.pop("pending_reconcile_files", None)
                    plan_context.pop("reconcile_item", None)
                logger.info("[controller] tool_result tool=%s is_error=%s chars=%d",
                            tool, out.is_error, len(out.output or ""))
                self._broadcaster.broadcast(self._channel_id, {
                    "type": "tool_result",
                    "payload": {"output": out.output, "is_error": out.is_error,
                                "call_index": call_index}})
                # Record into the turn trace → persisted as durable pills (reload).
                call_id = f"c{iteration}"
                self._calls.append(ToolCall(
                    call_id=call_id, tool_name=tool, arguments=args,
                    thought=str(resp.get("thought", "")) or None))
                self._results.append(ToolResult(
                    call_id=call_id, tool_name=tool, output=out.output, is_error=out.is_error))
                # Durable thinking entry (compact label, not the raw token stream).
                thought = str(resp.get("thought", ""))
                path = str(args.get("path", "")) if isinstance(args, dict) else ""
                label = f" {path.split('/')[-1]}" if path else ""
                self._thinking.append(
                    f"{tool}{label} — {thought[:200]}" if thought else f"{tool}{label}")
                # Durably persist the partial pills now (finding 5): a thread switch /
                # reopen before turn end reconstructs them from the transcript instead of
                # the lossy replay buffer. Best-effort — a persist failure never breaks
                # the turn.
                if on_pills_update is not None:
                    try:
                        pills = trace_to_tool_events(
                            AgentToolTrace(
                                step_id="chat", calls=self._calls, results=self._results),
                            "execution")
                        await on_pills_update(pills, list(self._thinking))
                    except Exception:
                        logger.debug("[controller] inflight pill persist failed", exc_info=True)
                history.append(assistant_turn(resp))
                history.append({"role": "tool_result", "tool": tool, "content": out.output})
                continue
            if atype == "clarify":
                history.append(assistant_turn(resp))
                raw_opts = resp.get("options")
                options = [str(o) for o in raw_opts] if isinstance(raw_opts, list) else []
                question = str(resp.get("question", ""))
                return ControllerOutcome(
                    kind="clarify", text=question, history=history,
                    payload={"question": question, "options": options})
            if atype == "propose_mode":
                history.append(assistant_turn(resp))
                return ControllerOutcome(kind="propose_mode", payload={
                    "plan_sketch": resp.get("plan_sketch", ""),
                    "recommended": _normalized_recommended(resp),
                    "reason": resp.get("reason", ""),
                    "options": resp.get("options", []),
                }, history=history)
            if atype == "edit":
                # EDIT phase is only reachable with an edit_session (phase SM gate).
                assert self._edit is not None
                raw_ops = resp.get("patch_ops")
                ops: list[dict[str, object]] = raw_ops if isinstance(raw_ops, list) else []
                logger.info("[controller] edit phase=%s ops=%d files=%s",
                            self._sm.phase, len(ops),
                            [op.get("file") for op in ops if isinstance(op, dict)])
                if not ops:
                    # Empty 'edit' = the live write_todos mis-route: the model's thought wants the
                    # todo list ("I'll use write_todos first") but it picked type='edit' (the only
                    # "do something" action it associates with EDIT) and shipped no ops. The old
                    # apply([]) error ("emit at least one op or submit_changes") pushed it to retry
                    # the SAME empty edit — observed thrashing 4+ turns. Redirect to the RIGHT action
                    # type with exact syntax. A redirect, NOT a malformed action (don't touch
                    # consecutive_malformed) — only max_iters bounds it.
                    logger.info("[controller] empty edit (ops=0) — redirecting (write_todos/edit/submit)")
                    history.append(assistant_turn(
                        {k: v for k, v in resp.items() if k != "patch_ops"}))
                    history.append({
                        "role": "tool_result", "tool": "edit",
                        "content": (
                            "That 'edit' had no patch_ops, so NOTHING was applied. If you meant to "
                            "create or update the TODO LIST, that is a TOOL CALL — emit "
                            '{"type":"tool_call","tool":"write_todos","args":{"items":[…]}}, NOT '
                            "type='edit'. To change a file, emit type='edit' with a NON-EMPTY "
                            "patch_ops (each op: file + its op fields). To finish, emit "
                            "type='submit_changes'.")})
                    continue
                try:
                    diff = await self._edit.apply(ops)
                except Exception as exc:
                    # A malformed op (code-in-'file'), bad search string, policy violation, or
                    # ambiguous selector raises. Feed it back so the agent re-emits instead of
                    # crashing the turn — mirrors ToolLoop._apply_patch_inline. CRUCIAL: do NOT
                    # echo the malformed patch_ops into history; a weak model copies its own bad
                    # op into a repetition attractor (see planning/loop.py thought-strip). Persist
                    # only the failed *intent*, and let the exception message carry the fix.
                    #
                    # Observability: a failed edit produces NO diff card (edit_record_cb only
                    # fires on success), so without this it is invisible — the UI shows a silent
                    # wait while the model thrashes. Log it AND surface a live + durable thinking
                    # line ("✗ edit failed: <reason>") so the failure is legible in agentd.log
                    # and the chat thinking pane.
                    reason_line = str(exc).splitlines()[0][:200] if str(exc) else "unknown error"
                    logger.info("[controller] edit FAILED phase=%s ops=%d: %s",
                                self._sm.phase, len(ops), reason_line)
                    self._thinking.append(f"✗ edit failed: {reason_line}")
                    self._broadcaster.broadcast(self._channel_id, {
                        "type": "chat_agent_thinking",
                        "payload": {"message": f"✗ edit failed: {reason_line}"}})
                    intent = {k: v for k, v in resp.items() if k != "patch_ops"}
                    history.append(assistant_turn(intent))
                    history.append({
                        "role": "tool_result", "tool": "edit",
                        "content": f"PATCH FAILED: {exc} Re-emit ONE corrected edit op — "
                                   "'file' is a workspace-relative path, code goes in 'content'."})
                    continue
                # Auto-accept (instant promote) OR hold for a per-edit review decision.
                # The decision cb holds the SSE stream open + renders the live diff via
                # the /live EditGate (review mode only). The loop does NOT broadcast
                # diff_ready — edit_record_cb is the single transcript writer (durable
                # diff_card + the auto-accept live render), so nothing dangles on reload.
                if auto_accept_edits or edit_decision_cb is None:
                    await self._edit.accept()
                    accepted = True
                    reason = ""
                else:
                    decision = await edit_decision_cb(diff)
                    accepted = decision.get("decision") == "accept"
                    reason = str(decision.get("reason", ""))
                    if accepted:
                        await self._edit.accept()
                    else:
                        await self._edit.reject()  # restore shadow from real (shadow==real)
                if edit_record_cb is not None:
                    await edit_record_cb(diff, "accept" if accepted else "reject", reason)
                history.append(assistant_turn(resp))
                if accepted:
                    touched = [d.path for d in diff]
                    # A real edit landed → out of the EDIT-entry window (clears edit_entry).
                    self._edit_applied = True
                    # Log the apply outcome (the diff card carries the UI; agentd.log had no
                    # record of a successful apply — only the pre-apply ops line at L302).
                    logger.info("[controller] edit applied phase=%s files=%s",
                                self._sm.phase, touched)
                    # Q1 reconcile checkpoint: when a todo list is ACTIVE (something still
                    # open), flag the just-edited files + the active item so the NEXT turn's
                    # instruction leads with a pointed "is THIS item done?" question. This is
                    # NOT a hard gate — an edit may only PARTIALLY complete an item, so forcing
                    # a write_todos would pressure a false 'done'. The model answers the
                    # checkpoint by marking done OR continuing the same item; the marker clears
                    # the moment write_todos runs (see the tool_call branch). Soft enforcement:
                    # if this still slips on a weak model, the next escalation is a gate that
                    # forces a write_todos call but accepts 'in_progress' as a valid answer.
                    if self._ledger.pending():
                        plan_context["pending_reconcile_files"] = touched
                        active = self._ledger.active_item()
                        if active is not None:
                            plan_context["reconcile_item"] = {
                                "title": active.title, "status": active.status}
                    history.append({
                        "role": "tool_result", "tool": "edit",
                        "content": f"applied+promoted: {touched}"})
                    # Append-only retrieval delta (spec §6): never rewrites the seed,
                    # only adds a compact refresh note to the cached tail.
                    if retrieval_delta_cb is not None:
                        delta = await retrieval_delta_cb(touched)
                        if delta:
                            history.append({
                                "role": "tool_result", "tool": "retrieval_refresh",
                                "content": delta})
                else:
                    history.append({
                        "role": "tool_result", "tool": "edit",
                        "content": f"REJECTED by user: {reason}. Revise and re-emit."})
                continue
            if atype == "submit_changes":
                # Hard gate: a non-empty ledger is a contract. Block submit while items are
                # pending/in_progress (NOT blocked/cancelled/done — those never deadlock) and
                # redirect to the next item. This is a legitimate redirect, NOT a malformed
                # action, so it does NOT touch consecutive_malformed — only max_iters bounds it.
                still_open = self._ledger.pending()
                if still_open:
                    titles = ", ".join(i.title for i in still_open)
                    history.append(assistant_turn(resp))
                    history.append({
                        "role": "tool_result", "tool": "",
                        "content": (
                            f"submit_changes BLOCKED — {len(still_open)} todo item(s) still "
                            f"open: {titles}. Continue with the next item (one edit at a time), "
                            "then call write_todos to mark it 'done' (cite evidence in 'note'). "
                            "If one is genuinely stuck, mark it 'blocked' (with the unblock "
                            "reason) or 'cancelled' (with why). Do NOT submit until nothing is "
                            "pending."),
                    })
                    continue
                # The shadow is closed by run()'s finally on return (no double-close).
                history.append(assistant_turn(resp))
                # Deterministic fallback for an empty summary — unlike answer/clarify we do NOT
                # retry (the edits are already promoted; retrying-to-exhaustion would convert a
                # done turn into a failure). A non-empty summary keeps the closing chat message
                # from collapsing to nothing (the "no closing message" gap).
                summary = str(resp.get("summary", "")).strip() or "Changes submitted."
                return ControllerOutcome(
                    kind="submit_changes", text=summary, history=history)
            raise NotImplementedError(atype)
        return ControllerOutcome(kind="answer", text="(loop ended)", history=history)
