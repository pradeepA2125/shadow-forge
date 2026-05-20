"""ReAct tool-use loop — two-phase explore+verify execution per plan step."""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable
from uuid import uuid4

from agentd.domain.models import (
    AgentToolTrace,
    PlanStep,
    PlanTarget,
    PlanTargetIntent,
    TaskBudget,
    TaskUsage,
    ToolCall,
    ToolResult,
)
from agentd.orchestrator.broadcaster import PatchEventBroadcaster
from agentd.reasoning.contracts import ReasoningEngine
from agentd.tools.post_patch import AnalyzerBuilder
from agentd.tools.registry import ToolRegistry
from agentd.tools.verify_phase_sm import (
    MAX_PATCH_RETRIES,
    VerifyPhaseEvent,
    VerifyPhaseExhausted,
    VerifyPhaseState,
    VerifyPhaseStateMachine,
)

logger = logging.getLogger(__name__)

_POST_PATCH_ANALYZER = AnalyzerBuilder.default()

_MAX_OUTPUT_INJECT_CHARS = int(os.environ.get("AI_EDITOR_TOOL_RESULT_MAX_CHARS", "100000"))


@dataclass
class VerifyResult:
    patch_document: dict[str, object]   # last applied patch (for artifact writing)
    touched_files: list[str]            # all files modified across all emit_patch calls
    verified: bool
    test_output: str                    # empty when no test_command
    tool_trace: AgentToolTrace


@dataclass
class PlanHandoff:
    step_id: str
    reason: str
    evidence: str
    hinted_affected_steps: list[str]
    tool_trace: AgentToolTrace


StepOutcome = VerifyResult | PlanHandoff


@dataclass
class ScopeDecision:
    """Result of asking whether to extend a step's scope to cover an out-of-scope file."""
    approve: bool
    extended_files: list[str] = field(default_factory=list)
    reason: str = ""
    remember: bool = False


# Callback signature: receives the out-of-scope files + the agent's thought, returns a decision.
ScopeExtensionCallback = Callable[[list[str], str], Awaitable[ScopeDecision]]


async def _default_reject_callback(
    files: list[str], reason: str,
) -> ScopeDecision:
    """Default behavior when no callback is supplied — preserves the pre-feature reject path."""
    _ = files, reason  # acknowledge unused
    return ScopeDecision(approve=False, extended_files=[], reason="default policy")


_SCOPE_FILE_PATTERN = re.compile(r"outside current step scope:\s*([^\s,;]+)")


def _extract_out_of_scope_files(error_msg: str) -> list[str]:
    """Parse 'Patch op targets file outside current step scope: <path>' into a list."""
    return _SCOPE_FILE_PATTERN.findall(error_msg)


class ToolBudgetExceededError(Exception):
    """Raised when the explore budget exhausts before emitting a patch."""


def _build_patch_key(patch_ops: list[object]) -> tuple:
    """Stable hashable key for a list of patch ops — used for emit_patch dedup."""
    return tuple(
        json.dumps(op, sort_keys=True, default=str)
        for op in patch_ops
        if isinstance(op, dict)
    )


