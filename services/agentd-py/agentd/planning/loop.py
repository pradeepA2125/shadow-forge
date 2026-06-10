"""Explore-then-commit ReAct loop for the PlanningAgent."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from uuid import uuid4

from agentd.domain.models import (
    AgentToolTrace,
    PlanRevisionResult,
    PlanningResult,
    RevisedStep,
    TaskBudget,
    ToolCall,
    ToolResult,
)
from agentd.orchestrator.broadcaster import PatchEventBroadcaster, cap_event_output
from agentd.planning.registry import PlanningToolRegistry
from agentd.reasoning.contracts import ReasoningEngine

logger = logging.getLogger(__name__)

_MAX_PLANNING_RESULT_CHARS = int(os.environ.get("AI_EDITOR_PLANNING_RESULT_MAX_CHARS", "100000"))


def _assistant_turn(response: dict[str, object]) -> dict[str, object]:
    """Build the assistant history entry for a model turn WITHOUT its 'thought'.

    Persisting the model's verbatim 'thought' lets a weak model copy-continue its own
    prior reasoning, which compounds into a repetition attractor (the same call emitted
    turn after turn). Keep the actionable fields (type/tool/args) so history still
    reflects what action was taken; drop the free-text reasoning. Append-only, so the
    KV-cache prefix is unaffected.
    """
    persisted = {k: v for k, v in response.items() if k != "thought"}
    return {"role": "assistant", "content": json.dumps(persisted, default=str)}


class PlanningBudgetExceededError(Exception):
    """Raised when the planning loop exhausts its tool-call budget."""

    def __init__(self, message: str, partial_trace: "AgentToolTrace | None" = None) -> None:
        super().__init__(message)
        self.partial_trace = partial_trace


def _validate_no_duplicate_file_targets(steps: list[dict[str, object]]) -> list[str]:
    """Check that no file path appears in more than one step's targets."""
    seen: dict[str, str] = {}
    errors: list[str] = []
    for step in steps:
        step_id = str(step.get("id", step.get("step_id", "?")))
        targets = step.get("targets", [])
        if not isinstance(targets, list):
            continue
        for target in targets:
            path = target.get("path", "") if isinstance(target, dict) else str(target)
            if path in seen:
                errors.append(
                    f"File '{path}' appears in both step '{seen[path]}' and step '{step_id}'. "
                    "Consolidate all changes to this file into one step."
                )
            else:
                seen[path] = step_id
    return errors


