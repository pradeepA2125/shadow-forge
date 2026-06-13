# Tier B — Task Lifecycle Control & Durable Telemetry (Design)

**Date:** 2026-06-13
**Branch:** `chat-ui-redesign` (worktree)
**Status:** Approved design — ready for implementation plan
**Supersedes the Tier B notes in:** `docs/superpowers/plans/2026-06-12-chat-ui-v2-handoff.md` (items 1, 2, 3, 5; item 4 deferred)

## Goal

Make the chat UI's lifecycle promises *true and durable*. Today three things are
hollow or ephemeral:

- **Stop** is only safe for chat turns; there is no cooperative abort for an executing task.
- **READY_FOR_REVIEW** is hollow — `_partial_promote` already wrote each completed step's
  files to the real workspace during execution, so the final promote re-copies and reject
  cannot revert (`engine.py:741` TODO).
- **ErrorCard / ReviewCard detail** lives only in extension memory and vanishes on reload —
  `TaskRecord` has no `failure_summary` / `run_summary`.

And one UX lie: the "Review each step" composer toggle is frozen into `TaskRecord.
step_review_auto_accept` at creation; flipping it mid-run does nothing.

## Keystone decision (everything inherits from this)

**Keep the partial-promote write model; make the UI honest about it.** Steps continue
landing in the real workspace per-step. We do NOT move to checkpoint-deferred writes or
add pre-run checkpoints. Consequences, accepted:

- Final accept collapses to `SUCCEEDED` (drop the redundant final re-copy).
- Reject keeps the applied changes and marks `ABORTED` — the UI says so honestly; there is
  no true revert.
- Abort stops the run; whatever already partial-promoted stays. Aborting mid-step is safe
  because `_partial_promote` runs *after* a step returns — a mid-step abort promotes nothing.

## Decisions summary

| # | Decision | Choice |
|---|----------|--------|
| 1 | Workspace-write model | Keep partial-promote; make UI honest |
| 2 | Abort granularity | Cooperative; checked between steps AND between ToolLoop iterations |
| 3 | Mid-task review preference | Full two-way dynamic (live-mutable per task) |
| 4 | Durable telemetry | Persist both `failure_summary` + `run_summary`; on FAILED write both |
| 5 | Structured plan steps at approval gate | **Deferred** (orthogonal, low value, extra LLM call) |

## Architecture

The four features share one new mechanism: a per-running-task **control channel** the
execution loop polls. Abort and the dynamic review preference are both live signals to a
running coroutine in a single-process asyncio engine, so they live in memory on the
orchestrator, not on the persisted record.

### Component 1 — `TaskControl` (the shared mechanism)

```python
@dataclass
class TaskControl:
    abort: asyncio.Event              # set by the abort route; polled by the loop
    step_review_auto_accept: bool     # live-mutable; re-read before each step gate
```

- **Owns:** `AgentOrchestrator._task_controls: dict[str, TaskControl]`.
- **Lifecycle:** created when a task starts running (`run_task` / `resume_task`), removed at
  terminal state (in the same `finally` that already finalizes the run).
- **Concurrency:** single-process asyncio ⇒ check+set with no `await` between is race-safe
  (same pattern as `_in_flight_resume` / `_in_flight_feedback` in `build_router`).
- **What it does:** one channel for two live signals. **Interface:** routes call
  `control.abort.set()` / assign `control.step_review_auto_accept`; the loop reads them.
  **Depends on:** nothing new; lives entirely in the engine. Seed value of
  `step_review_auto_accept` comes from `TaskRecord.step_review_auto_accept` (creation-time
  default), then diverges live.

### Component 2 — Cooperative abort (F12)

- **Route:** `POST /tasks/{id}/abort` → `control.abort.set()` for a running task. It does NOT
  touch the shadow or status — the coroutine owns those. `/cancel` stays as-is for
  queued/terminal tasks.
- **Loop checks:**
  - `_execute_plan` checks `control.abort.is_set()` at the top of each step.
  - `ToolLoop` checks between ReAct iterations and raises `TaskAborted`.
  - Because `_partial_promote` runs *after* a step returns, raising mid-step leaves
    **nothing half-promoted**; already-completed (promoted) steps stay in the real workspace.
- **ABORTED-aware unwind:** catch `TaskAborted` → `transition(task, ABORTED)` **once** →
  save → clean shadow → breadcrumb. Invariant (CLAUDE.md gate lessons): no later save
  re-writes a stale status over `ABORTED` (the stale-object clobber bug). The caller holds
  the task reference; we mutate it in place, never re-fetch a divergent copy.
- **Shadow cleanup ordering:** cleanup happens *after* the coroutine acknowledges the abort
  (inside the unwind), never from the route — fixes the current `/cancel` hazard where the
  route frees the shadow while the coroutine still runs against it.
- **No state-machine change:** `_TRANSITIONS` already permits `ABORTED` from every running
  state (EXECUTING, VALIDATING, REPAIRING, PLANNED, all `AWAITING_*`).

### Component 3 — Final-review collapse (F8)

- `PROMOTING` stops re-copying files (already partial-promoted). It becomes **finalize only**:
  clean shadow, finalize `run_summary`, → `SUCCEEDED`. No state-machine change (the
  `READY_FOR_REVIEW → PROMOTING → SUCCEEDED` edges already exist).
