# Verify Phase State Machine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace five scattered boolean flags in `tools/loop.py` with an explicit `VerifyPhaseStateMachine` that hard-enforces tool availability per state, clears emit_patch dedup on every transition, and injects a per-turn contextual prompt telling the model what state it is in and what to do next.

**Architecture:** Three-file change. A new `agentd/tools/verify_phase_sm.py` owns all state machine logic. `loop.py` imports it, removes the five flags, and wires event dispatch at four points: (1) patch failure → `PATCH_FAILED`; (2) read called while in `PATCH_FAILED_MUST_READ` → `READ_CALLED`; (3) PostPatchAnalyzer result after a successful patch → `POSTPATCH_BLOCKING` or `POSTPATCH_CLEAN`; (4) run_command result → `TEST_PASSED` or `TEST_FAILED`. Note: patch success is not itself a state machine event — the loop runs PostPatchAnalyzer immediately and fires the postpatch event directly. `tool_prompts.py` removes all static verify-phase guidance and injects `sm.state_description()` into the per-turn `instruction` field of the user payload.

**Tech Stack:** Python 3.11+, pytest-asyncio, existing `agentd` conventions (no new dependencies).

**Spec:** `docs/superpowers/specs/2026-05-20-verify-phase-statemachine-design.md`

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `agentd/tools/verify_phase_sm.py` | Create | State enum, event enum, transition table, `VerifyPhaseStateMachine` class, exception classes |
| `tests/test_verify_phase_sm.py` | Create | Unit tests for state machine — transitions, dedup, retry counter, state_description |
| `agentd/tools/loop.py` | Modify | Remove 5 flags, import SM, wire events, replace dedup, simplify guards |
| `agentd/reasoning/tool_prompts.py` | Modify | Remove static verify-phase sections, add `state_description` param to `build_tool_step_payload` |

---

## Task 1: Create `verify_phase_sm.py`

**Files:**
- Create: `services/agentd-py/agentd/tools/verify_phase_sm.py`

- [ ] **Step 1: Write `verify_phase_sm.py`**

