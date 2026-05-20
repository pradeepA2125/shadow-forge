# Verify Phase State Machine — Post-Implementation Verification

**Date:** 2026-05-20
**Subject:** Verification of [`VerifyPhaseStateMachine`](../../../services/agentd-py/agentd/tools/verify_phase_sm.py) and its integration in [`tools/loop.py`](../../../services/agentd-py/agentd/tools/loop.py)
**Status:** All 394 unit + integration tests pass (1 skipped). This doc traces real-world scenarios through the implementation to identify gaps the test suite does not yet cover.

---

## 1. What was built (recap)

A 7-state, 6-event state machine that replaces five scattered boolean flags in `tools/loop.py::ToolLoop.run()`:

- **States:** `EXPLORE`, `PATCH_FAILED_MUST_READ`, `PATCH_FAILED_CAN_RETRY`, `POSTPATCH_BLOCKING`, `POSTPATCH_CLEAN`, `TEST_FAILED`, `TEST_PASSED`
- **Events:** `PATCH_FAILED`, `READ_CALLED`, `POSTPATCH_BLOCKING`, `POSTPATCH_CLEAN`, `TEST_FAILED`, `TEST_PASSED`
- **Two enforcement layers:**
  1. **Schema filtering** — `sm.allowed_tools()` filters the JSON schema's `tool` enum each turn (`loop.py:172-175`).
  2. **Handler-level state check** — `verify_done` is rejected by the loop if `sm.state ∉ {POSTPATCH_CLEAN, TEST_PASSED}` (`loop.py:228-256`).
- **Dedup:** `emit_patch` calls are keyed and blocked within a single state stay; cache clears on every transition.
- **Retry counter:** `MAX_PATCH_RETRIES = 5` consecutive failures from `PATCH_FAILED_CAN_RETRY` raise `VerifyPhaseExhausted`, which the loop converts to `VerifyResult(verified=False)`.
- **Per-turn instruction:** `sm.state_description(iteration, error_summary, failure_summary)` is injected as the `instruction` field of the user payload (`tool_prompts.py:172` and `loop.py:208-212`).

---

## 2. Test coverage today

| Layer | File | Count |
|---|---|---|
| SM unit | `tests/test_verify_phase_sm.py` | 28 |
| ToolLoop integration | `tests/test_tool_loop_skip_verify.py`, `…event_format.py`, `…scope_gate.py` | 12 |
| Orchestrator end-to-end | `tests/test_orchestrator_verify_flow.py` | 8 (incl. 2 new SM tests) |

The new SM tests cover: (a) `POSTPATCH_CLEAN → verify_done` without `run_command`, (b) `MAX_PATCH_RETRIES` exhaustion returning `VerifyResult(verified=False)`, (c) state-description threading.

---

## 3. Scenarios that work correctly (verified)

### 3.1 Happy path — single patch, no tests
```
EXPLORE → emit_patch (ok) → analyzer clean → POSTPATCH_CLEAN
        → verify_done(True) → terminal
```
Covered by `test_state_machine_verify_done_allowed_in_postpatch_clean_without_test`.

### 3.2 Happy path — single patch + scoped test
```
EXPLORE → emit_patch (ok) → POSTPATCH_CLEAN
        → run_command pytest (exit 0) → TEST_PASSED
        → verify_done(True) → terminal
```
The `run_command` event dispatch lives at `loop.py:684-700`. `TEST_PASSED` requires being in `POSTPATCH_CLEAN` or `TEST_FAILED` first — the gate is correct.

### 3.3 Postpatch blocking, then clean
```
EXPLORE → emit_patch (ok, mypy fails) → POSTPATCH_BLOCKING
        → emit_patch (ok, mypy passes) → POSTPATCH_CLEAN → ...
```
`emit_patch` IS in `POSTPATCH_BLOCKING`'s allowed tools — model is expected to fix and re-patch.

### 3.4 Patch failure → must-read → retry → success
```
EXPLORE → emit_patch (fail: search-text not found) → PATCH_FAILED_MUST_READ
        → read_file (READ_CALLED) → PATCH_FAILED_CAN_RETRY
        → emit_patch (ok) → POSTPATCH_CLEAN → …
```
`READ_CALLED` is only dispatched when `sm.state == PATCH_FAILED_MUST_READ` (`loop.py:651-661`). Other reads execute without firing.