- **Finish** (accept at READY_FOR_REVIEW) → finalize → `SUCCEEDED`.
- **Close without finishing** (reject) → `ABORTED`, applied changes kept.
- The already-shipped honest ReviewCard copy ("Task complete — changes applied / Finish /
  Close without finishing") becomes fully accurate.
- Same critique applies to the normal validation-pass path and the validation-accept gate
  path (`engine.py:741`): both drop the redundant final re-copy.

### Component 4 — Durable telemetry (F9 + F8 → item 3)

```python
class FailureSummary(BaseModel):
    step_id: str | None
    step_index: int | None     # "step 3 of 4"
    error_class: str           # e.g. "VerifyPhaseExhausted"
    message: str               # capped

class RunSummary(BaseModel):
    steps_completed: int
    steps_total: int
    deviations: list[str]      # scope extensions, delta replans, discarded steps, validation-accepted

# TaskRecord additions:
failure_summary: FailureSummary | None = None
run_summary: RunSummary | None = None
```

- **`run_summary` is finalized on EVERY terminal transition** (SUCCEEDED / FAILED / ABORTED),
  accumulated server-side during the run from `execution_state` (which already tracks
  `delta_replans_used`, scope approvals, discarded steps, validation-accepted).
- **`failure_summary` is additionally written on FAILED**, capturing the failing step +
  error class. **On a failure both are populated** so the ErrorCard shows the failure detail
  *and* the run-so-far context ("got through 2 of 4, one scope extension, then step 3 —
  VerifyPhaseExhausted").
- **Exposure:** `resolve_live_state` adds `failure_summary` when FAILED/ABORTED and
  `run_summary` whenever present; both also surface on `TaskResult` / `TaskView`.
- **Frontend:** ErrorCard ← `failure_summary` (+ `run_summary`); ReviewCard ← `run_summary`.
  The extension's ephemeral `runDeviations` / `lastStepStarted` / `lastPatchError` become a
  live supplement the durable copy supersedes on reload (keep for live-feel; no longer the
  source of truth).

### Component 5 — Dynamic review preference (item 5)

- **Route:** `POST /tasks/{id}/review-pref {auto_accept: bool}` → sets
  `control.step_review_auto_accept`.
- **Engine:** re-reads `control.step_review_auto_accept` before each step's gate decision,
  instead of the frozen `TaskRecord` value.
- **Edge case (pinned):** if a step gate is **currently pending** and the user flips to
  auto-accept, the `/review-pref` route resolves that pending gate as **accept** too
  (consistent intent) — it checks for a live `pending_step_review` and fires its decision
  future. Flipping the other way (→ review) only affects future steps.
- **Frontend:** the composer checkbox stays enabled during execution and posts to
  `/review-pref` both directions; the StepGate card gains an "Accept & auto-accept the rest"
  affordance that posts the same.

## Frontend touchpoints (extension + webview)

- Work-bar **Stop** shown during task execution → `POST /abort` (F12). (Chat-turn Stop
  unchanged.)
- Dynamic **review checkbox** + StepGate "auto-accept the rest" → `POST /review-pref`.
- **ErrorCard** renders durable `failure_summary` (+ `run_summary`); **ReviewCard** renders
  durable `run_summary`; Finish/Close copy already correct.

## Implementation slices (one spec, sequenced)

The control channel is the foundation both abort and dynamic-pref need, so it lands first.

1. **Control channel + cooperative abort** (backend) + Stop button (frontend).
2. **Final-review collapse** (backend) — PROMOTING finalize-only.
3. **Durable telemetry** (backend model + `/live` + `TaskResult`) + ErrorCard/ReviewCard
   durable render (frontend).
4. **Dynamic review preference** (backend re-read + route) + dynamic checkbox (frontend).

## Testing posture

- Python: integration-style with real `tmp_path` shadows and the scripted engine. Use
  `SQLiteTaskStore` (not `InMemoryTaskStore`) for any test that depends on store-returns-a-
  copy semantics / object-divergence (per CLAUDE.md) — notably the ABORTED-aware no-clobber
  test and the gate-resolve-on-pref-flip test.
- Key cases: abort between steps; abort mid-ToolLoop-iteration leaves nothing half-promoted;
  ABORTED-aware save does not clobber; shadow cleanup ordering; accept→SUCCEEDED performs no
  re-copy; reject→ABORTED keeps changes; `run_summary` finalized on all three terminal
  states; `failure_summary` + `run_summary` both present on FAILED; pref flip resolves a
  pending step gate.
- TypeScript: vitest for ErrorCard/ReviewCard durable render and the dynamic checkbox /
  Stop-during-execution controller paths.

## Invariants carried over (CLAUDE.md gate lessons)

- Gates and aborts clear/transition the **caller's** task object in place; never re-fetch a
  divergent copy and mutate that (stale-object clobber → card reappears on reload, 409 on
  re-action, invalid-transition crash).
- Decision routes only `future.set_result(...)`; they never mutate/persist the task, so the
  `await` is safe.

## Out of scope (explicitly deferred)

- **Checkpoint-deferred writes / true final revert** — the keystone keeps partial-promote;
  a real revert (checkpoint-based workspace restore) is a separate backend redesign.
- **Structured plan steps at the approval gate** (item 4) — orthogonal (plan-conversion
  timing), low value, costs a pre-approval LLM call.
- **Mid-iteration abort of non-task ToolLoops** (inline change / chat) — abort applies to
  task execution; chat-turn Stop already works via SSE disconnect.
