# Task Narrative — LLM-authored run summary (Design)

**Date:** 2026-06-13
**Branch:** `chat-ui-redesign` (worktree)
**Status:** Approved design — ready for implementation plan
**Builds on:** Tier B durable telemetry (`2026-06-13-tier-b-lifecycle-control-telemetry-design.md`) — reuses `RunSummary`/`FailureSummary`, the terminal finalize chokepoints, and the `/live`+`TaskResult` exposure path.

## Goal

Give every finished task an **LLM-authored narrative** of what it actually did — a one-line headline plus a few "what happened" points — serving two consumers:

1. **The reviewer** — rendered on the ReviewCard (success) and ErrorCard (fail/abort) so the user understands the change without diffing every file.
2. **The next chat turn** — persisted into the thread so the `ChatAgent`'s explore/classify/respond phases inherit "here's what the last task accomplished (or attempted, and where it stopped)" as compact, high-signal context.

This is distinct from the existing **`run_summary`** (deterministic counts: `steps_completed`, `steps_total`, `deviations`). `run_summary` is the *numbers*; `task_narrative` is the *story*.

## Keystone decision (everything inherits from this)

**Accumulate an append-only event log of the run, then synthesize a narrative from it with one LLM call per terminal.**

- Per-step prose is captured **for free** by adding a `step_summary` field to the `verify_done` action the execution agent already emits — no extra per-step LLM call.
- The accumulator is an **append-only `run_events` log**, never pruned. A delta replan reverting/replacing steps does NOT rewrite history — the dead-ends and the course-correction stay in the log, because they are the most informative part of the story (for both the reviewer and the next turn).
- One `summarize_run` LLM call at each terminal (SUCCEEDED / FAILED / ABORTED) turns the ordered log into `{headline, points}`, reusing the exact terminal chokepoints built for `run_summary`.

Why append-only (not a keyed final-state snapshot that drops reverted steps): a snapshot is lossy (it erases the journey) and forces mutation logic that must stay in sync with `_apply_revision`. The log is a simpler invariant (only ever append) and is fully decoupled from `completed_step_ids` / the live `x of n` progress, so a delta replan that grows or shrinks the plan cannot corrupt it.

## Decisions summary

| # | Decision | Choice |
|---|----------|--------|
| 1 | What the narrative is for | Reviewer card **and** next-turn chat context |
| 2 | Per-step capture | `step_summary` field on `verify_done` (zero extra calls) + deterministic fallback |
| 3 | Accumulator shape | **Append-only `run_events` log** (never pruned) |
| 4 | Synthesis | One `summarize_run` call per terminal, outcome-aware |
| 5 | Trigger scope | Every run outcome — at **READY_FOR_REVIEW** (success path, so the card shows it pre-accept) + **FAILED** / **ABORTED** |
| 6 | Delta replan | Append a `replan` event; `_apply_revision` untouched (no pruning) |
| 7 | Naming | `task_narrative` (LLM story) vs `run_summary` (deterministic counts) |
| 8 | v1 latency | Synthesis is **synchronous** at terminal (one call); async deferred (YAGNI) |

## Architecture

### Component 1 — Data model

```python
class RunEvent(BaseModel):
    kind: Literal["step_done", "step_failed", "replan"]
    # step_done / step_failed
    step_id: str | None = None
    goal: str | None = None
    note: str | None = None          # model-authored (verify_done step_summary) or deterministic
    # replan
    reason: str | None = None
    reverted_step_ids: list[str] = Field(default_factory=list)
    revised_step_ids: list[str] = Field(default_factory=list)

class TaskNarrative(BaseModel):
    outcome: Literal["succeeded", "failed", "aborted"]
    headline: str
    points: list[str]

# TaskExecutionState addition:
run_events: list[RunEvent] = Field(default_factory=list)   # append-only, never pruned

# TaskRecord addition:
task_narrative: TaskNarrative | None = None
```

### Component 2 — Per-step capture (free)

- **Schema:** add an optional `step_summary` string to the `verify_done` action in `AGENT_STEP_RESPONSE_SCHEMA` (`reasoning/tool_prompts.py`) — the execution agent authors a one-sentence "what this step did" inside the `verify_done` it already emits. (Anthropic has no constrained decoding — the field is described in the stringified schema; compliance is prompt-level, same caveat as every other field.)
- **Loop:** `tools/loop.py`'s `verify_done` branch reads `step_summary` and carries it onto `VerifyResult` (new optional field).
- **Orchestrator:** when a step completes (`_merge_step_result` / the `_mark_step_completed` site in `_execute_plan`), append a `step_done` event `{step_id, goal, note}` — `note` is the model's `step_summary`, or a deterministic fallback (`"edited <touched files>"`) when absent.
- **Failure fallback:** at the step-exhaustion FAILED site, append a `step_failed` event with a deterministic note. No step is silently absent from the log.