```python
"""Explicit state machine for the verify phase of the tool-use loop."""
from __future__ import annotations

from enum import Enum


class VerifyPhaseState(str, Enum):
    EXPLORE                = "EXPLORE"
    PATCH_FAILED_MUST_READ = "PATCH_FAILED_MUST_READ"
    PATCH_FAILED_CAN_RETRY = "PATCH_FAILED_CAN_RETRY"
    POSTPATCH_BLOCKING     = "POSTPATCH_BLOCKING"
    POSTPATCH_CLEAN        = "POSTPATCH_CLEAN"
    TEST_FAILED            = "TEST_FAILED"
    TEST_PASSED            = "TEST_PASSED"


class VerifyPhaseEvent(str, Enum):
    PATCH_FAILED       = "patch_failed"
    READ_CALLED        = "read_called"
    POSTPATCH_BLOCKING = "postpatch_blocking"
    POSTPATCH_CLEAN    = "postpatch_clean"
    TEST_PASSED        = "test_passed"
    TEST_FAILED        = "test_failed"


class VerifyPhaseExhausted(Exception):
    """Raised when PATCH_FAILED_CAN_RETRY exhausts MAX_PATCH_RETRIES consecutive failures."""


class InvalidVerifyPhaseTransition(Exception):
    """Raised for a (state, event) pair that has no defined transition."""


MAX_PATCH_RETRIES: int = 5

_S = VerifyPhaseState
_E = VerifyPhaseEvent

# All (state, event) → next_state pairs that the loop actually dispatches.
# READ_CALLED is only dispatched from PATCH_FAILED_MUST_READ.
# PATCH_FAILED from PATCH_FAILED_CAN_RETRY is handled inline (counter check).
_TRANSITIONS: dict[tuple[_S, _E], _S] = {
    (_S.EXPLORE,                _E.PATCH_FAILED):       _S.PATCH_FAILED_MUST_READ,
    (_S.EXPLORE,                _E.POSTPATCH_BLOCKING): _S.POSTPATCH_BLOCKING,
    (_S.EXPLORE,                _E.POSTPATCH_CLEAN):    _S.POSTPATCH_CLEAN,

    (_S.PATCH_FAILED_MUST_READ, _E.READ_CALLED):        _S.PATCH_FAILED_CAN_RETRY,

    # PATCH_FAILED from CAN_RETRY handled inline below (counter check).
    (_S.PATCH_FAILED_CAN_RETRY, _E.POSTPATCH_BLOCKING): _S.POSTPATCH_BLOCKING,
    (_S.PATCH_FAILED_CAN_RETRY, _E.POSTPATCH_CLEAN):    _S.POSTPATCH_CLEAN,

    (_S.POSTPATCH_BLOCKING,     _E.PATCH_FAILED):       _S.PATCH_FAILED_MUST_READ,
    (_S.POSTPATCH_BLOCKING,     _E.POSTPATCH_BLOCKING): _S.POSTPATCH_BLOCKING,
    (_S.POSTPATCH_BLOCKING,     _E.POSTPATCH_CLEAN):    _S.POSTPATCH_CLEAN,

    (_S.POSTPATCH_CLEAN,        _E.TEST_FAILED):        _S.TEST_FAILED,
    (_S.POSTPATCH_CLEAN,        _E.TEST_PASSED):        _S.TEST_PASSED,

    (_S.TEST_FAILED,            _E.PATCH_FAILED):       _S.PATCH_FAILED_MUST_READ,
    (_S.TEST_FAILED,            _E.POSTPATCH_BLOCKING): _S.POSTPATCH_BLOCKING,
    (_S.TEST_FAILED,            _E.POSTPATCH_CLEAN):    _S.POSTPATCH_CLEAN,
    (_S.TEST_FAILED,            _E.TEST_FAILED):        _S.TEST_FAILED,
    (_S.TEST_FAILED,            _E.TEST_PASSED):        _S.TEST_PASSED,
}

_ALLOWED_TOOLS: dict[_S, frozenset[str]] = {
    _S.EXPLORE:                frozenset({"read_file", "search_code", "list_directory",
                                           "search_semantic", "emit_patch"}),
    _S.PATCH_FAILED_MUST_READ: frozenset({"read_file", "search_code", "list_directory",
                                           "search_semantic"}),
    _S.PATCH_FAILED_CAN_RETRY: frozenset({"read_file", "search_code", "list_directory",
                                           "search_semantic", "emit_patch"}),
    _S.POSTPATCH_BLOCKING:     frozenset({"read_file", "search_code", "list_directory",
                                           "search_semantic", "emit_patch"}),
    _S.POSTPATCH_CLEAN:        frozenset({"read_file", "search_code", "list_directory",
                                           "search_semantic", "run_command", "verify_done",
                                           "find_binary", "setup_env", "init_workspace"}),
    _S.TEST_FAILED:            frozenset({"read_file", "search_code", "list_directory",
                                           "search_semantic", "emit_patch", "run_command",
                                           "find_binary", "setup_env", "init_workspace"}),
    _S.TEST_PASSED:            frozenset({"read_file", "search_code", "list_directory",
                                           "search_semantic", "verify_done"}),
}

_FAILED_PATCH_STATES = frozenset({_S.PATCH_FAILED_MUST_READ, _S.PATCH_FAILED_CAN_RETRY})


class VerifyPhaseStateMachine:
    """Tracks verify-phase state, enforces tool availability, and manages emit_patch dedup."""

    def __init__(self) -> None:
        self.state: VerifyPhaseState = _S.EXPLORE
        self._retry_count: int = 0
        self._seen_patch_calls: set[tuple] = set()

    def transition(self, event: VerifyPhaseEvent) -> VerifyPhaseState:
        """Apply event, update state, clear patch dedup cache.

        Raises:
            VerifyPhaseExhausted: when PATCH_FAILED_CAN_RETRY hits MAX_PATCH_RETRIES.
            InvalidVerifyPhaseTransition: for any dispatched (state, event) not in the table.
        """
        # Special: PATCH_FAILED from CAN_RETRY increments counter and may exhaust.
        if self.state == _S.PATCH_FAILED_CAN_RETRY and event == _E.PATCH_FAILED:
            self._retry_count += 1
            if self._retry_count >= MAX_PATCH_RETRIES:
                raise VerifyPhaseExhausted(
                    f"emit_patch failed {self._retry_count} consecutive times in "
                    "PATCH_FAILED_CAN_RETRY — step exhausted"
                )
            self._seen_patch_calls = set()
            self.state = _S.PATCH_FAILED_MUST_READ
            return self.state

        key = (self.state, event)
        if key not in _TRANSITIONS:
            raise InvalidVerifyPhaseTransition(
                f"No transition defined for state={self.state.value!r}, event={event.value!r}"
            )

        next_state = _TRANSITIONS[key]
        # Reset retry counter when exiting PATCH_FAILED states on success.
        if self.state in _FAILED_PATCH_STATES and next_state not in _FAILED_PATCH_STATES:
            self._retry_count = 0

        self._seen_patch_calls = set()
        self.state = next_state
        return self.state

    def allowed_tools(self) -> frozenset[str]:
        """Tool names available in the current state. Used to filter the JSON schema."""
        return _ALLOWED_TOOLS[self.state]

    def check_patch_dedup(self, patch_key: tuple) -> bool:
        """True if this exact patch was already attempted in the current state stay."""
        return patch_key in self._seen_patch_calls

    def record_patch_attempt(self, patch_key: tuple) -> None:
        """Record an emit_patch attempt for dedup tracking."""
        self._seen_patch_calls.add(patch_key)

    def is_terminal(self) -> bool:
        return self.state == _S.TEST_PASSED

    def state_description(
        self,
        *,
        error_summary: str = "",
        failure_summary: str = "",
    ) -> str:
        """Contextual prompt injected into the model's instruction field each turn."""
        s = self.state
        rc, mx = self._retry_count, MAX_PATCH_RETRIES

        if s == _S.EXPLORE:
            return (
                "CURRENT STATE: EXPLORE\n"
                "No patch has been applied yet. Read the relevant files, search for symbols, "
                "and understand the code structure. When you have enough context, emit your patch.\n"
                "Available tools: read_file, search_code, list_directory, emit_patch"
            )

        if s == _S.PATCH_FAILED_MUST_READ:
            return (
                "CURRENT STATE: PATCH_FAILED — MUST READ BEFORE RETRYING\n"
                "Your last patch failed — the file content doesn't match what your patch "
                "expected. This applies to any op type: search text not found, diff doesn't "
                "apply, AST node missing, byte range wrong. The file may have changed since "
                "you last read it, or your assumptions were off.\n\n"
                "emit_patch is unavailable right now. Read the actual current file content "
                "first. Once you call read_file or search_code, emit_patch becomes available again.\n"
                "Available tools: read_file, search_code, list_directory\n"
                "Next: read the file → emit_patch unlocks"
            )

        if s == _S.PATCH_FAILED_CAN_RETRY:
            return (
                f"CURRENT STATE: PATCH_FAILED — RETRY {rc} of {mx}\n"
                "You've read the file. emit_patch is available again. "
                "Use what you just read to construct a correct patch — exact text, correct "
                "line range, or the right AST node, depending on your op type. "
                "If the same approach keeps failing, try a different operation type "
                "(e.g. apply_diff instead of search_replace).\n"
                "Available tools: read_file, search_code, list_directory, emit_patch"
            )

        if s == _S.POSTPATCH_BLOCKING:
            summary = f"\n{error_summary}\n" if error_summary else ""
            return (
                "CURRENT STATE: POSTPATCH — BLOCKING ERRORS\n"
                "Your patch applied but static analysis (py_compile / mypy) found blocking "
                f"errors that must be fixed before running tests:{summary}\n"
                "Read the affected lines, then emit a corrective patch. "
                "run_command is unavailable until these errors are resolved.\n"
                "Available tools: read_file, search_code, list_directory, emit_patch"
            )

        if s == _S.POSTPATCH_CLEAN:
            return (
                "CURRENT STATE: POSTPATCH — CLEAN\n"
                "Static checks passed — no compile or type errors.\n"
                "Run the relevant tests with run_command to confirm correctness. "
                "If this step has no automated tests (docs, config, comment-only changes), "
                "call verify_done(True) directly.\n"
                "Available tools: read_file, search_code, list_directory, run_command, verify_done"
            )

        if s == _S.TEST_FAILED:
            summary = f"\n{failure_summary}\n" if failure_summary else ""
            return (
                f"CURRENT STATE: TEST_FAILED{summary}\n"
                "Tests ran but failed. Read the failure output, locate the issue, "
                "and emit a corrective patch. You can re-run a narrower test command "
                "after patching — no need to run the full suite again.\n"
                "Available tools: read_file, search_code, list_directory, emit_patch, run_command"
            )

        if s == _S.TEST_PASSED:
            return (
                "CURRENT STATE: TEST_PASSED\n"
                "Tests passed. Call verify_done(True) to complete this step.\n"
                "Available tools: read_file, verify_done"
            )

        return f"CURRENT STATE: {s.value}"
```