### 3.5 Retry exhaustion
After 5 cycles of MUST_READ → CAN_RETRY → PATCH_FAILED, `VerifyPhaseExhausted` is raised. Both call sites that fire `PATCH_FAILED` (`loop.py:377-394`, `453-470`) catch the exception and return a `VerifyResult(verified=False)` with a `"giving up"` message. Covered by `test_state_machine_patch_retry_exhaustion_returns_verify_result`.

### 3.6 Tests fail, then pass after re-patch
```
POSTPATCH_CLEAN → run_command (fail) → TEST_FAILED
              → emit_patch (ok) → POSTPATCH_CLEAN
              → run_command (pass) → TEST_PASSED → verify_done
```
`emit_patch` is in `TEST_FAILED`'s allowed tools.

### 3.7 Tests fail, narrow re-run passes (no extra patch)
```
POSTPATCH_CLEAN → run_command (full suite, fail) → TEST_FAILED
              → run_command (single failing test, pass) → TEST_PASSED
```
`run_command` is in `TEST_FAILED`'s allowed tools too — the SM allows re-running without forcing a patch first.

### 3.8 Scope-extension granted, retry succeeds
```
EXPLORE → emit_patch (scope error)
        → callback approves → retry _apply_patch_inline → success
        → POSTPATCH_*
```
No `PATCH_FAILED` event fires on the scope-granted success path (`loop.py:396`: "fall through to success path"). Correct.

### 3.9 Scope denied — model must adjust or revise
```
EXPLORE → emit_patch (scope error) → callback rejects
        → stays in EXPLORE
```
**No SM transition.** Dedup cache **retains** the patch_key (it was recorded at `loop.py:331` before `_apply_patch_inline`). The model cannot retry the *exact same* patch, which is correct — the scope didn't change.

### 3.10 Dedup catches schema-bypass repeats
Even though `emit_patch` is in the response schema's `type` enum for every state, a malicious or confused model emitting the same patch twice within a state stay is caught by `sm.check_patch_dedup` (`loop.py:315`).

---

## 4. Defensive behaviors confirmed

| Bypass scenario | Behavior | Source |
|---|---|---|
| `emit_patch` from `MUST_READ`, patch fails | Stay in `MUST_READ`, retry counter NOT incremented (read precondition not met) | `verify_phase_sm.py:53-56` |
| `emit_patch` from `POSTPATCH_CLEAN`, patch fails | Recover to `MUST_READ` | `verify_phase_sm.py:67-70` |
| `emit_patch` from `TEST_PASSED`, patch fails | Recover to `MUST_READ` | `verify_phase_sm.py:74-75` |
| `verify_done` from `EXPLORE/MUST_READ/CAN_RETRY/BLOCKING/TEST_FAILED` | Pushback message + stay in current state | `loop.py:237-256` |
| `revision_needed` from any state | Always accepted → `PlanHandoff` | `loop.py:258-275` |

---

## 5. Gaps & risks identified

### 🔴 Gap 5.1 — Schema-bypass with *successful* patch from `POSTPATCH_CLEAN` or `TEST_PASSED`

**Scenario:** The response schema's `type` enum still allows `emit_patch` from every state (filtering only happens on the inner `tool_call.tool` enum, not the outer `type` enum). A small model that ignores the per-turn `state_description` could emit `{"type": "emit_patch", ...}` from `POSTPATCH_CLEAN` or `TEST_PASSED`. If that patch **succeeds**, the loop runs `PostPatchAnalyzer` and fires `POSTPATCH_CLEAN` or `POSTPATCH_BLOCKING`.

**The transition table at `verify_phase_sm.py:45-76` does not define:**
- `POSTPATCH_CLEAN + POSTPATCH_CLEAN`
- `POSTPATCH_CLEAN + POSTPATCH_BLOCKING`
- `TEST_PASSED + POSTPATCH_CLEAN`
- `TEST_PASSED + POSTPATCH_BLOCKING`

So `sm.transition()` will raise `InvalidVerifyPhaseTransition`, which is **not caught** by the postpatch dispatch site (`loop.py:520-525`). The loop crashes mid-step, `_run_step_with_retries` catches the exception, and the orchestrator marks the step attempt failed — work is lost, no graceful pushback to the model.