### Component 3 — Delta replan event

- At the `revision_needed` / `PlanHandoff` handling site in `_execute_plan` (before `_apply_revision` runs), append a `replan` event `{reason, reverted_step_ids, revised_step_ids}` from the `PlanHandoff` + `PlanRevisionResult`.
- `_apply_revision` is **untouched** — the log is append-only, so the reverted steps' earlier `step_done` events remain (the synthesis sees "did step 2 → replanned because X → redid step 2 as Y"). The plan list may grow or shrink (`_apply_revision` drops reverted-without-replacement steps and appends brand-new ones); the log is decoupled from plan size and unaffected.

### Component 4 — Synthesis (`summarize_run`)

- New reasoning method on the `ReasoningEngine` protocol: `summarize_run(...) -> dict` returning `{headline, points}` (structured output). Implemented across providers and `ScriptedReasoningEngine`.
- **Input:** goal, outcome, ordered `run_events`, `run_summary.deviations`, final `modified_files`. The prompt is outcome-aware (frames a failure/abort as "attempted … stopped at …").
- **Trigger:** generated **inside the `_execute_plan` finally**, mirroring where `run_summary` is finalized — at **`READY_FOR_REVIEW`** (success path) and at **`FAILED`/`ABORTED`**. It is authored at `READY_FOR_REVIEW` (not at accept) precisely because the ReviewCard renders the narrative *before* the user decides; outcome there is `"succeeded"` (execution + validation passed, pending the Finish/Discard gate). The accept route (`→SUCCEEDED`) does **not** re-synthesize — the `READY_FOR_REVIEW` narrative carries over. Result stored as `task.task_narrative` and saved.
- **Discard-after-review (resolved ambiguity):** if the user Discards at `READY_FOR_REVIEW` (reject → true revert → `ABORTED`), the narrative is **not regenerated** in v1. It accurately describes the executed work; the discard is recorded separately by the existing ✗ breadcrumb / `run_summary`, so the transcript reads "did X … ✗ all changes discarded" — an accurate pair for the reviewer and the next turn. (Regenerating on discard is a deferred refinement.)
- **v1 is synchronous** — one call; if the few-seconds latency before the ReviewCard becomes a problem, make it async and stream into the card (deferred).

### Component 5 — Exposure & chat-turn consumption

- **Exposure:** `task_narrative` flows through `resolve_live_state` (whenever present) and `TaskResult`/`TaskView`, exactly like `run_summary`/`failure_summary`. Frontend Zod mirror + camel/snake mapping in `http-backend-client`.
- **Reviewer render:** ReviewCard (success) and ErrorCard (fail/abort) show `headline` + `points`.
- **Next-turn context, two cheap hooks:**
  1. Persist the narrative as a durable `agent/text` transcript message at terminal — it then rides the existing `thread.messages → history` plumbing into the explore (`history[-6:]`) and QA (`history[-10:]`) phases for free.
  2. `_find_recent_task` (already computed per turn and passed to the classifier and the resume path in `chat/agent.py`) surfaces the recent task's `task_narrative`, so the classifier sees "last task: \<headline\>" explicitly, independent of history truncation.

## Frontend touchpoints

- `editor-client` contracts: `TaskNarrative` Zod + `task_narrative` on TaskView/TaskResult/ThreadLiveState; snake→camel mapping. (`run_events` stays backend-internal — only the synthesized `task_narrative` crosses the wire.)
- `controller.ts`: forward `live.task_narrative` into the ReviewCard / ErrorCard render data.
- `webview-ui`: ReviewCard + ErrorCard render `headline` + `points`.

## Testing posture

- Python (scripted engine + `tmp_path`): `verify_done.step_summary` flows into a `step_done` event; `step_failed` appended on exhaustion; a `replan` event recorded with reverted/revised ids, and the reverted step's earlier `step_done` event **remains** (append-only); `summarize_run` invoked once per outcome — at READY_FOR_REVIEW (success), FAILED, and ABORTED — and `task_narrative` persisted + exposed via `/live`/`TaskResult`; the recent-task hook surfaces `task_narrative` to the classifier. `ScriptedReasoningEngine` implements `summarize_run`.
- TypeScript: vitest for ReviewCard/ErrorCard narrative render and contract round-trip of the new fields.

## Out of scope / deferred

- **Async synthesis + streaming the narrative into the card** — v1 is synchronous (one call). Revisit only if latency hurts.
- **Deeper delta-replan trace (TODO):** before implementing Component 3, re-trace the delta-replan path end-to-end — confirm the precise `reverted_step_ids` vs `revised_steps` semantics, the exact append ordering relative to `_apply_revision`, and whether any non-`verify_done` / non-exhaustion step terminal can slip through without an event. Carry this note into the chat-UI handoff at its next update.
- **Batching/segmenting the log for very long runs** — the log is bounded in practice (`max_delta_replans` default 3); no segmentation needed now.