- [ ] **Step 2: Run the (not-yet-existing) tests to confirm they fail cleanly**

```bash
cd services/agentd-py && source .venv/bin/activate
pytest tests/test_verify_phase_sm.py -v 2>&1 | head -5
```

Expected: `ERROR` — test file doesn't exist yet. Confirms test runner is healthy.

- [ ] **Step 3: Commit the new module**

```bash
git add services/agentd-py/agentd/tools/verify_phase_sm.py
git commit -m "feat(verify-phase-sm): add VerifyPhaseStateMachine module"
```

---

## Task 2: Write unit tests for the state machine

**Files:**
- Create: `services/agentd-py/tests/test_verify_phase_sm.py`

- [ ] **Step 1: Write the test file**

```python
"""Unit tests for VerifyPhaseStateMachine."""
from __future__ import annotations
import pytest
from agentd.tools.verify_phase_sm import (
    VerifyPhaseState as S,
    VerifyPhaseEvent as E,
    VerifyPhaseStateMachine,
    VerifyPhaseExhausted,
    InvalidVerifyPhaseTransition,
    MAX_PATCH_RETRIES,
)


def make_sm() -> VerifyPhaseStateMachine:
    return VerifyPhaseStateMachine()


# ── initial state ─────────────────────────────────────────────────────────────

def test_initial_state_is_explore():
    sm = make_sm()
    assert sm.state == S.EXPLORE
    assert not sm.is_terminal()


# ── happy path: explore → postpatch_clean → test_passed ───────────────────────

def test_explore_postpatch_clean_test_passed():
    sm = make_sm()
    sm.transition(E.POSTPATCH_CLEAN)
    assert sm.state == S.POSTPATCH_CLEAN
    sm.transition(E.TEST_PASSED)
    assert sm.state == S.TEST_PASSED
    assert sm.is_terminal()


def test_explore_postpatch_blocking_then_clean():
    sm = make_sm()
    sm.transition(E.POSTPATCH_BLOCKING)
    assert sm.state == S.POSTPATCH_BLOCKING
    sm.transition(E.POSTPATCH_CLEAN)
    assert sm.state == S.POSTPATCH_CLEAN


# ── PATCH_FAILED cycle ────────────────────────────────────────────────────────

def test_explore_patch_failed_goes_to_must_read():
    sm = make_sm()
    sm.transition(E.PATCH_FAILED)
    assert sm.state == S.PATCH_FAILED_MUST_READ


def test_must_read_read_called_goes_to_can_retry():
    sm = make_sm()
    sm.transition(E.PATCH_FAILED)
    sm.transition(E.READ_CALLED)
    assert sm.state == S.PATCH_FAILED_CAN_RETRY


def test_can_retry_patch_failed_increments_counter_and_goes_to_must_read():
    sm = make_sm()
    sm.transition(E.PATCH_FAILED)   # → MUST_READ
    sm.transition(E.READ_CALLED)    # → CAN_RETRY
    sm.transition(E.PATCH_FAILED)   # fail in CAN_RETRY
    assert sm.state == S.PATCH_FAILED_MUST_READ
    assert sm._retry_count == 1


def test_can_retry_exhausted_raises():
    sm = make_sm()
    for _ in range(MAX_PATCH_RETRIES):
        sm.transition(E.PATCH_FAILED)   # → MUST_READ
        sm.transition(E.READ_CALLED)    # → CAN_RETRY
        if sm._retry_count < MAX_PATCH_RETRIES - 1:
            sm.transition(E.PATCH_FAILED)
    with pytest.raises(VerifyPhaseExhausted):
        sm.transition(E.PATCH_FAILED)


def test_retry_count_resets_on_postpatch_clean():
    sm = make_sm()
    sm.transition(E.PATCH_FAILED)   # → MUST_READ
    sm.transition(E.READ_CALLED)    # → CAN_RETRY
    sm.transition(E.PATCH_FAILED)   # fail; count=1
    assert sm._retry_count == 1
    sm.transition(E.READ_CALLED)    # → CAN_RETRY
    sm.transition(E.POSTPATCH_CLEAN)
    assert sm._retry_count == 0


# ── test_failed paths ─────────────────────────────────────────────────────────

def test_postpatch_clean_test_failed_goes_to_test_failed():
    sm = make_sm()
    sm.transition(E.POSTPATCH_CLEAN)
    sm.transition(E.TEST_FAILED)
    assert sm.state == S.TEST_FAILED


def test_test_failed_self_loop():
    sm = make_sm()
    sm.transition(E.POSTPATCH_CLEAN)
    sm.transition(E.TEST_FAILED)
    sm.transition(E.TEST_FAILED)
    assert sm.state == S.TEST_FAILED


def test_test_failed_test_passed():
    sm = make_sm()
    sm.transition(E.POSTPATCH_CLEAN)
    sm.transition(E.TEST_FAILED)
    sm.transition(E.TEST_PASSED)
    assert sm.state == S.TEST_PASSED
    assert sm.is_terminal()


def test_test_failed_patch_failed_goes_to_must_read():
    sm = make_sm()
    sm.transition(E.POSTPATCH_CLEAN)
    sm.transition(E.TEST_FAILED)
    sm.transition(E.PATCH_FAILED)
    assert sm.state == S.PATCH_FAILED_MUST_READ


# ── dedup cache ───────────────────────────────────────────────────────────────

def test_dedup_within_state():
    sm = make_sm()
    key = ("search_replace", "a.py", "old", "new")
    assert not sm.check_patch_dedup(key)
    sm.record_patch_attempt(key)
    assert sm.check_patch_dedup(key)


def test_dedup_clears_on_transition():
    sm = make_sm()
    key = ("search_replace", "a.py", "old", "new")
    sm.record_patch_attempt(key)
    sm.transition(E.PATCH_FAILED)   # → MUST_READ; cache should clear
    assert not sm.check_patch_dedup(key)


def test_dedup_clears_on_postpatch_transition():
    sm = make_sm()
    key = ("search_replace", "a.py", "old", "new")
    sm.record_patch_attempt(key)
    sm.transition(E.POSTPATCH_CLEAN)
    assert not sm.check_patch_dedup(key)


# ── allowed_tools ─────────────────────────────────────────────────────────────

def test_allowed_tools_explore_includes_emit_patch():
    sm = make_sm()
    assert "emit_patch" in sm.allowed_tools()
    assert "run_command" not in sm.allowed_tools()
    assert "verify_done" not in sm.allowed_tools()


def test_allowed_tools_must_read_excludes_emit_patch():
    sm = make_sm()
    sm.transition(E.PATCH_FAILED)
    assert "emit_patch" not in sm.allowed_tools()
    assert "run_command" not in sm.allowed_tools()


def test_allowed_tools_postpatch_clean_includes_run_and_verify():
    sm = make_sm()
    sm.transition(E.POSTPATCH_CLEAN)
    tools = sm.allowed_tools()
    assert "run_command" in tools
    assert "verify_done" in tools
    assert "emit_patch" not in tools


def test_allowed_tools_test_failed_includes_emit_patch_and_run():
    sm = make_sm()
    sm.transition(E.POSTPATCH_CLEAN)
    sm.transition(E.TEST_FAILED)
    tools = sm.allowed_tools()
    assert "emit_patch" in tools
    assert "run_command" in tools
    assert "verify_done" not in tools


def test_allowed_tools_test_passed_only_verify_done_and_reads():
    sm = make_sm()
    sm.transition(E.POSTPATCH_CLEAN)
    sm.transition(E.TEST_PASSED)
    tools = sm.allowed_tools()
    assert "verify_done" in tools
    assert "emit_patch" not in tools
    assert "run_command" not in tools
    assert "read_file" in tools


# ── invalid transitions ───────────────────────────────────────────────────────

def test_invalid_transition_raises():
    sm = make_sm()
    # READ_CALLED is only valid from PATCH_FAILED_MUST_READ
    with pytest.raises(InvalidVerifyPhaseTransition):
        sm.transition(E.READ_CALLED)


def test_invalid_transition_test_failed_from_explore():
    sm = make_sm()
    with pytest.raises(InvalidVerifyPhaseTransition):
        sm.transition(E.TEST_FAILED)


# ── state_description ─────────────────────────────────────────────────────────

def test_state_description_explore_contains_emit_patch():
    sm = make_sm()
    desc = sm.state_description()
    assert "emit_patch" in desc
    assert "EXPLORE" in desc


def test_state_description_must_read_says_emit_patch_unavailable():
    sm = make_sm()
    sm.transition(E.PATCH_FAILED)
    desc = sm.state_description()
    assert "emit_patch is unavailable" in desc
    assert "read_file" in desc


def test_state_description_can_retry_shows_counter():
    sm = make_sm()
    sm.transition(E.PATCH_FAILED)
    sm.transition(E.READ_CALLED)
    sm.transition(E.PATCH_FAILED)  # count = 1
    sm.transition(E.READ_CALLED)   # → CAN_RETRY
    desc = sm.state_description()
    assert "1 of" in desc


def test_state_description_postpatch_blocking_shows_summary():
    sm = make_sm()
    sm.transition(E.POSTPATCH_BLOCKING)
    desc = sm.state_description(error_summary="NameError: foo not defined")
    assert "NameError: foo not defined" in desc


def test_state_description_test_failed_shows_summary():
    sm = make_sm()
    sm.transition(E.POSTPATCH_CLEAN)
    sm.transition(E.TEST_FAILED)
    desc = sm.state_description(failure_summary="FAILED tests/test_foo.py::test_bar")
    assert "test_bar" in desc
```

