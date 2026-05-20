"""Unit tests for VerifyPhaseStateMachine."""
from __future__ import annotations

import pytest

from agentd.tools.verify_phase_sm import (
    InvalidVerifyPhaseTransition,
    MAX_PATCH_RETRIES,
    VerifyPhaseEvent as E,
    VerifyPhaseExhausted,
    VerifyPhaseState as S,
    VerifyPhaseStateMachine,
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
    # EXPLORE → MUST_READ (rc=0)
    sm.transition(E.PATCH_FAILED)
    # Cycle MUST_READ → CAN_RETRY → MUST_READ, incrementing rc each time.
    for _ in range(MAX_PATCH_RETRIES - 1):
        sm.transition(E.READ_CALLED)     # → CAN_RETRY
        sm.transition(E.PATCH_FAILED)    # → MUST_READ, rc += 1
    # rc == MAX - 1, state == MUST_READ
    sm.transition(E.READ_CALLED)         # → CAN_RETRY (rc unchanged)
    with pytest.raises(VerifyPhaseExhausted):
        sm.transition(E.PATCH_FAILED)    # rc would become MAX → raises


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


def test_allowed_tools_postpatch_blocking_includes_binary_discovery():
    """POSTPATCH_BLOCKING permits find_binary/setup_env/init_workspace so the
    model can fix missing-dependency blocking errors. run_command stays gated."""
    sm = make_sm()
    sm.transition(E.POSTPATCH_BLOCKING)
    tools = sm.allowed_tools()
    assert "find_binary" in tools
    assert "setup_env" in tools
    assert "init_workspace" in tools
    assert "emit_patch" in tools
    assert "run_command" not in tools, "run_command must stay gated until static checks pass"


def test_allowed_tools_postpatch_clean_includes_run_verify_and_env():
    sm = make_sm()
    sm.transition(E.POSTPATCH_CLEAN)
    tools = sm.allowed_tools()
    assert "run_command" in tools
    assert "verify_done" in tools
    assert "find_binary" in tools
    assert "setup_env" in tools
    assert "init_workspace" in tools
    assert "emit_patch" not in tools


def test_allowed_tools_test_failed_includes_emit_patch_run_and_env():
    sm = make_sm()
    sm.transition(E.POSTPATCH_CLEAN)
    sm.transition(E.TEST_FAILED)
    tools = sm.allowed_tools()
    assert "emit_patch" in tools
    assert "run_command" in tools
    assert "find_binary" in tools
    assert "setup_env" in tools
    assert "init_workspace" in tools
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


def test_state_description_must_read_locks_emit_patch():
    sm = make_sm()
    sm.transition(E.PATCH_FAILED)
    desc = sm.state_description()
    assert "emit_patch is locked" in desc
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


def test_state_description_iteration_header():
    sm = make_sm()
    desc = sm.state_description(iteration=7)
    assert "[iteration 7]" in desc


# ── defensive transitions (schema bypass with success) ────────────────────────

def test_postpatch_clean_self_loop_on_postpatch_clean():
    """Schema bypass: emit_patch from POSTPATCH_CLEAN succeeds, analyzer clean.
    SM should idempotently stay in POSTPATCH_CLEAN."""
    sm = make_sm()
    sm.transition(E.POSTPATCH_CLEAN)
    sm.transition(E.POSTPATCH_CLEAN)
    assert sm.state == S.POSTPATCH_CLEAN


def test_postpatch_clean_to_blocking_on_schema_bypass_success():
    """Schema bypass: emit_patch from POSTPATCH_CLEAN succeeds but introduces a
    blocking error. SM moves to POSTPATCH_BLOCKING rather than crashing."""
    sm = make_sm()
    sm.transition(E.POSTPATCH_CLEAN)
    sm.transition(E.POSTPATCH_BLOCKING)
    assert sm.state == S.POSTPATCH_BLOCKING


def test_test_passed_to_postpatch_clean_on_schema_bypass_success():
    """Schema bypass: emit_patch from TEST_PASSED succeeds, analyzer clean.
    SM moves back to POSTPATCH_CLEAN (the new patch invalidates the test result)."""
    sm = make_sm()
    sm.transition(E.POSTPATCH_CLEAN)
    sm.transition(E.TEST_PASSED)
    sm.transition(E.POSTPATCH_CLEAN)
    assert sm.state == S.POSTPATCH_CLEAN


def test_test_passed_to_postpatch_blocking_on_schema_bypass_success():
    """Schema bypass: emit_patch from TEST_PASSED succeeds but introduces a
    blocking error."""
    sm = make_sm()
    sm.transition(E.POSTPATCH_CLEAN)
    sm.transition(E.TEST_PASSED)
    sm.transition(E.POSTPATCH_BLOCKING)
    assert sm.state == S.POSTPATCH_BLOCKING


# ── allowed_action_types (top-level response type filtering) ──────────────────

def test_allowed_action_types_explore_includes_emit_patch_not_verify_done():
    sm = make_sm()
    types = sm.allowed_action_types()
    assert "tool_call" in types
    assert "revision_needed" in types
    assert "emit_patch" in types
    assert "verify_done" not in types


def test_allowed_action_types_must_read_excludes_emit_patch():
    sm = make_sm()
    sm.transition(E.PATCH_FAILED)
    types = sm.allowed_action_types()
    assert "tool_call" in types
    assert "revision_needed" in types
    assert "emit_patch" not in types
    assert "verify_done" not in types


def test_allowed_action_types_postpatch_clean_has_verify_done_not_emit_patch():
    sm = make_sm()
    sm.transition(E.POSTPATCH_CLEAN)
    types = sm.allowed_action_types()
    assert "verify_done" in types
    assert "emit_patch" not in types
    # tool_call always available so run_command can be invoked
    assert "tool_call" in types


def test_allowed_action_types_test_passed_only_terminal_actions():
    sm = make_sm()
    sm.transition(E.POSTPATCH_CLEAN)
    sm.transition(E.TEST_PASSED)
    types = sm.allowed_action_types()
    assert "verify_done" in types
    assert "emit_patch" not in types
    assert "tool_call" in types  # reads still need to work
    assert "revision_needed" in types  # escape hatch


def test_allowed_action_types_revision_needed_always_available():
    """revision_needed is the escape hatch — must be available from every state."""
    sm = make_sm()
    states_to_check = [
        S.EXPLORE,
        S.PATCH_FAILED_MUST_READ,
        S.POSTPATCH_BLOCKING,
        S.POSTPATCH_CLEAN,
        S.TEST_FAILED,
        S.TEST_PASSED,
    ]
    # Walk through each state via valid transitions and check
    sm2 = make_sm()  # EXPLORE
    assert "revision_needed" in sm2.allowed_action_types()
    sm2.transition(E.PATCH_FAILED)  # MUST_READ
    assert "revision_needed" in sm2.allowed_action_types()
    sm2.transition(E.READ_CALLED)  # CAN_RETRY
    assert "revision_needed" in sm2.allowed_action_types()
    sm2.transition(E.POSTPATCH_BLOCKING)  # BLOCKING
    assert "revision_needed" in sm2.allowed_action_types()
    sm2.transition(E.POSTPATCH_CLEAN)  # CLEAN
    assert "revision_needed" in sm2.allowed_action_types()
    sm2.transition(E.TEST_FAILED)  # FAILED
    assert "revision_needed" in sm2.allowed_action_types()
    sm2.transition(E.TEST_PASSED)  # PASSED
    assert "revision_needed" in sm2.allowed_action_types()
    _ = states_to_check  # sanity reference