**Likelihood:** Low for well-behaved models; moderate for small/quantized local models. The defensive `PATCH_FAILED` transitions for these states (5.1's siblings) suggest the original intent was symmetric — the success-side counterparts appear to have been overlooked.

**Recommended fix:** Add four self-loop or recovery transitions:
```python
(_S.POSTPATCH_CLEAN,  _E.POSTPATCH_CLEAN):    _S.POSTPATCH_CLEAN,    # self-loop
(_S.POSTPATCH_CLEAN,  _E.POSTPATCH_BLOCKING): _S.POSTPATCH_BLOCKING,
(_S.TEST_PASSED,      _E.POSTPATCH_CLEAN):    _S.POSTPATCH_CLEAN,    # back into verify
(_S.TEST_PASSED,      _E.POSTPATCH_BLOCKING): _S.POSTPATCH_BLOCKING,
```
Or — equivalent and arguably cleaner — wrap the postpatch-event dispatch in a try/except (`loop.py:520-525`) that logs and stays in the current state when the transition is undefined. Adding a unit test for this exact bypass should be part of the fix.

### 🟡 Gap 5.2 — `POSTPATCH_BLOCKING` cannot install dependencies

**Scenario:** `mypy` reports `error: Cannot find module 'X'` because a dep is missing from `requirements.txt`. State → `POSTPATCH_BLOCKING`. The allowed-tools set for `POSTPATCH_BLOCKING` (`verify_phase_sm.py:85-86`) is `{read_file, search_code, list_directory, search_semantic, emit_patch}` — **no `setup_env`, `init_workspace`, or `find_binary`**.

The model's only path forward is `emit_patch` (e.g., remove the import, add a stub, edit requirements). It cannot run `setup_env` to install the missing dep. If the right fix is "install the dep," the model is stuck and likely loops on `emit_patch` retries until `MAX_PATCH_RETRIES` exhausts.

**Why the design ended up this way:** The original grouping in the spec ([`docs/.../2026-05-20-verify-phase-statemachine-design.md`](2026-05-20-verify-phase-statemachine-design.md)) puts `run_command/find_binary/setup_env/init_workspace` together as a "run-or-install" group, gated only in `POSTPATCH_CLEAN` and `TEST_FAILED`. `POSTPATCH_BLOCKING` is treated as "fix the code before you can run anything." This is consistent with the spec but conflicts with the real-world case where the blocking error itself is a missing-binary symptom.

**Recommended fix:** Add `find_binary` to `POSTPATCH_BLOCKING`'s allowed tools (it's diagnostic-only — no state event fires). Optionally add `setup_env` and `init_workspace` for the missing-dep case. Spec text would need a corresponding update.

### 🟡 Risk 5.3 — `type` enum is not dynamically filtered

The spec promises "hard schema enforcement — the model cannot call a tool that is absent from the schema." This holds for tools inside `tool_call` (`AGENT_STEP_RESPONSE_SCHEMA.properties.tool` — well, today the `tool` field is `{"type": "string"}` with no enum at all, but `tool_definitions` is the contract the model sees). But the **outer `type` enum** (`tool_call | emit_patch | verify_done | revision_needed`) at `tool_prompts.py:13` is **static**.

A determined model can still emit `{"type": "emit_patch", …}` from `PATCH_FAILED_MUST_READ`. The loop's defenses are:
- Dedup (only catches *repeats*, not first attempts)
- Defensive transitions (handle the *failure* outcome — Gap 5.1 documents the missing *success* coverage)
- `verify_done` handler state check (catches one of four action types)

**Recommended fix (optional, larger):** Dynamically rewrite `AGENT_STEP_RESPONSE_SCHEMA.properties.type.enum` per turn based on `sm.allowed_tools()` (include `tool_call`/`emit_patch`/`verify_done` only when their preconditions hold). This would close the gap completely but introduces schema mutation that providers may handle differently. Decision: probably not worth doing until production data shows real models bypass.

### 🟢 Minor 5.4 — Dedup cache and scope-denied path interaction

When `emit_patch` hits a scope-denied error in `EXPLORE`, the loop stays in `EXPLORE` (no SM transition → cache NOT cleared). The recorded `patch_key` blocks the model from retrying the same patch. This is correct, but the pushback message (`loop.py:399-408`) does NOT mention that the patch is now dedup-blocked. If the model tries the same patch again it'll hit `DUPLICATE PATCH BLOCKED` on the next turn, with a slightly redundant explanation.

