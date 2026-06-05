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
    # Defensive: emit_patch is not in the MUST_READ schema, but a model bypassing
    # the schema can still emit one. If it fails, stay in MUST_READ (no retry
    # counter increment — the read precondition isn't satisfied yet).
    (_S.PATCH_FAILED_MUST_READ, _E.PATCH_FAILED):       _S.PATCH_FAILED_MUST_READ,

    # PATCH_FAILED from CAN_RETRY handled inline below (counter check).
    (_S.PATCH_FAILED_CAN_RETRY, _E.POSTPATCH_BLOCKING): _S.POSTPATCH_BLOCKING,
    (_S.PATCH_FAILED_CAN_RETRY, _E.POSTPATCH_CLEAN):    _S.POSTPATCH_CLEAN,

    (_S.POSTPATCH_BLOCKING,     _E.PATCH_FAILED):       _S.PATCH_FAILED_MUST_READ,
    (_S.POSTPATCH_BLOCKING,     _E.POSTPATCH_BLOCKING): _S.POSTPATCH_BLOCKING,
    (_S.POSTPATCH_BLOCKING,     _E.POSTPATCH_CLEAN):    _S.POSTPATCH_CLEAN,

    (_S.POSTPATCH_CLEAN,        _E.TEST_FAILED):        _S.TEST_FAILED,
    (_S.POSTPATCH_CLEAN,        _E.TEST_PASSED):        _S.TEST_PASSED,
    # Defensive: emit_patch is not in the POSTPATCH_CLEAN schema, but a model bypassing
    # the schema (or a scripted test) may still emit one. Cover all three outcomes so
    # the SM never blows up the loop on bypass.
    (_S.POSTPATCH_CLEAN,        _E.PATCH_FAILED):       _S.PATCH_FAILED_MUST_READ,
    (_S.POSTPATCH_CLEAN,        _E.POSTPATCH_CLEAN):    _S.POSTPATCH_CLEAN,
    (_S.POSTPATCH_CLEAN,        _E.POSTPATCH_BLOCKING): _S.POSTPATCH_BLOCKING,

    (_S.TEST_FAILED,            _E.PATCH_FAILED):       _S.PATCH_FAILED_MUST_READ,
    (_S.TEST_FAILED,            _E.POSTPATCH_BLOCKING): _S.POSTPATCH_BLOCKING,
    (_S.TEST_FAILED,            _E.POSTPATCH_CLEAN):    _S.POSTPATCH_CLEAN,
    (_S.TEST_FAILED,            _E.TEST_FAILED):        _S.TEST_FAILED,
    (_S.TEST_FAILED,            _E.TEST_PASSED):        _S.TEST_PASSED,

    # Defensive (same rationale as POSTPATCH_CLEAN + PATCH_FAILED above).
    # All three patch-outcome events covered so successful schema-bypass patches
    # don't crash the loop.
    (_S.TEST_PASSED,            _E.PATCH_FAILED):       _S.PATCH_FAILED_MUST_READ,
    (_S.TEST_PASSED,            _E.POSTPATCH_CLEAN):    _S.POSTPATCH_CLEAN,
    (_S.TEST_PASSED,            _E.POSTPATCH_BLOCKING): _S.POSTPATCH_BLOCKING,
}