- [ ] **Step 2: Run tests — expect all to pass**

```bash
cd services/agentd-py && source .venv/bin/activate
pytest tests/test_verify_phase_sm.py -v
```

Expected: all green. If any fail, fix `verify_phase_sm.py` before continuing.

- [ ] **Step 3: Commit**

```bash
git add services/agentd-py/tests/test_verify_phase_sm.py
git commit -m "test(verify-phase-sm): unit tests for state machine transitions, dedup, and descriptions"
```

---

## Task 3: Integrate state machine into `loop.py`

**Files:**
- Modify: `services/agentd-py/agentd/tools/loop.py`

The goal is to replace these five variables from `run()`:
- `last_verify_run_errored` → state machine (verify_done blocked by state)
- `verify_passed_after_last_patch` → state machine (verify_done blocked by state)
- `guard3_consecutive_violations` → removed (POSTPATCH_CLEAN allows verify_done by design)
- `last_blocking_check_passed` → state machine (POSTPATCH_CLEAN / POSTPATCH_BLOCKING)
- `_seen_calls: dict[str, int]` → replaced by emit_patch-only dedup via state machine

And `phase: str` becomes a derived variable: `"explore" if sm.state == S.EXPLORE else "verify"`.

- [ ] **Step 1: Add import at top of `loop.py`**

Below the existing imports, add:

```python
from agentd.tools.verify_phase_sm import (
    VerifyPhaseEvent,
    VerifyPhaseExhausted,
    VerifyPhaseState,
    VerifyPhaseStateMachine,
)
```

- [ ] **Step 2: Add `_build_patch_key` helper after the imports**

```python
def _build_patch_key(patch_ops: list[object]) -> tuple:
    """Stable hashable key for a list of patch ops — used for emit_patch dedup."""
    return tuple(
        json.dumps(op, sort_keys=True, default=str)
        for op in patch_ops
        if isinstance(op, dict)
    )
```

