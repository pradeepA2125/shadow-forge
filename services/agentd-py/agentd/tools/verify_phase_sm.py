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
        iteration: int = 0,
        error_summary: str = "",
        failure_summary: str = "",
    ) -> str:
        """Contextual prompt injected into the model's instruction field each turn."""
        s = self.state
        rc, mx = self._retry_count, MAX_PATCH_RETRIES
        iter_note = f" [iteration {iteration}]" if iteration else ""

        if s == _S.EXPLORE:
            return (
                f"CURRENT STATE: EXPLORE{iter_note}\n"
                "No patch has been applied yet. Read the relevant files, search for symbols, "
                "and understand the code structure. When you have enough context, emit your patch.\n"
                "Available tools: read_file, search_code, list_directory, emit_patch"
            )

        if s == _S.PATCH_FAILED_MUST_READ:
            return (
                f"CURRENT STATE: PATCH_FAILED{iter_note}\n"
                "Last patch failed — the file may not match what the patch expected. "
                "Reading the file gives you ground truth before deciding what to do next. "
                "emit_patch is locked until you read; it unlocks automatically after.\n"
                "Available tools: read_file, search_code, list_directory"
            )

        if s == _S.PATCH_FAILED_CAN_RETRY:
            return (
                f"CURRENT STATE: PATCH_FAILED — RETRY {rc} of {mx}{iter_note}\n"
                "You've read the file. emit_patch is available. "
                "Use what you observed to decide your next move — patch if needed, "
                "read more if unsure, or switch op type if the current one keeps failing. "
                f"Retry counter ({rc}/{mx}) only increments on actual engine failures.\n"
                "Available tools: read_file, search_code, list_directory, emit_patch"
            )

        if s == _S.POSTPATCH_BLOCKING:
            summary = f"\n{error_summary}\n" if error_summary else ""
            return (
                f"CURRENT STATE: POSTPATCH — BLOCKING ERRORS{iter_note}{summary}\n"
                "Patch applied, but static analysis found errors that need resolving "
                "before tests can run. run_command is locked for now.\n"
                "Available tools: read_file, search_code, list_directory, emit_patch"
            )

        if s == _S.POSTPATCH_CLEAN:
            return (
                f"CURRENT STATE: POSTPATCH — CLEAN{iter_note}\n"
                "Static checks passed. You can run tests, read more, "
                "or call verify_done if the step is complete.\n"
                "Available tools: read_file, search_code, list_directory, run_command, verify_done"
            )

        if s == _S.TEST_FAILED:
            summary = f"\n{failure_summary}\n" if failure_summary else ""
            return (
                f"CURRENT STATE: TEST_FAILED{iter_note}{summary}\n"
                "Tests failed. Read the output, patch if needed, "
                "or re-run a narrower command to narrow down the issue.\n"
                "Available tools: read_file, search_code, list_directory, emit_patch, run_command"
            )

        if s == _S.TEST_PASSED:
            return (
                f"CURRENT STATE: TEST_PASSED{iter_note}\n"
                "Tests passed. Call verify_done when ready.\n"
                "Available tools: read_file, verify_done"
            )

        return f"CURRENT STATE: {s.value}"