class ToolLoop:
    """Two-phase ReAct loop for a single plan step.

    Phase 1 (explore): agent calls tools and emits a patch. Patch is applied inline.
    Phase 2 (verify): agent runs linters/tests and emits verify_done, or corrects with
    another patch.
    """

    def __init__(
        self,
        reasoning_engine: ReasoningEngine,
        registry: ToolRegistry,
        broadcaster: PatchEventBroadcaster,
        task_id: str,
        patch_engine: object | None = None,   # PatchEngine — optional for backward compat in tests
        shadow_path: Path | None = None,
        scope_extension_callback: ScopeExtensionCallback | None = None,
        broadcast_key: str | None = None,
        skip_verify: bool = False,
        thinking_log: list[str] | None = None,
        static_baseline: frozenset[str] | None = None,
    ) -> None:
        self._reasoning = reasoning_engine
        self._registry = registry
        self._broadcaster = broadcaster
        self._task_id = task_id
        self._broadcast_key = broadcast_key if broadcast_key is not None else task_id
        self._skip_verify = skip_verify
        self._patch_engine = patch_engine
        self._shadow_path = shadow_path
        self._thinking_log = thinking_log
        self._static_baseline = static_baseline
        self._scope_cb: ScopeExtensionCallback = (
            scope_extension_callback or _default_reject_callback
        )

    async def run(
        self,
        step: PlanStep,
        patch_request_context: dict[str, object],
        budget: TaskBudget,
        usage: TaskUsage,
        initial_history: list[dict[str, object]] | None = None,
    ) -> StepOutcome:
        trace = AgentToolTrace(step_id=step.id)
        history: list[dict[str, object]] = list(initial_history) if initial_history else []
        sm = VerifyPhaseStateMachine()
        explore_calls = 0
        verify_calls = 0
        last_patch_document: dict[str, object] = {}
        all_touched_files: list[str] = []
        had_scope_violation: bool = False     # True if any patch was rejected for out-of-scope file
        _last_auto_checks_error: str = ""     # first ~300 chars of postpatch output when blocking
        _last_test_failure: str = ""          # first ~300 chars of last failing run_command output

        retrieval_ctx = patch_request_context.get("retrieval_context") or {}
        if not isinstance(retrieval_ctx, dict):
            retrieval_ctx = {}

        step_context: dict[str, object] = {
            "goal": step.goal,
            "targets": [{"path": t.path, "intent": t.intent} for t in step.targets],
            "risk": step.risk,
            "implementation_details": step.implementation_details,
            "edge_cases": step.edge_cases,
            "design_rationale": step.design_rationale,
            "testing_strategy": step.testing_strategy,
            "allowed_files": patch_request_context.get("allowed_files"),
            "file_contents": None,  # agent reads on demand via read_file
            "diagnostics": patch_request_context.get("diagnostics"),
            "last_failure": patch_request_context.get("last_failure"),
            "plan_markdown": patch_request_context.get("plan_markdown"),
            "prior_step_files": patch_request_context.get("prior_step_files") or [],
            "prior_step_patches": patch_request_context.get("prior_step_patches") or {},
        }

        max_explore = budget.max_tool_calls_per_step
        max_verify = budget.max_verify_calls_per_step
        total_budget = max_explore + max_verify + 10  # generous outer cap
        for iteration in range(total_budget):
            phase = "explore" if sm.state == VerifyPhaseState.EXPLORE else "verify"
            _all_defs = self._registry.definitions(phase=phase)
            _allowed = sm.allowed_tools()
            tool_defs = [t.model_dump() for t in _all_defs if t.name in _allowed]
            # Schema snippets injected into patch-failure feedback so the model knows exact arg names.
            _rf_json = json.dumps(next((t for t in tool_defs if t["name"] == "read_file"), {}), indent=2)
            _sc_json = json.dumps(next((t for t in tool_defs if t["name"] == "search_code"), {}), indent=2)

            _thinking_chunks: list[str] = []

            def _on_thinking(chunk: str) -> None:
                _thinking_chunks.append(chunk)
                self._broadcaster.broadcast(self._broadcast_key, {
                    "type": "tool_thinking_chunk",
                    "payload": {"chunk": chunk},
                })

            try:
                response = await self._reasoning.create_tool_step(
                    step_context=step_context,
                    history=history,
                    tool_definitions=tool_defs,
                    on_thinking=_on_thinking,
                    state_description=sm.state_description(
                        iteration=iteration + 1,
                        error_summary=_last_auto_checks_error,
                        failure_summary=_last_test_failure,
                    ),
                    allowed_action_types=sm.allowed_action_types(),
                )
            except RuntimeError as exc:
                # Malformed / non-JSON response from the model. Inject into history
                # so the model self-corrects on the next iteration rather than losing
                # all explore context via a step restart.
                logger.warning(
                    "[loop] create_tool_step malformed response (iter=%d step=%s): %s",
                    iteration + 1, step.id, exc,
                )
                history.append({"role": "assistant", "content": "(malformed response)"})
                history.append({
                    "role": "tool_result", "tool": "_parse_error",
                    "content": (
                        f"Your previous response could not be parsed as valid JSON: {exc}. "
                        "Please retry with a well-formed JSON object matching the schema."
                    ),
                })
                continue
            finally:
                if _thinking_chunks:
                    logger.info(
                        "[loop] thinking: task=%s step=%s iter=%d\n%s",
                        self._task_id, step.id, iteration + 1,
                        "".join(_thinking_chunks),
                    )

            action_type = str(response.get("type", ""))
            thought = str(response.get("thought", ""))
            logger.info(
                "[loop] iter=%d phase=%s action=%s task=%s step=%s thought=%r",
                iteration + 1, phase, action_type, self._task_id, step.id,
                thought[:300] if thought else "",
            )

            # ── verify_done ──────────────────────────────────────────────
            if action_type == "verify_done":
                verified_flag = bool(response.get("verified", False))
                logger.info(
                    "[loop] verify_done: task=%s step=%s state=%s verified=%s",
                    self._task_id, step.id, sm.state.value, verified_flag,
                )
                # State machine owns when verify_done is valid; it's only present in the
                # schema for POSTPATCH_CLEAN and TEST_PASSED. This guard catches crafted
                # calls from other states (model bypassing the schema).
                if sm.state not in (
                    VerifyPhaseState.POSTPATCH_CLEAN, VerifyPhaseState.TEST_PASSED,
                ):
                    logger.warning(
                        "verify_done called from invalid state %s (step %s)",
                        sm.state.value, step.id, extra={"task_id": self._task_id},
                    )
                    history.append({
                        "role": "assistant",
                        "content": json.dumps(response, default=str),
                    })
                    history.append({
                        "role": "tool_result", "tool": "_verify_guard",
                        "content": (
                            "verify_done is not valid in the current state.\n"
                            f"{sm.state_description()}"
                        ),
                    })
                    continue

                return VerifyResult(
                    patch_document=last_patch_document,
                    touched_files=all_touched_files,
                    verified=verified_flag,
                    test_output=str(response.get("test_output", "")),
                    tool_trace=trace,
                )

            # ── revision_needed ──────────────────────────────────────────
            if action_type == "revision_needed":
                reason = str(response.get("reason", ""))
                evidence = str(response.get("evidence", ""))
                raw_affected = response.get("affected_steps", [])
                affected = [str(s) for s in raw_affected] if isinstance(raw_affected, list) else []
                logger.info("Tool loop revision_needed: %s", reason[:200],
                            extra={"task_id": self._task_id, "step_id": step.id})
                self._broadcaster.broadcast(self._broadcast_key, {
                    "type": "revision_needed",
                    "payload": {"step_id": step.id, "reason": reason, "evidence": evidence[:300]},
                })
                return PlanHandoff(
                    step_id=step.id, reason=reason, evidence=evidence,
                    hinted_affected_steps=affected, tool_trace=trace,
                )

            # ── emit_patch ───────────────────────────────────────────────
            if action_type == "emit_patch":
                patch_ops = response.get("patch_ops")
                if not isinstance(patch_ops, list):
                    raise ToolBudgetExceededError(
                        f"Step {step.id!r}: emit_patch has non-list 'patch_ops'"
                        f" at iteration {iteration}"
                    )

                # Dedup: block exact-repeat emit_patch within the current SM state stay.
                # The cache clears on every transition, so reads / patch failures / postpatch
                # events all reset it. Same-args retries are possible after the model reads.
                patch_key = _build_patch_key(patch_ops)
                if sm.check_patch_dedup(patch_key):
                    logger.warning(
                        "[loop] emit_patch dedup blocked: task=%s step=%s state=%s",
                        self._task_id, step.id, sm.state.value,
                    )
                    history.append({"role": "assistant", "content": json.dumps(response, default=str)})
                    history.append({
                        "role": "tool_result", "tool": "_patch_apply",
                        "content": (
                            "DUPLICATE PATCH BLOCKED: you already attempted this exact patch "
                            "in the current state. Read the file with read_file to get current "
                            "content, then emit a corrected patch.\n"
                            f"{sm.state_description()}"
                        ),
                    })
                    continue
                sm.record_patch_attempt(patch_key)

                patch_document = self._wrap_as_patch_document(patch_ops)
                history.append({"role": "assistant", "content": json.dumps(response, default=str)})

                # Apply inline if patch_engine is available
                if self._patch_engine is not None and self._shadow_path is not None:
                    apply_result = await self._apply_patch_inline(patch_document, step)

                    if apply_result.get("is_error"):
                        error_msg = str(apply_result.get("error", "patch application failed"))
                        logger.warning("Inline patch failed: %s", error_msg,
                                       extra={"task_id": self._task_id, "step_id": step.id})
                        is_scope_error = "outside current step scope" in error_msg

                        if is_scope_error:
                            out_of_scope = _extract_out_of_scope_files(error_msg)
                            decision = await self._scope_cb(out_of_scope, thought)

                            if decision.approve:
                                # Extend step.targets in place; new files default to "new".
                                existing = {t.path for t in step.targets}
                                for path in decision.extended_files:
                                    if path not in existing:
                                        step.targets.append(
                                            PlanTarget(path=path, intent=PlanTargetIntent.NEW)
                                        )
                                        existing.add(path)
                                # Retry preflight with extended scope.
                                apply_result = await self._apply_patch_inline(
                                    patch_document, step,
                                )
                                if apply_result.get("is_error"):
                                    # Different error after scope extension — feed it back.
                                    new_err = str(apply_result.get("error", "patch failed"))
                                    history.append({
                                        "role": "tool_result", "tool": "_patch_apply",
                                        "content": (
                                            f"Patch FAILED after scope extension: {new_err}\n"
                                            "Fix your patch ops and re-emit."
                                        ),
                                    })
                                    self._broadcaster.broadcast(self._broadcast_key, {
                                        "type": "patch_failed",
                                        "payload": {"step_id": step.id, "error": new_err},
                                    })
                                    try:
                                        sm.transition(VerifyPhaseEvent.PATCH_FAILED)
                                    except VerifyPhaseExhausted:
                                        logger.warning(
                                            "[loop] patch retries exhausted: task=%s step=%s",
                                            self._task_id, step.id,
                                        )
                                        return VerifyResult(
                                            patch_document=last_patch_document,
                                            touched_files=all_touched_files,
                                            verified=False,
                                            test_output=(
                                                f"Step {step.id!r}: emit_patch failed "
                                                f"{MAX_PATCH_RETRIES} consecutive times — "
                                                "giving up on this step attempt."
                                            ),
                                            tool_trace=trace,
                                        )
                                    continue
                                # Patch succeeded after extension — fall through to success path.
                            else:
                                had_scope_violation = True
                                feedback = (
                                    f"Patch FAILED: {error_msg}\n"
                                    f"Scope extension was not granted ({decision.reason}). "
                                    "This exact patch is now blocked from re-submission "
                                    "in the current state (dedup). "
                                    "Options: (1) emit a different patch using only your "
                                    "allowed files, or (2) emit revision_needed explaining "
                                    "which file must be added to the plan and why."
                                )
                                history.append({
                                    "role": "tool_result", "tool": "_patch_apply",
                                    "content": feedback,
                                })
                                self._broadcaster.broadcast(self._broadcast_key, {
                                    "type": "patch_failed",
                                    "payload": {"step_id": step.id, "error": error_msg},
                                })
                                continue  # stay in explore, agent corrects/revises
                        else:
                            if "appears" in error_msg and "times" in error_msg:
                                feedback = (
                                    f"Patch FAILED: {error_msg}\n"
                                    "Your search string matches multiple locations — it is not unique.\n"
                                    "DO NOT re-emit immediately. Instead:\n"
                                    "  1. Call search_code with the ambiguous text to see all occurrences.\n"
                                    "  2. Pick a longer, unique surrounding context from one occurrence.\n"
                                    "  3. Re-emit using that longer string as your search field.\n"
                                    "\nsearch_code tool schema:\n" + _sc_json
                                )
                            elif "not found" in error_msg.lower():
                                feedback = (
                                    f"Patch FAILED: {error_msg}\n"
                                    "The search text does not exist in the file.\n"
                                    "DO NOT re-emit immediately. You MUST read the file first:\n"
                                    "  1. Call read_file on the target file to get its exact current content.\n"
                                    "  2. Find the exact text as it exists now.\n"
                                    "  3. Re-emit using ONLY text returned by read_file as your search field.\n"
                                    "\nread_file tool schema:\n" + _rf_json + "\n"
                                    "\nsearch_code tool schema (alternative):\n" + _sc_json
                                )
                            else:
                                feedback = (
                                    f"Patch FAILED: {error_msg}\n"
                                    "Call read_file or search_code to get the current file content, "
                                    "then re-emit using only text you just read.\n"
                                    "\nread_file tool schema:\n" + _rf_json + "\n"
                                    "\nsearch_code tool schema:\n" + _sc_json
                                )
                            history.append({
                                "role": "tool_result", "tool": "_patch_apply",
                                "content": feedback,
                            })
                            self._broadcaster.broadcast(self._broadcast_key, {
                                "type": "patch_failed",
                                "payload": {"step_id": step.id, "error": error_msg},
                            })
                            try:
                                sm.transition(VerifyPhaseEvent.PATCH_FAILED)
                            except VerifyPhaseExhausted:
                                logger.warning(
                                    "[loop] patch retries exhausted: task=%s step=%s",
                                    self._task_id, step.id,
                                )
                                return VerifyResult(
                                    patch_document=last_patch_document,
                                    touched_files=all_touched_files,
                                    verified=False,
                                    test_output=(
                                        f"Step {step.id!r}: emit_patch failed "
                                        f"{MAX_PATCH_RETRIES} consecutive times — "
                                        "giving up on this step attempt."
                                    ),
                                    tool_trace=trace,
                                )
                            continue  # SM moved to PATCH_FAILED_MUST_READ; model must read

                    # Patch succeeded
                    touched = apply_result.get("touched_files", [])
                    if isinstance(touched, list):
                        for f in touched:
                            if f not in all_touched_files:
                                all_touched_files.append(str(f))
                else:
                    # No patch engine (scripted tests without inline apply) — extract touched files
                    for op in patch_ops:
                        if isinstance(op, dict) and "file" in op:
                            f = str(op["file"])
                            if f not in all_touched_files:
                                all_touched_files.append(f)

                last_patch_document = patch_document
                logger.info(
                    "Inline patch applied successfully",
                    extra={
                        "task_id": self._task_id, "step_id": step.id,
                        "touched_files": all_touched_files,
                    },
                )

                # Patch succeeded — switch to shadow reads, run postpatch analyzer,
                # fire POSTPATCH_BLOCKING or POSTPATCH_CLEAN event.
                self._registry.use_shadow_for_reads()
                touched_files_str = ", ".join(all_touched_files) or "none"
                testing_strategy = step.testing_strategy or "not specified"
                test_cmd_hint = step.test_command or "none — derive from testing_strategy and touched files"
                _shadow_root = getattr(self._registry, "_shadow_root", None)
                auto_checks, _blocking_clean = (
                    await _POST_PATCH_ANALYZER.analyze(
                        _shadow_root,
                        all_touched_files,
                        baseline=self._static_baseline,
                    )
                    if _shadow_root is not None
                    else ("", True)
                )
                _last_auto_checks_error = (
                    auto_checks.strip() if (not _blocking_clean and auto_checks) else ""
                )
                _last_test_failure = ""  # a fresh patch invalidates any prior test failure
                postpatch_event = (
                    VerifyPhaseEvent.POSTPATCH_CLEAN
                    if _blocking_clean
                    else VerifyPhaseEvent.POSTPATCH_BLOCKING
                )
                sm.transition(postpatch_event)
                logger.info(
                    "[loop] patch applied: task=%s step=%s touched=%s state→%s",
                    self._task_id, step.id, all_touched_files, sm.state.value,
                )
                history.append({
                    "role": "tool_result", "tool": "_patch_apply",
                    "content": (
                        f"Patch applied successfully.\n"
                        f"Touched files: {touched_files_str}\n"
                        f"testing_strategy: {testing_strategy}\n"
                        f"test_command hint: {test_cmd_hint}\n"
                        + auto_checks
                    ),
                })
                self._broadcaster.broadcast(self._broadcast_key, {
                    "type": "patch_applied",
                    "payload": {"step_id": step.id, "phase": "verify", "touched_files": all_touched_files},
                })
                if self._skip_verify:
                    return VerifyResult(
                        patch_document=last_patch_document,
                        touched_files=all_touched_files,
                        verified=True,
                        test_output="",
                        tool_trace=trace,
                    )
                continue

            # ── tool_call ────────────────────────────────────────────────
            # Reads are no longer deduped — re-reading a file is harmless and often
            # necessary after a patch. Only emit_patch is dedup-checked, via the SM.

            if action_type != "tool_call":
                raise ToolBudgetExceededError(
                    f"Step {step.id!r}: unexpected response type '{action_type}'"
                    f" at iteration {iteration}"
                )

            # Budget enforcement per phase
            if phase == "explore":
                if explore_calls >= max_explore:
                    if had_scope_violation:
                        # Agent kept burning budget after a scope rejection without emitting
                        # revision_needed. Convert to PlanHandoff so the outer retry doesn't
                        # fire — we already know the plan needs a target added.
                        logger.warning(
                            "Explore budget exhausted after scope violation (step %s) — "
                            "converting to PlanHandoff to skip outer retry",
                            step.id, extra={"task_id": self._task_id},
                        )
                        self._broadcaster.broadcast(self._broadcast_key, {
                            "type": "revision_needed",
                            "payload": {
                                "step_id": step.id,
                                "reason": "Scope violation: required file not in step targets",
                                "evidence": "Patch rejected for out-of-scope file; agent exhausted budget without emitting revision_needed",  # noqa: E501
                            },
                        })
                        return PlanHandoff(
                            step_id=step.id,
                            reason="Scope violation: required file not in step targets",
                            evidence=(
                                "Patch rejected for out-of-scope file. "
                                "Agent exhausted explore budget without emitting revision_needed. "
                                "The plan should add the required file as a target."
                            ),
                            hinted_affected_steps=[],
                            tool_trace=trace,
                        )
                    raise ToolBudgetExceededError(
                        f"Step {step.id!r}: explore budget ({max_explore})"
                        " exhausted without emitting a patch"
                    )
                explore_calls += 1
            else:
                if verify_calls >= max_verify:
                    return VerifyResult(
                        patch_document=last_patch_document,
                        touched_files=all_touched_files,
                        verified=False,
                        test_output=(
                            f"Verify budget exhausted after {verify_calls} calls"
                            " without passing checks"
                        ),
                        tool_trace=trace,
                    )
                verify_calls += 1

            tool_name = str(response.get("tool", ""))
            raw_args = response.get("args")
            args: dict[str, object] = raw_args if isinstance(raw_args, dict) else {}

            args_repr = json.dumps(args, default=str)[:300]
            logger.info(
                "[loop] tool_call: task=%s step=%s phase=%s iter=%d tool=%s args=%s",
                self._task_id, step.id, phase, iteration + 1, tool_name, args_repr,
            )
            self._broadcaster.broadcast(self._broadcast_key, {
                "type": "tool_call",
                "payload": {"tool": tool_name, "thought": thought[:300], "iteration": iteration + 1, "phase": phase, "args": args},
            })
            if self._thinking_log is not None:
                path = str(args.get("path", "")) if isinstance(args, dict) else ""
                file_label = f" {path.split('/')[-1]}" if path else ""
                self._thinking_log.append(f"{tool_name}{file_label} — {thought[:200]}" if thought else f"{tool_name}{file_label}")

            tool_output = await self._registry.execute(tool_name, args)
            usage.tool_calls_used += 1

            # READ_CALLED fires only when the model reads while in PATCH_FAILED_MUST_READ.
            # In all other states reads execute without a SM transition.
            if (
                tool_name in ("read_file", "search_code")
                and sm.state == VerifyPhaseState.PATCH_FAILED_MUST_READ
                and not tool_output.is_error
            ):
                sm.transition(VerifyPhaseEvent.READ_CALLED)
                logger.info(
                    "[loop] READ_CALLED: task=%s step=%s state→%s",
                    self._task_id, step.id, sm.state.value,
                )

            # Prior-step file nudge: if the model reads a file touched by an accepted
            # earlier step, remind it that the content is current (already promoted)
            # and it should not re-implement what was already done.
            if tool_name == "read_file" and not tool_output.is_error:
                _read_path = str(args.get("path", ""))
                _prior_files: list[str] = step_context.get("prior_step_files") or []  # type: ignore[assignment]
                _target_paths = {t["path"] for t in (step_context.get("targets") or []) if isinstance(t, dict)}  # type: ignore[index]
                if _read_path in _prior_files:
                    _is_target = _read_path in _target_paths
                    _scope_note = (
                        "It is one of your targets — you may patch it directly."
                        if _is_target
                        else "It is not in your targets — patching it will trigger a scope-extension prompt."
                    )
                    _prior_nudge = (
                        f"\n\n⚠️  PRIOR-STEP FILE: `{_read_path}` was modified and accepted by an earlier step. "
                        "The content above already reflects those changes — do NOT re-implement what is already there. "
                        f"{_scope_note} "
                        "Only add changes NEW to your step's goal."
                    )
                    from agentd.tools.registry import ToolOutput as _ToolOutput
                    tool_output = _ToolOutput(
                        output=tool_output.output + _prior_nudge,
                        is_error=False,
                    )
                    logger.info(
                        "[loop] prior-step nudge injected: task=%s step=%s file=%s",
                        self._task_id, step.id, _read_path,
                    )

            out_preview = tool_output.output[:200].replace("\n", "↵")
            logger.info(
                "[loop] tool_result: task=%s step=%s tool=%s is_error=%s chars=%d preview=%r",
                self._task_id, step.id, tool_name, tool_output.is_error, len(tool_output.output), out_preview,
            )

            # Fire TEST_PASSED / TEST_FAILED on run_command result (only from states
            # where run_command is in the allowed set; the schema enforces this, but
            # we still gate the SM dispatch to avoid InvalidVerifyPhaseTransition).
            if tool_name == "run_command" and sm.state in (
                VerifyPhaseState.POSTPATCH_CLEAN, VerifyPhaseState.TEST_FAILED,
            ):
                test_event = (
                    VerifyPhaseEvent.TEST_PASSED
                    if not tool_output.is_error
                    else VerifyPhaseEvent.TEST_FAILED
                )
                sm.transition(test_event)
                _last_test_failure = (
                    tool_output.output[:300] if tool_output.is_error else ""
                )
                logger.info(
                    "[loop] run_command result: task=%s step=%s is_error=%s state→%s",
                    self._task_id, step.id, tool_output.is_error, sm.state.value,
                )
            # find_binary / setup_env / init_workspace are diagnostic — they don't
            # fire SM events. Their result text stays in history for the model to read.

            self._broadcaster.broadcast(self._broadcast_key, {
                "type": "tool_result",
                "payload": {"tool": tool_name, "output": tool_output.output[:500], "is_error": tool_output.is_error, "iteration": iteration + 1},
            })

            call_id = f"{step.id}-{uuid4().hex[:8]}"
            trace.calls.append(ToolCall(call_id=call_id, tool_name=tool_name, arguments=args))
            trace.results.append(ToolResult(
                call_id=call_id, tool_name=tool_name,
                output=tool_output.output[:_MAX_OUTPUT_INJECT_CHARS],
                is_error=tool_output.is_error,
            ))

            history.append({"role": "assistant", "content": json.dumps(response, default=str)})
            history.append({
                "role": "tool_result", "tool": tool_name,
                "content": tool_output.output[:_MAX_OUTPUT_INJECT_CHARS],
            })

        if had_scope_violation:
            logger.warning(
                "Total budget exceeded after scope violation (step %s) — converting to PlanHandoff",
                step.id, extra={"task_id": self._task_id},
            )
            return PlanHandoff(
                step_id=step.id,
                reason="Scope violation: required file not in step targets",
                evidence=(
                    "Patch rejected for out-of-scope file. "
                    "Agent exhausted total budget without emitting revision_needed. "
                    "The plan should add the required file as a target."
                ),
                hinted_affected_steps=[],
                tool_trace=trace,
            )
        raise ToolBudgetExceededError(f"Step {step.id!r}: total budget exceeded")

    async def _apply_patch_inline(
        self,
        patch_document: dict[str, object],
        step: PlanStep,
    ) -> dict[str, object]:
        """Apply patch_document to shadow_path. Returns {touched_files, is_error, error}."""
        from pydantic import ValidationError

        from agentd.domain.models import PatchDocumentV2

        assert self._patch_engine is not None
        assert self._shadow_path is not None

        try:
            doc = PatchDocumentV2.model_validate(patch_document)
        except (ValidationError, Exception) as exc:
            return {"is_error": True, "error": f"Invalid patch document: {exc}", "touched_files": []}  # noqa: E501

        if not doc.candidates:
            return {"is_error": True, "error": "No candidates in patch document", "touched_files": []}  # noqa: E501

        candidate = doc.candidates[0]
        allowed_files = {t.path for t in step.targets}

        try:
            result = await self._patch_engine.apply_patch_candidate(  # type: ignore[attr-defined]
                self._shadow_path,
                candidate,
                allowed_files=allowed_files,
            )
        except Exception as exc:
            return {"is_error": True, "error": str(exc), "touched_files": []}

        return {"is_error": False, "touched_files": result.touched_files}

    @staticmethod
    def _wrap_as_patch_document(patch_ops: list[object]) -> dict[str, object]:
        """Wrap patch ops into a PatchDocumentV2-compatible raw dict."""
        return {
            "candidates": [
                {
                    "candidate_id": "tool-loop-c1",
                    "patch_ops": patch_ops,
                }
            ]
        }


def build_tool_registry(
    shadow_root: Path,
    retrieval_client: object | None = None,
    real_workspace_path: Path | None = None,
) -> ToolRegistry:
    """Construct a ToolRegistry for a step, extracting the semantic index if available."""
    semantic_index = getattr(retrieval_client, "_semantic_index", None)
    return ToolRegistry(
        shadow_root=shadow_root,
        real_workspace_path=real_workspace_path or shadow_root,
        semantic_index=semantic_index,
    )