- [ ] **Step 3: Replace the five flags with the state machine at the top of `run()`**

Find this block (lines ~135–145):

```python
phase = "explore"
explore_calls = 0
verify_calls = 0
last_patch_document: dict[str, object] = {}
all_touched_files: list[str] = []
last_verify_run_errored: bool = False  # True if last verify-phase run_command failed
verify_passed_after_last_patch: bool = False  # True if any run_command passed since last emit_patch
guard3_consecutive_violations: int = 0  # consecutive verify_done(True) without a passing run_command
last_blocking_check_passed: bool = False  # True when last auto-check had no blocking (py_compile/mypy) failures
had_scope_violation: bool = False     # True if any patch was rejected for out-of-scope file
_seen_calls: dict[str, int] = {}  # dedup: (tool, canonical_args) → first iteration seen
```

Replace with:

```python
sm = VerifyPhaseStateMachine()
explore_calls = 0
verify_calls = 0
last_patch_document: dict[str, object] = {}
all_touched_files: list[str] = []
had_scope_violation: bool = False
```

- [ ] **Step 4: Update the tool definitions line at the top of the loop**

Find:

```python
tool_defs = [t.model_dump() for t in self._registry.definitions(phase=phase)]
```

Replace with:

```python
phase = "explore" if sm.state == VerifyPhaseState.EXPLORE else "verify"
_all_defs = self._registry.definitions(phase=phase)
_allowed = sm.allowed_tools()
tool_defs = [t.model_dump() for t in _all_defs if t.name in _allowed]
```

- [ ] **Step 5: Simplify the `verify_done` handler**

Find the entire `if action_type == "verify_done":` block (lines ~228–332). Replace it with:

```python
if action_type == "verify_done":
    verified_flag = bool(response.get("verified", False))
    logger.info(
        "[loop] verify_done: task=%s step=%s state=%s verified=%s",
        self._task_id, step.id, sm.state.value, verified_flag,
    )
    # State machine enforces when verify_done is valid.
    # It's only in the schema for POSTPATCH_CLEAN and TEST_PASSED.
    # Guard against crafted calls from other states.
    if sm.state not in (VerifyPhaseState.POSTPATCH_CLEAN, VerifyPhaseState.TEST_PASSED):
        logger.warning(
            "verify_done called from invalid state %s (step %s)",
            sm.state.value, step.id, extra={"task_id": self._task_id},
        )
        history.append({"role": "assistant", "content": json.dumps(response, default=str)})
        history.append({
            "role": "tool_result", "tool": "_verify_guard",
            "content": (
                f"verify_done is not valid in the current state.\n"
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
```

- [ ] **Step 6: Replace the dedup guard at the top of `tool_call` handling**

The current dedup block (inside `if action_type == "tool_call":`) is:

```python
_dedup_args = dict(args_raw)
if tool_name_raw == "search_code":
    _dedup_args.pop("context_lines", None)
_call_key = f"{tool_name_raw}:{json.dumps(_dedup_args, sort_keys=True, default=str)}"
if _call_key in _seen_calls:
    _first_iter = _seen_calls[_call_key]
    _dedup_msg = (...)
    ...
    continue
_seen_calls[_call_key] = iteration + 1
```

Remove this entire block. Reads are no longer deduped. (emit_patch dedup is handled separately in Step 7.)

- [ ] **Step 7: Replace the emit_patch success/failure paths**

Find the successful patch block (around `# Patch succeeded`):

```python
# Always enter verify phase — execution agent decides what to run
logger.info(...)
phase = "verify"
self._registry.use_shadow_for_reads()
last_verify_run_errored = False
verify_passed_after_last_patch = False
last_blocking_check_passed = False
guard3_consecutive_violations = 0
# After a patch the shadow state changes...
_seen_calls = {}
touched_files_str = ...
testing_strategy = ...
test_cmd_hint = ...
_shadow_root = ...
auto_checks, _blocking_clean = (
    await _POST_PATCH_ANALYZER.analyze(...)
    if _shadow_root is not None
    else ("", True)
)
last_blocking_check_passed = _blocking_clean
history.append({
    "role": "tool_result", "tool": "_patch_apply",
    "content": (
        "Patch applied successfully. Entering VERIFY PHASE.\n"
        f"Touched files: {touched_files_str}\n"
        f"testing_strategy: {testing_strategy}\n"
        f"test_command hint: {test_cmd_hint}\n"
        "Run tests. Emit verify_done when all pass, "
        "or emit_patch again to correct failures."
        + auto_checks
    ),
})
```

Replace with:

```python
# Patch succeeded — switch to shadow reads and fire postpatch event.
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
        f"Patch applied. {sm.state_description(error_summary=auto_checks.strip())}\n"
        f"Touched files: {touched_files_str}\n"
        f"testing_strategy: {testing_strategy}\n"
        f"test_command hint: {test_cmd_hint}\n"
        + auto_checks
    ),
})
```

And for the patch failure path, find the part after `is_scope_error` handling where non-scope errors push back. The end of the else branch that has the "not found" / "ambiguous" feedback messages should add:

```python
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
            f"Step {step.id!r}: emit_patch failed {MAX_PATCH_RETRIES} consecutive times "
            "in PATCH_FAILED_CAN_RETRY — giving up on this step attempt."
        ),
        tool_trace=trace,
    )
```

Add this block right before `continue` in the non-scope patch failure branch. Also add the emit_patch dedup check near the start of the `if action_type == "emit_patch":` block, before calling `_apply_patch_inline`:

```python
# emit_patch dedup: block exact repeat within same state stay.
patch_ops = response.get("patch_ops")
if not isinstance(patch_ops, list):
    raise ToolBudgetExceededError(
        f"Step {step.id!r}: emit_patch has non-list 'patch_ops' at iteration {iteration}"
    )
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
            "DUPLICATE PATCH BLOCKED: you already attempted this exact patch in the current state. "
            "Read the file first with read_file to get the current content, then emit a corrected patch.\n"
            f"{sm.state_description()}"
        ),
    })
    continue
sm.record_patch_attempt(patch_key)
```