**Recommended fix (optional, cosmetic):** Mention "this exact patch is now blocked from retry" in the scope-denied feedback at `loop.py:399-405`. Low impact.

### 🟢 Minor 5.5 — `_last_auto_checks_error` lingers across PATCH_FAILED

`_last_auto_checks_error` is set when `POSTPATCH_BLOCKING` fires (`loop.py:516`). If the model then emits a patch from `POSTPATCH_BLOCKING` that *fails* (engine error), the SM moves to `MUST_READ` but `_last_auto_checks_error` is not cleared. The state description for `MUST_READ` doesn't use `error_summary`, so the stale value is **not surfaced**. Safe. But: if the model then transitions back to `POSTPATCH_BLOCKING` (different patch, new blocking error), the new `auto_checks` overwrites the old one. Also safe.

No fix needed — included for completeness.

### 🟢 Minor 5.6 — Iteration counter is per-`run()`, not per-step-retry

`sm = VerifyPhaseStateMachine()` is created fresh at the top of `run()`. If the orchestrator retries the same step (e.g., `verify_done(verified=False)` causes step retry in `_run_step_with_retries`), a new `ToolLoop.run()` is called with a fresh SM. The retry counter resets, dedup cache is empty. This is the **intended** behavior — a fresh step attempt should not inherit the prior attempt's failed-patch history. Just documenting.

---

## 6. Operational observations

### 6.1 Removed feature: read-side dedup

The pre-SM `_seen_calls` dict deduped *all* tool calls (including `read_file`, `search_code`). The new SM only dedups `emit_patch`. A consequence: a model that re-reads the same file 10 times in `EXPLORE` will burn 10 budget slots. In practice this is fine — reads are cheap and idempotent, and the per-turn `state_description` should steer the model. But for runaway models, the only backstop is the `max_tool_calls_per_step` budget.

### 6.2 `setup_env`/`init_workspace` no longer clear test failures

The pre-SM loop reset `last_verify_run_errored = False` after a successful `setup_env`/`init_workspace` to allow the next `run_command` to be considered fresh. The SM removes this: a successful `setup_env` in `TEST_FAILED` keeps the SM in `TEST_FAILED`. To leave `TEST_FAILED` the model must run a passing `run_command` (which is in `TEST_FAILED`'s allowed tools). This is more honest — installing a dep doesn't prove tests pass — but worth flagging for behavioral parity.

### 6.3 The `"Patch applied successfully"` sentinel

The patch-applied history message at `loop.py:534` contains the literal string `"Patch applied successfully."` as a sentinel for scripted test engines that detect verify phase (per [`CLAUDE.md`](../../../CLAUDE.md)). The earlier iteration of this commit briefly changed it to `"Patch applied."` and broke many scripted tests; restoring the sentinel kept the contract intact. Worth noting because the post-`state_description` design philosophy is "the instruction field tells you the state" — the sentinel is now a *test-only* contract, not a production hint.

---

## 7. Recommended next actions (in priority order)

| # | Action | Effort | Priority |
|---|---|---|---|
| 1 | Fix Gap 5.1 (success-side defensive transitions). Add the 4 transitions + a unit test asserting `sm.transition(POSTPATCH_CLEAN)` is idempotent from `POSTPATCH_CLEAN`. | ~10 min | High — only crash-risk gap |
| 2 | Decide Gap 5.2 (`find_binary` in `POSTPATCH_BLOCKING`). If yes, update `verify_phase_sm.py` + spec. | ~5 min + spec | Medium |
| 3 | Cosmetic 5.4 — mention dedup block in scope-denied feedback. | ~5 min | Low |
| 4 | Optional Risk 5.3 — dynamically filter the outer `type` enum. | ~30 min + provider testing | Low (defensive only) |
| 5 | Documentation: update `CLAUDE.md` "Step execution" section to mention the SM (currently doesn't reference it). | ~10 min | Low |

---

## 8. Verdict

The implementation matches the spec for the happy paths and the documented failure cycles. All 394 tests pass. The one **crash-risk gap** is 5.1 — a model bypassing the schema with a successful patch from `POSTPATCH_CLEAN` or `TEST_PASSED` will raise `InvalidVerifyPhaseTransition`. The fix is symmetric with the defensive transitions already added and is low-effort.

Recommend addressing Gap 5.1 before merging. Gaps 5.2–5.6 can be deferred or skipped depending on production signals.