_ALLOWED_TOOLS: dict[_S, frozenset[str]] = {
    _S.EXPLORE:                frozenset({"read_file", "search_code", "list_directory",
                                           "search_semantic", "read_env_profile", "query_graph", "emit_patch"}),
    _S.PATCH_FAILED_MUST_READ: frozenset({"read_file", "search_code", "list_directory",
                                           "search_semantic", "read_env_profile", "query_graph"}),
    _S.PATCH_FAILED_CAN_RETRY: frozenset({"read_file", "search_code", "list_directory",
                                           "search_semantic", "read_env_profile", "query_graph", "emit_patch"}),
    _S.POSTPATCH_BLOCKING:     frozenset({"read_file", "search_code", "list_directory",
                                           "search_semantic", "read_env_profile", "query_graph", "emit_patch",
                                           "find_binary", "setup_env", "init_workspace"}),
    _S.POSTPATCH_CLEAN:        frozenset({"read_file", "search_code", "list_directory",
                                           "search_semantic", "read_env_profile", "query_graph",
                                           "run_command", "verify_done",
                                           "find_binary", "setup_env", "init_workspace"}),
    _S.TEST_FAILED:            frozenset({"read_file", "search_code", "list_directory",
                                           "search_semantic", "read_env_profile", "query_graph", "emit_patch",
                                           "run_command",
                                           "find_binary", "setup_env", "init_workspace"}),
    _S.TEST_PASSED:            frozenset({"read_file", "search_code", "list_directory",
                                           "search_semantic", "read_env_profile", "query_graph", "verify_done"}),
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

    def allowed_action_types(self) -> frozenset[str]:
        """Top-level response 'type' values allowed in the current state.

        Derived from allowed_tools: emit_patch and verify_done membership in the
        allowed-tools table mirrors their availability as response action types.
        tool_call and revision_needed are always allowed — the former because
        any tool execution flows through it, the latter as an escape hatch
        when the plan is wrong (model needs to be able to signal that anywhere).

        Used to filter AGENT_STEP_RESPONSE_SCHEMA.type.enum per turn so a model
        cannot bypass state gating by crafting an off-schema action type.
        """
        allowed = _ALLOWED_TOOLS[self.state]
        types: set[str] = {"tool_call", "revision_needed"}
        if "emit_patch" in allowed:
            types.add("emit_patch")
        if "verify_done" in allowed:
            types.add("verify_done")
        return frozenset(types)

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
        budget_note: str = "",
    ) -> str:
        """Contextual prompt injected into the model's instruction field each turn."""
        s = self.state
        rc, mx = self._retry_count, MAX_PATCH_RETRIES
        iter_note = f" [iteration {iteration}]" if iteration else ""
        # Surface the live phase budget (e.g. "explore: 12/50 calls used") so the
        # model paces itself instead of guessing how much room it has.
        if budget_note:
            iter_note += f" [{budget_note}]"

        if s == _S.EXPLORE:
            return (
                f"CURRENT STATE: EXPLORE{iter_note}\n"
                "No patch has been applied yet.\n"
                "FIRST, reflect on what you already know: which target files, symbols, and the\n"
                "concrete edit can you name, and what — if anything — material to the change is\n"
                "still genuinely unknown?\n"
                "THEN choose ONE, based only on your reflection:\n"
                "  (A) READ MORE — if something material is unknown, search/read the specific\n"
                "      region you need (read only files you have not already seen). When your\n"
                "      change alters a symbol other code depends on, call\n"
                "      query_graph(node=\"<file>:Symbol\") to find its callers/implementers and\n"
                "      read those connected files so your patch covers them.\n"
                "  (B) EMIT THE PATCH — if you can already name the target files and the edit,\n"
                "      emit_patch now (include a patch_op for every file in targets).\n"
                "Neither option is penalized and neither is forced — pick what your reflection\n"
                "supports.\n"
                "Available tools: read_file, search_code, search_semantic, list_directory, "
                "query_graph, emit_patch"
            )

        if s == _S.PATCH_FAILED_MUST_READ:
            summary = (
                f"\nCompiler/static errors still active:\n{error_summary}\n"
                if error_summary
                else ""
            )
            return (
                f"CURRENT STATE: PATCH_FAILED{iter_note}\n"
                f"{summary}"
                "Last patch failed — the file may not match what the patch expected.\n"
                "You MUST search for and read the code around the error symbols or line numbers\n"
                "first (emit_patch is locked).\n"
                "Follow this approach:\n"
                "  1. Identify the files, lines, or symbols involved in the failure or active\n"
                "     compiler errors.\n"
                "  2. Use search_code to locate the error symbols or lines if you are unsure\n"
                "     of their exact location.\n"
                "  3. Call read_file with start_line and end_line parameters to read a window\n"
                "     (e.g. 100-200 lines) around the error location.\n"
                "  4. DO NOT do a whole-file read (capped at 500 lines and will truncate).\n"
                "     You MUST read targeted sections.\n"
                "  5. Keep reading/searching recursively until you have complete and correct\n"
                "     context around the lines to patch.\n"
                "emit_patch will unlock automatically after you perform a successful\n"
                "read_file or search_code.\n"
                "Available tools: read_file, search_code, search_semantic, list_directory, query_graph"
            )

        if s == _S.PATCH_FAILED_CAN_RETRY:
            return (
                f"CURRENT STATE: PATCH_FAILED — RETRY {rc} of {mx}{iter_note}\n"
                "You've read the file. emit_patch is available. "
                "Use what you observed to decide your next move — patch if needed, "
                "read more if unsure, or switch op type if the current one keeps failing.\n"
                "If you are still unsure of the error context, continue searching code or reading\n"
                "targeted line ranges. Keep reading recursively until you have complete and\n"
                "correct context before retrying emit_patch.\n"
                f"Retry counter ({rc}/{mx}) only increments on actual engine failures.\n"
                "Available tools: read_file, search_code, search_semantic, list_directory, "
                "query_graph, emit_patch"
            )

        if s == _S.POSTPATCH_BLOCKING:
            summary = f"\n{error_summary}\n" if error_summary else ""
            return (
                f"CURRENT STATE: POSTPATCH — BLOCKING ERRORS{iter_note}{summary}\n"
                "Patch applied, but static analysis found errors that need resolving "
                "before tests can run. run_command is locked for now.\n"
                "Available tools: read_file, search_code, search_semantic, list_directory, "
                "query_graph, emit_patch"
            )

        if s == _S.POSTPATCH_CLEAN:
            return (
                f"CURRENT STATE: POSTPATCH — CLEAN{iter_note}\n"
                "Static checks passed. Decide your next move: run tests, read more, or verify_done.\n"
                "If THIS step created or modified a test file (or the code those tests cover), you "
                "MUST run the relevant tests with run_command and see them PASS before calling "
                "verify_done. Do NOT use verify_done to skip running tests you just wrote — passing "
                "static checks (py_compile/ruff/mypy) is NOT the same as the tests passing.\n"
                "Only call verify_done WITHOUT running tests when there is genuinely nothing to run "
                "at this step (e.g. the test file is created by a LATER step, or no test exists "
                "yet) — and never try to run a test file that does not exist.\n"
                "Available tools: read_file, search_code, list_directory, run_command, verify_done"
            )

        if s == _S.TEST_FAILED:
            summary = f"\n{failure_summary}\n" if failure_summary else ""
            return (
                f"CURRENT STATE: TEST_FAILED{iter_note}{summary}\n"
                "Tests failed. Read the output, patch if needed, "
                "or re-run a narrower command to narrow down the issue.\n"
                "Available tools: read_file, search_code, search_semantic, list_directory, "
                "query_graph, emit_patch, run_command"
            )

        if s == _S.TEST_PASSED:
            return (
                f"CURRENT STATE: TEST_PASSED{iter_note}\n"
                "Tests passed. Call verify_done when ready.\n"
                "Available tools: read_file, verify_done"
            )

        return f"CURRENT STATE: {s.value}"