This replaces the old `patch_ops = response.get("patch_ops")` / non-list check at the start of the `emit_patch` branch.

- [ ] **Step 8: Add READ_CALLED dispatch after read tool executes**

Find the section after `tool_output = await self._registry.execute(tool_name, args)` (around line 647). After `usage.tool_calls_used += 1`, add:

```python
# Dispatch READ_CALLED only from PATCH_FAILED_MUST_READ — unlocks emit_patch.
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
```

- [ ] **Step 9: Add TEST_PASSED / TEST_FAILED dispatch after run_command**

Find the existing block that tracks run_command outcomes (around line 687):

```python
# Track verify-phase run_command outcomes for Guard 2 and Guard 3.
if phase == "verify" and tool_name == "run_command":
    last_verify_run_errored = tool_output.is_error
    guard3_consecutive_violations = 0
    if not tool_output.is_error:
        verify_passed_after_last_patch = True

# A successful setup_env or init_workspace clears the previous
# "binary not found" failure — the binary is now expected to exist.
if (
    tool_name in ("setup_env", "init_workspace")
    and not tool_output.is_error
):
    last_verify_run_errored = False
```

Replace with:

```python
# Fire test event on run_command result.
if tool_name == "run_command" and sm.state in (
    VerifyPhaseState.POSTPATCH_CLEAN, VerifyPhaseState.TEST_FAILED
):
    test_event = VerifyPhaseEvent.TEST_PASSED if not tool_output.is_error else VerifyPhaseEvent.TEST_FAILED
    sm.transition(test_event)
    logger.info(
        "[loop] run_command result: task=%s step=%s is_error=%s state→%s",
        self._task_id, step.id, tool_output.is_error, sm.state.value,
    )
```

- [ ] **Step 10: Update budget enforcement to use derived phase**

The budget block references `phase` which is now derived at the top of each iteration. Verify that the `if phase == "explore":` budget check (around line 580) uses the same `phase` variable (it will, since `phase` is set at the top of each iteration in Step 4). No code change needed here — just confirm the variable is in scope.

- [ ] **Step 11: Import MAX_PATCH_RETRIES in loop.py for the exhaustion error message**

The `from agentd.tools.verify_phase_sm import` line added in Step 1 already imports what's needed. Add `MAX_PATCH_RETRIES` to that import:

```python
from agentd.tools.verify_phase_sm import (
    MAX_PATCH_RETRIES,
    VerifyPhaseEvent,
    VerifyPhaseExhausted,
    VerifyPhaseState,
    VerifyPhaseStateMachine,
)
```

- [ ] **Step 12: Run existing tool loop tests**

```bash
cd services/agentd-py && source .venv/bin/activate
pytest tests/test_tool_loop_skip_verify.py tests/test_tool_loop_event_format.py tests/test_tool_loop_scope_gate.py -v
```

Expected: all pass. If any fail, fix `loop.py` before continuing.

- [ ] **Step 13: Commit**

```bash
git add services/agentd-py/agentd/tools/loop.py
git commit -m "feat(loop): replace 5 verify-phase flags with VerifyPhaseStateMachine"
```

---

## Task 4: Reform `tool_prompts.py`

**Files:**
- Modify: `services/agentd-py/agentd/reasoning/tool_prompts.py`

The goal is to:
1. Remove the static "EXECUTION PHASES" section from `TOOL_LOOP_SYSTEM_PROMPT` (it describes verify phase behaviour that is now dynamic).
2. Add a `{verify_state}` placeholder in the system prompt where the state description will be injected.
3. Update `build_tool_step_payload()` to accept and inject `state_description: str`.
4. Update `format_tool_system_prompt()` to accept `state_description` and pass it through.

- [ ] **Step 1: Remove the static verify-phase section from `TOOL_LOOP_SYSTEM_PROMPT`**

Find and delete the `EXECUTION PHASES:` block (lines ~198–260), which starts with:

```
EXECUTION PHASES:

Phase 1 — EXPLORE & PATCH
...
Phase 2 — VERIFY
  Phase 2 begins when you see "Patch applied successfully" in the conversation.
  ...
  HARD RULES — verify_done(verified=true) requires ALL of:
  ...
  NEVER run the full test suite...
  TIMEOUTS count as failure:
```

Replace the entire block with this shorter version:

```
EXECUTION PHASES:

The instruction field of each request tells you your CURRENT VERIFY STATE and what you
should do next. Follow it precisely — available tools change per state and the schema
enforces this. Trust the instruction over any general heuristic.

Phase 1 (EXPLORE): locate code and emit your first patch. Reads go to the real workspace.
Phase 2 (VERIFY): entered automatically after your patch is applied. Reads switch to shadow
(your patched files). The instruction field tells you your exact state each turn.
```

- [ ] **Step 2: Also remove the "READ/SEARCH BEHAVIOR BY PHASE" verify-half**

Find the `READ/SEARCH BEHAVIOR BY PHASE — CRITICAL:` section. Keep the Phase 1 paragraph. Remove the Phase 2 paragraph starting with `Phase 2 (VERIFY, after first patch applied):`. Replace it with one line:

```
Phase 2 (VERIFY): read_file, search_code, list_directory automatically read the SHADOW
workspace (your patched files). The instruction field tells you what state you are in.
```

Also remove the two sub-sections:
- `  If a patch fails in verify phase — DO NOT re-emit immediately. Diagnose first:` (and all its bullets)

These are now covered by `PATCH_FAILED_MUST_READ` / `PATCH_FAILED_CAN_RETRY` state descriptions.

- [ ] **Step 3: Update `build_tool_step_payload()` — remove dead verify block, add `state_description`**

The `if phase == "verify":` block in this function is currently dead code: `engine.py` always calls
`build_tool_step_payload(step_context, history)` with no `phase` arg, so verify guidance never fired
from here. Remove the dead block, drop the `phase` parameter, and add `state_description`.

Change the signature from:
```python
def build_tool_step_payload(
    step_context: dict[str, object],
    history: list[dict[str, object]],
    *,
    phase: str = "explore",
) -> dict[str, object]:
```