class PlanningLoop:
    """Implements the explore-then-commit ReAct loop for PlanningAgent.

    Calls reasoning_engine.create_planning_step() each iteration.
    Returns when the agent emits emit_plan or emit_revision.
    Raises PlanningBudgetExceededError if budget exhausted without emitting.
    """

    def __init__(
        self,
        reasoning_engine: ReasoningEngine,
        registry: PlanningToolRegistry,
        broadcaster: PatchEventBroadcaster,
        task_id: str,
        chat_channel_id: str | None = None,
    ) -> None:
        self._reasoning = reasoning_engine
        self._registry = registry
        self._broadcaster = broadcaster
        self._task_id = task_id
        self._chat_channel_id = chat_channel_id

    def _broadcast(self, event: dict) -> None:
        self._broadcaster.broadcast(self._task_id, event)
        if self._chat_channel_id:
            self._broadcaster.broadcast(self._chat_channel_id, event)
            event_type = event.get("type", "?")
            payload = event.get("payload", {})
            if event_type == "planning_tool_call":
                logger.info("[chat→task] planning_tool_call: tool=%s iter=%s → %s",
                            payload.get("tool"), payload.get("iteration"), self._chat_channel_id)
            elif event_type == "planning_complete":
                logger.info("[chat→task] planning_complete: confidence=%s → %s",
                            payload.get("confidence"), self._chat_channel_id)

    async def run(
        self,
        plan_context: dict[str, object],
        budget: TaskBudget,
        *,
        revision_mode: bool = False,
        seed_history: list[dict[str, object]] | None = None,
    ) -> PlanningResult | PlanRevisionResult:
        """Run one planning loop. Returns PlanningResult or PlanRevisionResult.

        seed_history: prior planning turns to replay as the cacheable prefix (a
        feedback round passes the persisted history with the feedback appended).
        """
        tool_defs = [t.model_dump() for t in self._registry.definitions()]
        max_calls = (
            budget.max_revision_tool_calls if revision_mode else budget.max_planning_tool_calls
        )
        emit_type = "emit_revision" if revision_mode else "emit_plan"
        return await self._run_single_pass(
            plan_context=plan_context,
            tool_defs=tool_defs,
            max_calls=max_calls,
            emit_type=emit_type,
            seed_history=seed_history,
        )

    async def _run_single_pass(
        self,
        plan_context: dict[str, object],
        tool_defs: list[dict[str, object]],
        max_calls: int,
        emit_type: str,
        seed_history: list[dict[str, object]] | None = None,
    ) -> PlanningResult | PlanRevisionResult:
        # Thread the real budget into plan_context so the prompt builders report
        # an accurate "N/max" status and only pressure the model on the final call.
        plan_context = {**plan_context, "max_tool_calls": max_calls}
        trace = AgentToolTrace(step_id="planning")
        # Replay any prior planning turns verbatim, then grow by append — the seed is
        # the KV-cache prefix the continuation reuses.
        history: list[dict[str, object]] = [dict(m) for m in seed_history] if seed_history else []
        # key = (tool_name, canonical_args_json) → first iteration it was called
        _seen_calls: dict[str, int] = {}

        _MAX_STEP_RETRIES = 2
        # Weak local models (e.g. qwen3.6 under a large context) intermittently
        # return an empty/typeless JSON object. Treat that as a recoverable turn —
        # inject a correction and retry — rather than killing the whole task. Bail
        # only after this many CONSECUTIVE malformed responses.
        _MAX_MALFORMED = 3
        _consecutive_malformed = 0

        for iteration in range(max_calls + 1):
            def _on_thinking(chunk: str, _iter: int = iteration) -> None:
                self._broadcast({
                    "type": "planning_thinking_chunk",
                    "payload": {"chunk": chunk, "iteration": _iter + 1},
                })

            last_step_exc: Exception | None = None
            response: dict[str, object] = {}
            for _attempt in range(_MAX_STEP_RETRIES + 1):
                try:
                    response = await self._reasoning.create_planning_step(
                        plan_context=plan_context,
                        history=history,
                        tool_definitions=tool_defs,
                        on_thinking=_on_thinking,
                    )
                    last_step_exc = None
                    break
                except Exception as exc:
                    last_step_exc = exc
                    logger.warning(
                        "[plan] create_planning_step failed at iter=%d attempt=%d/%d: %s",
                        iteration, _attempt + 1, _MAX_STEP_RETRIES + 1, exc,
                    )
                    # A parse failure (e.g. the model returned prose with no JSON object)
                    # is input-determined: retrying with the SAME prompt reproduces the
                    # SAME unparseable output. Inject a correction into history so the next
                    # attempt sees a CHANGED prompt and has a real chance to recover.
                    # Append a balanced (assistant + tool_result) pair to match the
                    # malformed-response path and keep history pairing intact.
                    if _attempt < _MAX_STEP_RETRIES:
                        history.append({"role": "assistant", "content": "{}"})
                        history.append({
                            "role": "tool_result",
                            "tool": "",
                            "content": (
                                "Your previous reply had no JSON object. Respond with ONLY a "
                                "single JSON object matching the required schema — no prose, no "
                                "explanation, no markdown fences."
                            ),
                        })
            if last_step_exc is not None:
                raise last_step_exc

            action_type = str(response.get("type", ""))
            thought = str(response.get("thought", ""))

            if action_type == "emit_plan":
                plan_markdown = response.get("plan_markdown")
                if not plan_markdown or not str(plan_markdown).strip():
                    # An empty plan_markdown is usually a truncated response (the model's
                    # reasoning exhausted the token budget), not a fatal error. Correct
                    # and retry like a malformed response; only bail after repeated misses.
                    _consecutive_malformed += 1
                    if _consecutive_malformed > _MAX_MALFORMED:
                        raise PlanningBudgetExceededError(
                            f"emit_plan returned empty 'plan_markdown' {_consecutive_malformed} "
                            f"times (last at iteration {iteration})",
                            partial_trace=trace,
                        )
                    logger.warning(
                        "[plan] iter=%d/%d  emit_plan EMPTY plan_markdown (%d/%d) — injecting correction",
                        iteration + 1, max_calls, _consecutive_malformed, _MAX_MALFORMED,
                    )
                    history.append(_assistant_turn(response))
                    history.append({
                        "role": "tool_result", "tool": "",
                        "content": (
                            "Your emit_plan had an empty 'plan_markdown' — the response was likely "
                            "truncated (reasoning consumed the token budget). Re-emit "
                            "type='emit_plan' with the FULL plan_markdown now, and keep it concise: "
                            "describe each step in prose, avoid long verbatim code blocks."
                        ),
                    })
                    continue
                _consecutive_malformed = 0
                files_examined = list(response.get("files_examined", []))
                confidence = str(response.get("confidence", "medium"))
                if confidence not in ("high", "medium", "low"):
                    confidence = "medium"
                self._broadcast({
                    "type": "planning_complete",
                    "payload": {"files_examined": files_examined, "confidence": confidence},
                })
                # Record the emitted plan as the final history turn so a later feedback
                # round REPLAYS the actual plan being revised (not just the exploration
                # that led to it). _assistant_turn keeps plan_markdown — it strips only
                # the free-text 'thought'.
                history.append(_assistant_turn(response))
                return PlanningResult(
                    plan_markdown=str(plan_markdown),
                    files_examined=files_examined,
                    confidence=confidence,  # type: ignore[arg-type]
                    tool_trace=trace,
                    conversation_history=history,
                )

            if action_type == "emit_revision":
                raw_steps = response.get("revised_steps")
                if not isinstance(raw_steps, list) or len(raw_steps) == 0:
                    raise PlanningBudgetExceededError(
                        f"emit_revision response missing or empty 'revised_steps' at iteration {iteration}",
                        partial_trace=trace,
                    )
                revised_steps = [
                    RevisedStep(
                        step_id=str(s.get("step_id", "")),
                        goal=str(s.get("goal", "")),
                        targets=s.get("targets", []),  # type: ignore[arg-type]
                        implementation_details=str(s.get("implementation_details", "")),
                        edge_cases=str(s.get("edge_cases", "")),
                        testing_strategy=str(s.get("testing_strategy", "")),
                        risk=str(s.get("risk", "low")),
                    )
                    for s in raw_steps
                    if isinstance(s, dict)
                ]
                reverted_step_ids = list(response.get("reverted_step_ids", []))
                revision_summary = str(response.get("revision_summary", ""))
                return PlanRevisionResult(
                    revised_steps=revised_steps,
                    reverted_step_ids=reverted_step_ids,
                    revision_summary=revision_summary,
                    tool_trace=trace,
                )

            if action_type == "emit_plan_patch":
                from agentd.planning.plan_patch import PlanPatchError, apply_plan_patch
                current_plan = str(plan_context.get("current_plan_markdown", ""))
                scratch = plan_context.get("plan_patch_scratch_dir")
                raw_ops = response.get("ops")
                ops = raw_ops if isinstance(raw_ops, list) else []
                # Record the patch attempt in the trace so the artifact reflects it —
                # emit_plan_patch is not a tool_call, so without this it would be invisible.
                patch_call_id = f"plan-{uuid4().hex[:8]}"
                trace.calls.append(ToolCall(
                    call_id=patch_call_id, tool_name="emit_plan_patch", arguments={"ops": ops},
                ))
                try:
                    new_plan = await apply_plan_patch(
                        current_plan, ops, scratch_dir=Path(str(scratch))
                    )
                except PlanPatchError as exc:
                    # Non-fatal: inject a correction and let the model retry or emit_plan.
                    logger.warning(
                        "[plan] iter=%d/%d  PLAN PATCH FAILED: %s", iteration + 1, max_calls, exc
                    )
                    trace.results.append(ToolResult(
                        call_id=patch_call_id, tool_name="emit_plan_patch",
                        output=f"PLAN PATCH FAILED: {exc}", is_error=True,
                    ))
                    history.append(_assistant_turn(response))
                    history.append({
                        "role": "tool_result", "tool": "",
                        "content": (
                            f"PLAN PATCH FAILED: {exc}. Each `search` must be an exact, unique "
                            "snippet copied verbatim from the current plan. Fix the op(s), or "
                            "reply with emit_plan for a full rewrite."
                        ),
                    })
                    continue
                trace.results.append(ToolResult(
                    call_id=patch_call_id, tool_name="emit_plan_patch",
                    output=f"PLAN PATCH APPLIED: {len(ops)} op(s); new plan {len(new_plan)} chars",
                    is_error=False,
                ))
                self._broadcast({
                    "type": "planning_complete",
                    "payload": {"files_examined": [], "confidence": "medium", "patched": True},
                })
                history.append(_assistant_turn(response))
                return PlanningResult(
                    plan_markdown=new_plan,
                    files_examined=[],
                    confidence="medium",
                    tool_trace=trace,
                    conversation_history=history,
                )

            if action_type != "tool_call":
                _consecutive_malformed += 1
                if _consecutive_malformed > _MAX_MALFORMED:
                    raise PlanningBudgetExceededError(
                        f"Planning model returned {_consecutive_malformed} consecutive malformed "
                        f"responses (last type {action_type!r}) at iteration {iteration}; "
                        "expected tool_call, emit_plan, or emit_revision",
                        partial_trace=trace,
                    )
                logger.warning(
                    "[plan] iter=%d/%d  MALFORMED response (type=%r, %d/%d) — injecting correction",
                    iteration + 1, max_calls, action_type, _consecutive_malformed, _MAX_MALFORMED,
                )
                _correction = (
                    "Your previous response was empty or had no valid 'type'. Reply with EXACTLY "
                    "ONE JSON object whose 'type' is one of: 'tool_call' (explore with a tool), "
                    "'emit_plan' (you have enough context — include plan_markdown, files_examined, "
                    "confidence), or 'emit_revision'. Do NOT return an empty object."
                )
                history.append(_assistant_turn(response))
                history.append({"role": "tool_result", "tool": "", "content": _correction})
                continue
            _consecutive_malformed = 0

            if iteration >= max_calls:
                raise PlanningBudgetExceededError(
                    f"Planning loop used {max_calls} tool calls without emitting {emit_type}",
                    partial_trace=trace,
                )

            tool_name = str(response.get("tool", ""))
            raw_args = response.get("args")
            args: dict[str, object] = raw_args if isinstance(raw_args, dict) else {}

            args_repr = json.dumps(args, default=str)[:300]
            logger.info(
                "[plan] iter=%d/%d  tool=%s  args=%s",
                iteration + 1, max_calls, tool_name, args_repr,
            )

            # Duplicate call guard: if exact (tool, args) was seen before, inject correction
            # instead of executing — prevents infinite search_code loops.
            # For search_code, normalize out context_lines so bumping it doesn't bypass the guard.
            _dedup_args = dict(args)
            if tool_name == "search_code":
                _dedup_args.pop("context_lines", None)
            _call_key = f"{tool_name}:{json.dumps(_dedup_args, sort_keys=True, default=str)}"
            if _call_key in _seen_calls:
                _first_iter = _seen_calls[_call_key]
                _dedup_msg = (
                    f"DUPLICATE CALL BLOCKED: you already called `{tool_name}` with these "
                    f"exact arguments at iteration {_first_iter}. Repeating it will return "
                    "the same result. You MUST do something different:\n"
                    "  • If you need to read more of a file, use `read_file` with explicit "
                    "`start_line` and `end_line` from the line numbers you already saw.\n"
                    f"  • If you have enough context, call `{emit_type}` now.\n"
                    "Do NOT call the same tool with the same args again."
                )
                logger.warning(
                    "[plan] iter=%d/%d  DUPLICATE BLOCKED: tool=%s first_seen_at_iter=%d",
                    iteration + 1, max_calls, tool_name, _first_iter,
                )
                # Prong 1: do NOT echo the rejected duplicate call back into history.
                # Re-appending it (with its thought) compounds into a repetition attractor
                # that statistically drowns out this correction — the model just continues
                # its own dominant pattern. A neutral placeholder preserves the
                # assistant/tool_result pairing without reinforcing the repeat. Append-only,
                # so the KV-cache prefix is unaffected.
                history.append({"role": "assistant", "content": "{}"})
                history.append({"role": "tool_result", "tool": tool_name, "content": _dedup_msg})
                continue
            _seen_calls[_call_key] = iteration + 1

            self._broadcast({
                "type": "planning_tool_call",
                "payload": {"tool": tool_name, "thought": thought[:300], "args": args, "iteration": iteration + 1},
            })

            tool_output = await self._registry.execute(tool_name, args)

            out_chars = len(tool_output.output)
            preview = tool_output.output[:200].replace("\n", "↵")
            logger.info(
                "[plan] iter=%d/%d  tool=%s  →  chars=%d  is_error=%s  preview=%r",
                iteration + 1, max_calls, tool_name, out_chars, tool_output.is_error, preview,
            )

            self._broadcast({
                "type": "planning_tool_result",
                "payload": {"tool": tool_name, "output": cap_event_output(tool_output.output), "is_error": tool_output.is_error, "iteration": iteration + 1},
            })

            call_id = f"plan-{uuid4().hex[:8]}"
            trace.calls.append(ToolCall(call_id=call_id, tool_name=tool_name, arguments=args))
            trace.results.append(ToolResult(
                call_id=call_id,
                tool_name=tool_name,
                output=tool_output.output[:_MAX_PLANNING_RESULT_CHARS],
                is_error=tool_output.is_error,
            ))

            history.append(_assistant_turn(response))
            history.append({
                "role": "tool_result",
                "tool": tool_name,
                "content": tool_output.output[:_MAX_PLANNING_RESULT_CHARS],
            })

        raise PlanningBudgetExceededError("Planning loop exited without result", partial_trace=trace)