To:
```python
def build_tool_step_payload(
    step_context: dict[str, object],
    history: list[dict[str, object]],
    *,
    state_description: str = "",
) -> dict[str, object]:
```

Inside `if history:`, remove the entire `if phase == "verify": ... else:` structure and replace it with a single block that starts from `state_description` when provided, then falls through to the explore budget hints:

```python
if history:
    payload["conversation_history"] = history
    iteration = len(history) // 2
    recent = [str(m.get("content", "")) for m in history[-6:]]
    patch_fail_count = sum(
        1 for m in recent if "search text not found" in m or "not found in" in m
    )

    if state_description:
        base = state_description
    elif patch_fail_count >= 2:
        base = (
            f"⚠ search_replace has failed {patch_fail_count} times. "
            "STOP using search_replace for these locations — switch ops NOW:\n"
            "  • apply_diff: call read_file on the target lines first, then emit apply_diff "
            "with a unified diff using those exact lines as context.\n"
            "  • create_file: if apply_diff is also failing or >30% of the file changes — "
            "read_file the full file (start_line=1 to end), then overwrite with create_file.\n"
            "Do NOT emit another search_replace with the same or similar search string."
        )
    elif patch_fail_count >= 1:
        base = (
            "⚠ Last patch failed: 'search text not found'. "
            "Your search string does not exactly match the file. "
            "Call read_file on the specific line range you want to change — "
            "use only the text returned by that read as your search field. "
            "If read_file confirms the text still doesn't match, switch to apply_diff or create_file "
            "(see WHEN search_replace FAILS in the system prompt)."
        )
    elif iteration >= 12:
        base = (
            f"⚠ BUDGET: {iteration} tool calls used. You MUST emit_patch NOW. "
            "Do NOT call any more tools. You have enough context — commit to the patch immediately. "
            "Write your search_replace ops from what you have already read."
        )
    elif iteration >= 6:
        base = (
            f"Tool calls used: {iteration}. Stop exploring. "
            "Only call one more tool if a specific line range is still unknown. "
            "Otherwise emit_patch now. Output your NEXT action."
        )
    else:
        base = (
            "Continue. Pattern: search_code to locate a symbol (use context_lines=10) → "
            "read_file with start_line/end_line from the search result → "
            "search_code again for the next unknown symbol → read_file chunk → emit_patch. "
            "Every search hit with a line number REQUIRES an immediate read_file at that line. "
            "Do NOT search again before reading the result you just found. "
            "Output your NEXT action as a JSON object."
        )
    payload["instruction"] = base
```

The `else:` (no history / first turn) branch stays unchanged.

- [ ] **Step 4: Update `format_tool_system_prompt()` — no change needed**

This function only formats the static `TOOL_LOOP_SYSTEM_PROMPT`. `state_description` goes into the
user payload, not the system prompt. No changes needed here.

- [ ] **Step 5: Update `reasoning/engine.py` — thread `state_description` through `create_tool_step`**

`create_tool_step` is defined at line 124 of `reasoning/engine.py`. The call to
`build_tool_step_payload` is at line 136. Update both:

```python
async def create_tool_step(
    self,
    step_context: dict[str, object],
    history: list[dict[str, object]],
    tool_definitions: list[dict[str, object]],
    on_thinking: Callable[[str], None] | None = None,
    state_description: str = "",   # ← add
) -> dict[str, object]:
    from agentd.reasoning.tool_prompts import (
        AGENT_STEP_RESPONSE_SCHEMA,
        build_tool_step_payload,
        format_tool_system_prompt,
    )
    user_payload = build_tool_step_payload(
        step_context, history, state_description=state_description   # ← pass
    )
    system_instructions = format_tool_system_prompt(tool_definitions)
    result = await self._transport.generate_json(
        model=self._model,
        schema_name="agent_step_response",
        schema=AGENT_STEP_RESPONSE_SCHEMA,
        system_instructions=system_instructions,
        user_payload=user_payload,
        on_thinking=on_thinking,
    )
    return result
```

Also update the protocol at `reasoning/contracts.py` line 33:

```python
async def create_tool_step(
    self,
    step_context: dict[str, object],
    history: list[dict[str, object]],
    tool_definitions: list[dict[str, object]],
    on_thinking: Callable[[str], None] | None = None,
    state_description: str = "",   # ← add
) -> dict[str, object]:
    ...
```

- [ ] **Step 6: Pass `state_description` from `loop.py` to `create_tool_step`**

Add two tracking variables in `run()` right after `sm = VerifyPhaseStateMachine()`:

```python
_last_auto_checks_error: str = ""
_last_test_failure: str = ""
```

Change the `create_tool_step` call from:

```python
response = await self._reasoning.create_tool_step(
    step_context=step_context,
    history=history,
    tool_definitions=tool_defs,
    on_thinking=_on_thinking,
)
```

To:

```python
response = await self._reasoning.create_tool_step(
    step_context=step_context,
    history=history,
    tool_definitions=tool_defs,
    on_thinking=_on_thinking,
    state_description=sm.state_description(
        error_summary=_last_auto_checks_error,
        failure_summary=_last_test_failure,
    ),
)
```

In the patch success path, after `auto_checks, _blocking_clean = ...`, set:

```python
_last_auto_checks_error = auto_checks.strip() if not _blocking_clean else ""
_last_test_failure = ""
```

In the `run_command` tracking block (after `sm.transition(test_event)`), set:

```python
if tool_output.is_error:
    _last_test_failure = tool_output.output[:300]
else:
    _last_test_failure = ""
```

- [ ] **Step 8: Run the full test suite**

```bash
cd services/agentd-py && source .venv/bin/activate
pytest tests/ -x -q
```

Expected: all pass. If a test fails due to the `create_tool_step` signature change, update the stub reasoning engines in affected test files to accept `state_description=None` as a keyword argument (they can ignore it).

- [ ] **Step 9: Commit**

```bash
git add services/agentd-py/agentd/reasoning/tool_prompts.py \
        services/agentd-py/agentd/reasoning/engine.py \
        services/agentd-py/agentd/reasoning/contracts.py \
        services/agentd-py/agentd/tools/loop.py
git commit -m "feat(tool-prompts): inject per-turn state_description; remove static verify-phase guidance"
```

---

## Task 5: Integration test — full verify path

**Files:**
- Modify: `services/agentd-py/tests/test_orchestrator_verify_flow.py`

Add tests that drive the full state machine path through the ToolLoop using scripted reasoning engines.

- [ ] **Step 1: Add integration tests**

Append to `tests/test_orchestrator_verify_flow.py`:

```python
# ── State machine integration via ToolLoop ────────────────────────────────────

class _PatchThenTestEngine:
    """Scripted: emit_patch → (postpatch handled by loop) → run_command (pass) → verify_done."""
    def __init__(self, *, test_cmd="pytest tests/test_foo.py -x -q"):
        self._test_cmd = test_cmd
        self._turn = 0

    async def create_tool_step(self, step_context, history, tool_definitions, on_thinking=None, state_description=""):
        tool_names = {t["name"] for t in tool_definitions}
        self._turn += 1
        if self._turn == 1:
            return {
                "type": "emit_patch",
                "thought": "patch",
                "patch_ops": [{"op": "search_replace", "file": "a.py", "search": "x = 1", "replace": "x = 2", "reason": "r"}],
            }
        if "run_command" in tool_names and self._turn == 2:
            return {"type": "tool_call", "thought": "test", "tool": "run_command", "args": {"command": "pytest", "args": ["tests/test_foo.py", "-x", "-q"]}}
        return {"type": "verify_done", "thought": "done", "verified": True, "test_output": "1 passed"}

    async def create_patch(self, *a, **kw): return {}
    async def create_planning_step(self, *a, **kw): return {}
    async def create_plan(self, *a, **kw): return {}


@pytest.mark.asyncio
async def test_state_machine_verify_done_allowed_in_postpatch_clean_without_test(tmp_path):
    """POSTPATCH_CLEAN allows verify_done directly (no-test step)."""
    from agentd.tools.loop import ToolLoop
    from agentd.patch.engine import PatchEngine
    from agentd.tools.registry import ToolRegistry
    from agentd.orchestrator.broadcaster import EventBroadcaster

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("x = 1\n")

    class _PatchThenDoneEngine:
        _turn = 0
        async def create_tool_step(self, step_context, history, tool_definitions, on_thinking=None, state_description=""):
            self._turn += 1
            if self._turn == 1:
                return {
                    "type": "emit_patch",
                    "thought": "patch",
                    "patch_ops": [{"op": "search_replace", "file": "a.py", "search": "x = 1", "replace": "x = 2", "reason": "r"}],
                }
            # verify_done should be available in POSTPATCH_CLEAN even without run_command
            return {"type": "verify_done", "thought": "no tests needed", "verified": True, "test_output": "no tests required"}
        async def create_patch(self, *a, **kw): return {}
        async def create_planning_step(self, *a, **kw): return {}
        async def create_plan(self, *a, **kw): return {}

    broadcaster = EventBroadcaster()
    registry = ToolRegistry(shadow_root=ws, real_workspace_path=ws)
    loop = ToolLoop(
        _PatchThenDoneEngine(), registry, broadcaster, "task-sm1",
        patch_engine=PatchEngine(), shadow_path=ws,
    )
    step = PlanStep(
        id="s1", goal="g",
        targets=[PlanTarget(path="a.py", intent=PlanTargetIntent.EXISTING)],
        risk="low",
    )
    result = await loop.run(step, {}, TaskBudget(), TaskUsage())
    assert isinstance(result, VerifyResult)
    assert result.verified is True


@pytest.mark.asyncio
async def test_state_machine_patch_retry_exhaustion_returns_verify_result(tmp_path):
    """Exhausting MAX_PATCH_RETRIES returns VerifyResult(verified=False)."""
    from agentd.tools.loop import ToolLoop
    from agentd.patch.engine import PatchEngine
    from agentd.tools.registry import ToolRegistry
    from agentd.orchestrator.broadcaster import EventBroadcaster
    from agentd.tools.verify_phase_sm import MAX_PATCH_RETRIES

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("x = 1\n")

    calls = {"n": 0}

    class _AlwaysFailPatchEngine:
        async def create_tool_step(self, step_context, history, tool_definitions, on_thinking=None, state_description=""):
            calls["n"] += 1
            tool_names = {t["name"] for t in tool_definitions}
            if "emit_patch" in tool_names:
                # Emit a patch with a bad search string that will never match
                return {
                    "type": "emit_patch",
                    "thought": "try again",
                    "patch_ops": [{"op": "search_replace", "file": "a.py", "search": "DOES_NOT_EXIST", "replace": "y = 2", "reason": "r"}],
                }
            # In PATCH_FAILED_MUST_READ: call read_file to unlock emit_patch
            return {"type": "tool_call", "thought": "read", "tool": "read_file", "args": {"path": "a.py"}}
        async def create_patch(self, *a, **kw): return {}
        async def create_planning_step(self, *a, **kw): return {}
        async def create_plan(self, *a, **kw): return {}

    broadcaster = EventBroadcaster()
    registry = ToolRegistry(shadow_root=ws, real_workspace_path=ws)
    loop = ToolLoop(
        _AlwaysFailPatchEngine(), registry, broadcaster, "task-sm2",
        patch_engine=PatchEngine(), shadow_path=ws,
    )
    step = PlanStep(
        id="s1", goal="g",
        targets=[PlanTarget(path="a.py", intent=PlanTargetIntent.EXISTING)],
        risk="low",
    )
    result = await loop.run(step, {}, TaskBudget(), TaskUsage())
    assert isinstance(result, VerifyResult)
    assert result.verified is False
    assert "exhausted" in result.test_output.lower() or "consecutive" in result.test_output.lower()
```

- [ ] **Step 2: Run the integration tests**

```bash
cd services/agentd-py && source .venv/bin/activate
pytest tests/test_orchestrator_verify_flow.py -v
```

Expected: all pass.

- [ ] **Step 3: Run the full suite one final time**

```bash
pytest tests/ -x -q
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add services/agentd-py/tests/test_orchestrator_verify_flow.py
git commit -m "test(verify-phase-sm): integration tests for no-test path and retry exhaustion"
```
