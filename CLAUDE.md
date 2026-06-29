# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Service Layout

Polyglot monorepo — four packages, three runtimes:
- `apps/editor-client` (TypeScript): Zod-validated contracts + HTTP client for the backend
- `apps/vscode-extension` (TypeScript): VS Code extension — UI, polling, review panel
- `services/agentd-py` (Python): orchestration backend — task lifecycle, planning, patch, provider integrations
- `services/indexer-rs` (Rust): incremental indexing and symbol graph (tree-sitter parse + LSP-resolved Calls/Implements/Inherits edges + LSP diagnostics)

`apps/editor-client` is an npm workspace package consumed by the VS Code extension. Type changes there flow upstream to the extension via `BackendTaskClient` interface and Zod schemas.

## Commands

### TypeScript (root — runs across all workspaces)
```bash
npm install
npm run build        # build all TS packages
npm run test         # vitest across editor-client + vscode-extension
npm run typecheck    # tsc --noEmit across all workspaces
```

### TypeScript (workspace-scoped)
```bash
npm run -w @ai-editor/editor-client test
npm run -w @ai-editor/vscode-extension test
npm run -w @ai-editor/vscode-extension typecheck
```

### Python backend
```bash
cd services/agentd-py
python -m venv .venv && source .venv/bin/activate
pip install -e .[dev]

uvicorn agentd.main:app --reload --port 8000   # start server

pytest                          # all tests
pytest tests/test_foo.py        # single file
pytest tests/test_foo.py::test_bar  # single test

ruff check .                    # lint
ruff format .                   # format
mypy agentd                     # type-check
```

### Rust indexer
```bash
cd services/indexer-rs
cargo build
cargo run -- index --workspace /path/to/repo --snapshot-path /path/to/.ai-editor/index-snapshot.json --watch 0
cargo run -- query --snapshot-path /path/to/.ai-editor/index-snapshot.json --mode symbol_name --value build --depth 2 --limit 200
```

### Stress / E2E scripts
```bash
cd scripts/stress
./bootstrap.sh          # one-time env setup
./start-backend.sh      # start agentd-py with the right provider
python e2e-stress-test.py
```

## Task Lifecycle (Spec-First Model)

```
QUEUED → CONTEXT_READY → AWAITING_PLAN_APPROVAL ─[user gate]─► PLANNED
       → EXECUTING → VALIDATING ⇄ REPAIRING → VALIDATED
       → READY_FOR_REVIEW → PROMOTING → SUCCEEDED
                                              (or FAILED / ABORTED at any point)
```

Key invariants:
- **Shadow workspace**: every task gets its own shadow copy of the real repo. All patch ops run on the shadow; the real workspace is only written on `PROMOTING` (accept).
- **Agentic planning**: `CONTEXT_READY` means the `PlanningAgent` is actively exploring the real workspace (calling `search_code`, `read_file`, `list_directory`) before committing to a plan. This can take many tool calls and is the longest phase before `AWAITING_PLAN_APPROVAL`.
- **Plan approval gate**: the orchestrator pauses at `AWAITING_PLAN_APPROVAL`, emits `plan_markdown`, and waits for `POST /v1/tasks/{id}/plan/feedback`. `feedback=null` means approve; a string triggers plan re-exploration.
- **Agentic execution**: each plan step runs a `ToolLoop` (ReAct: Thought→Tool→Observe) instead of a single-shot patch call. The execution agent can `search_code`, `read_file`, `run_command`, and `search_semantic` before emitting patch ops. When the agent determines a step's approach is wrong, it emits `revision_needed` → delta replan fires.
- **Verify phase always runs**: after emitting a patch, the execution agent always enters a verify phase (Phase 2) to run linters and tests. There is no skip for steps without a `test_command` — the agent discovers what to run from `testing_strategy` and touched files. `verify_done(verified=true)` is gated by the state machine (see below).
- **Verify-phase state machine** (`tools/verify_phase_sm.py`): `VerifyPhaseStateMachine` owns all verify-phase state — replaces five scattered boolean flags previously in `loop.py`. 7 states (`EXPLORE`, `PATCH_FAILED_MUST_READ`, `PATCH_FAILED_CAN_RETRY`, `POSTPATCH_BLOCKING`, `POSTPATCH_CLEAN`, `TEST_FAILED`, `TEST_PASSED`), 6 events. Two enforcement layers: (1) per-turn schema filtering — `allowed_tools()` filters the inner `tool` enum, `allowed_action_types()` filters the outer `type` enum so the model literally cannot emit `verify_done` from `EXPLORE` or `emit_patch` from `POSTPATCH_CLEAN`; (2) state-handler check in the `verify_done` branch as a defense-in-depth. `emit_patch` dedup clears on every transition. `MAX_PATCH_RETRIES = 5` consecutive engine failures from `CAN_RETRY` raises `VerifyPhaseExhausted` → graceful `VerifyResult(verified=False)`. The model gets its current state explained each turn via `sm.state_description(iteration, error_summary, failure_summary)` injected into the user payload's `instruction` field.
- **Command-only steps** (`_is_command_only_step` in `tools/loop.py`): a step with empty targets (or folder-only targets like `tests/`) has nothing to patch — its goal is running commands ("run the full test suite"). It starts the SM in `POSTPATCH_CLEAN` (EXPLORE's only exits are patch events, so it would otherwise be trapped) with `command_only=True`: `verify_done` is gated behind `TEST_PASSED` (no "nothing to run" skip — the commands ARE the step), reads target the shadow from turn one, and the verify budget applies. On failure the model may still `emit_patch` — empty targets route novel writes through the scope-extension gate — or escalate via `revision_needed`. The chat classifier routes run/build/verify requests to `large_change` (NOT qa) so they become executable tasks. **Contract caveat:** `PlanStepSchema.targets` in editor-client must stay `.min(0)` — a `.min(1)` silently broke the ReviewCard for command-only plans (`getTaskResult` Zod-threw on every poll; the live slot never rendered).
- **testing_strategy vs test_command**: the planner sets `testing_strategy` on every code step as a natural-language hint (e.g. "run pytest on test_auth.py"). `test_command` is only set when the test file is itself a target of that step — this prevents running stale tests when the import hasn't been updated yet. The execution agent uses `testing_strategy` to discover the right command when `test_command` is absent.
- **read_file phased target**: in the explore phase `read_file`/`search_code` read the **real workspace** (pre-patch). Once the SM transitions out of `EXPLORE` (i.e. the first patch has been applied), `ToolRegistry.use_shadow_for_reads()` is called and subsequent reads return the **shadow workspace** — the model can see exactly what its patches produced before re-patching.
- **Pre-existing failure normalization**: `_normalize_error_message` in `engine.py` fingerprints pytest and cargo test output so that failures present before patching are filtered from the post-patch baseline comparison. Cargo failures are identified by extracting failed test names from the `failures:` block; shadow paths (`.agentd/shadows/task-xxx/`) are stripped before comparison.
- **Delta replan**: a `revision_needed` signal from the execution agent triggers `PlanningAgent.revise()`, which explores the workspace and emits targeted step revisions without restarting the task. Budget: `max_delta_replans` (default 3).
- **Milestone snapshot**: at every `AWAITING_PLAN_APPROVAL` transition the engine serializes the full task state into `plan_approval_snapshot`. Used verbatim to reconstruct the exact plan-review state for resume rollbacks.
- **Step execution** is bounded. Each step uses `completed_step_ids` to skip already-done work; failed steps checkpoint the shadow back before giving up.

### Tier B — lifecycle control & durable telemetry

These four features share one in-memory mechanism, the **`TaskControl`** channel (`orchestrator/task_control.py`: `abort` event + `abort_revert` flag + live `step_review_auto_accept`). `AgentOrchestrator._task_controls: dict[str, TaskControl]` holds one per running task; registered in `continue_task`/`resume_task` right before `_execute_plan`, released in `_execute_plan`'s `finally` (single owner). `_execute_plan` only **reads** the control (never creates one). Single-process asyncio ⇒ check+set with no `await` between is race-safe (same pattern as `_in_flight_*`).

- **Cooperative abort (F12)** — `POST /v1/tasks/{id}/abort {revert}` sets `control.abort_revert` then `control.abort.set()`; it does NOT touch the shadow/status (returns 409 if no control = task not running). The loop polls `control.abort` at the top of each step (`_execute_plan`) AND between ReAct iterations (`ToolLoop`, via the optional `abort` event — passed ONLY to the step-execution loop, never the inline-change loop) and raises `TaskAborted`. The `except TaskAborted` handler owns the unwind: rollback if `abort_revert`, clean shadow, transition `ABORTED` **in place on the caller's object**, write a ✗ stopped/reverted breadcrumb. Because `_partial_promote` runs *after* a step returns, a mid-step abort promotes nothing of the in-flight step. `/cancel` stays as-is for queued/terminal tasks (its route-side shadow free is unsafe for a running task — that's what `/abort` fixes).
- **True revert at reject/abort (F8)** — a pinned **pre-execution checkpoint** (`execution_state.pre_execution_checkpoint`) is captured at `_execute_plan` start under a `_baselines/<task_id>/` root that `prune_checkpoints` never scans (so it survives until terminal; cleared in the terminal `finally` via `_clear_pre_execution_checkpoint`). `_rollback_to_pre_execution(task)` = `_restore_shadow_checkpoint(baseline)` then `workspace_manager.promote(task)` — an exact rollback reusing existing machinery (promote copies modified files present in the shadow and **deletes** those absent, keyed on `task.modified_files`). `POST /reject` now performs this true revert → `ABORTED` (was "keep changes"); the ReviewCard's "Discard all changes" maps to it. Accept stays `→PROMOTING→promote→SUCCEEDED` (the final promote reconciles step-deleted files, so it is NOT dropped — deviation from the original spec).
- **Durable telemetry (F9)** — `FailureSummary`/`RunSummary` on `TaskRecord`. `_finalize_run_summary` runs at every terminal (`_execute_plan` finally) **and at `READY_FOR_REVIEW`** (so the ReviewCard shows "N of M" durably on reload, before accept/discard) plus the accept/reject/cancel routes. `failure_summary` is written richly at the FAILED except-site and via a finally fallback (always present on FAILED). Exposed via `resolve_live_state` (FAILED/ABORTED → `failure_summary`; `run_summary` whenever present) and `TaskResult`/`TaskView`. The extension's ephemeral `runDeviations`/`lastStepStarted`/`lastPatchError` are now a live-feel fallback the durable copy supersedes on reload.
- **Dynamic review preference (item 5)** — `POST /v1/tasks/{id}/review-pref {auto_accept}` mutates `control.step_review_auto_accept`; `_execute_plan` re-reads it (per step, falling back to the record value) instead of the frozen `TaskRecord.step_review_auto_accept`. Flipping to auto-accept while a step gate is **pending** resolves that gate as accept via `resolve_pending_step_review` (fires the same `_pending_step_decisions` future as `/step-decision`). The composer checkbox stays enabled during execution and posts both directions.
- **`_write_chat_completion` is ABORTED-aware** — returns silently for `ABORTED` so the `finally` never writes "Execution failed: <diag>" over the abort breadcrumb (the `e7b5f39`-class stale-completion bug).

### Task narrative (LLM-authored run summary)

Distinct from the deterministic `run_summary` (counts): `TaskNarrative` (`{outcome, headline, points}`) is an LLM-authored story of the run, for the Review/Error cards AND as next-chat-turn context. Spec/plan: `docs/superpowers/specs|plans/2026-06-13-task-narrative*`.
- **Append-only event log:** `execution_state.run_events: list[RunEvent]` (`kind: step_done|step_failed|replan`), appended in `_execute_plan` (`_append_run_event`) at the step-complete (after promote+mark), step-exhausted, repair-complete/fail, and replan (BEFORE `_apply_revision`) sites. **Never pruned** — a delta replan's reverted steps keep their `step_done` events, so the narrative tells the whole story; the log is decoupled from `completed_step_ids`/`x of n` (which a replan moves) and immune to plan growth/shrink.
- **Per-step note is FREE + RICH:** a `step_summary` field on the `verify_done` action (`AGENT_STEP_RESPONSE_SCHEMA`) → `VerifyResult.step_summary` → `StepRunResult.step_summary` → the `step_done` event's `note` (deterministic `edited <files>` fallback when the model omits it; capped 1500 chars). The per-step note is deliberately a **detailed account** (not a finished one-liner) — raw material the end summarizer distills, so neither layer is redundant.
- **Synthesis:** `ReasoningEngine.summarize_run(...)` (one structured call; `ReasoningEngineImpl` + `narrative_prompts.py`; `ScriptedReasoningEngine` takes a `run_narrative` kwarg). Called in `_finalize_task_narrative` at the `_execute_plan` finally — at **READY_FOR_REVIEW** (outcome `succeeded`, so the ReviewCard shows it pre-accept) and at FAILED/ABORTED. **Best-effort** (try/except): a synthesis failure or an engine lacking `summarize_run` never fails the task (narrative stays `None`). Discard-after-review does NOT regenerate (v1).
- **Exposure/consumption:** on `TaskRecord.task_narrative`, surfaced via `resolve_live_state` + `TaskResult`/`TaskView`; `_write_chat_narrative` persists it as a durable `agent/text` transcript message (rides `thread.messages → history` into explore/QA next turn); `_find_recent_task` adds it to the recent-task dict for the classifier (resumable tasks only). Frontend: `TaskNarrativeSchema` (editor-client), forwarded to `renderLiveReview`/`renderLiveError`, rendered as headline+points on ReviewCard/ErrorCard.

## Resume / Rollback (child task pattern)

`POST /v1/tasks/{id}/resume` creates an **immutable child task** linked via `resume_of_task_id`. The parent is never mutated.

| Stage | Child starts as | What fires |
|-------|-----------------|------------|
| `plan` | `QUEUED` | `orchestrator.run_task()` (full re-plan) |
| `feedback` | `AWAITING_PLAN_APPROVAL` (snapshot state) | nothing async — user calls `/plan/feedback` on child |
| `execute` | `PLANNED` (current plan + completed_step_ids copied) | shadow cloned, `orchestrator.resume_task()` |

Concurrency guards: `_in_flight_feedback` and `_in_flight_resume` are closure-scoped sets in `build_router`. Check+add with no `await` in between is race-safe in asyncio.

## Architecture Details

### Python backend (`agentd/`)
- `api/routes.py` — all FastAPI routes; `build_router()` closes over store/orchestrator/workspace_manager
- `domain/models.py` — all Pydantic models: `TaskRecord`, `TaskBudget`, `TaskUsage`, `TaskExecutionState`, `AgentToolTrace`, `ToolCall`, `ToolResult`, `DeltaReplanRequest`, `PlanningResult`, `PlanRevisionResult`, `RevisedStep`, `TaskMilestoneSnapshot`, `ResumeTaskRequest`, etc.
- `domain/state_machine.py` — `transition()` validates all status changes; direct `store.create()` with a pre-set status bypasses it (used for child task creation)
- `orchestrator/engine.py` — `AgentOrchestrator`: `run_task()`, `continue_task()`, `resume_task()`, `_execute_plan()`; orchestrates PlanningAgent and ToolLoop
- `orchestrator/scripted_engine.py` — deterministic engine for testing (replays fixed responses for all three reasoning methods)
- `planning/agent.py` — `PlanningAgent`: thin coordinator; `generate_plan()` and `revise()` both delegate to `PlanningLoop`
- `planning/loop.py` — `PlanningLoop`: explore-then-commit ReAct loop; calls `create_planning_step()` per iteration; returns `PlanningResult` or `PlanRevisionResult`
- `planning/registry.py` — `PlanningToolRegistry`: read-only tools for the planning loop (`search_code`, `read_file`, `list_directory`, `search_semantic`, `query_graph`); also exposes `_render_query_result` used by both registries
- `retrieval/graph_walker.py` — `GraphWalker`: in-process BFS over `index-snapshot.json` backing the `query_graph` tool. File seed (`path`) → distinct neighbour files grouped by direction; symbol seed (`path:Symbol`) → symbol-level Calls/Imports/References/Inherits/Implements with line numbers. mtime-cached, thread-safe; `GraphWalkerSnapshotError` for unreadable snapshots
- `planning/prompts.py` — system prompt, user payload builder, and `PLANNING_STEP_RESPONSE_SCHEMA` (discriminated union: `tool_call | emit_plan | emit_revision`)
- `tools/loop.py` — `ToolLoop`: ReAct execution loop per plan step; calls `create_tool_step()` per iteration; returns `PatchResult` or `PlanHandoff`; `broadcast_key` param routes SSE events to a custom channel (used by inline change path to send to chat channel instead of task channel); `skip_verify=True` skips verify phase (used by inline changes)
- `tools/registry.py` — `ToolRegistry`: tools for the execution loop (`search_code`, `read_file`, `run_command`, `search_semantic`, `query_graph`); enforces path traversal protection and shell allowlist
- `tools/search.py` — `search_code` (ripgrep) and `search_semantic` (vector index query)
- `tools/files.py` — `read_file` with line range support and path traversal rejection
- `tools/shell.py` — `run_command` with configurable allowlist and timeout
- `reasoning/contracts.py` — `ReasoningEngine` Protocol: `create_plan()`, `create_patch()`, `create_tool_step()`, `create_planning_step()`, plus critique methods
- `reasoning/engine.py` — `ReasoningEngineImpl`: default implementation wiring prompt builders to providers
- `reasoning/tool_prompts.py` — system prompt, payload builder, and `AGENT_STEP_RESPONSE_SCHEMA` (discriminated union: `tool_call | emit_patch | revision_needed`) for the execution tool loop
- `reasoning/prompt_builder.py` — prompt builders for plan/patch calls
- `patch/engine.py` — `PatchEngine`: applies `patch_ops` (create_file, search_replace, replace_node, apply_diff) on the shadow workspace
- `providers/` — one file per model provider (anthropic, openai, gemini, groq, huggingface, watsonx, openrouter); all implement `ReasoningEngine` in `reasoning/contracts.py`
- `retrieval/` — reads `index-snapshot.json` artifacts; injected into planning context as `retrieval_context`
- `storage/` — `InMemoryTaskStore` (tests) and SQLite store (production); both implement the `TaskStore` protocol
- `workspace/shadow.py` — `ShadowWorkspaceManager`: `prepare()` (full copy), `prepare_lightweight()` (shallow copy for inline changes — no git init, just file tree), `clone()`, `promote()`
- `validation/` — runs configurable validation commands (pytest, tsc, cargo test) on the shadow; returns `ValidationResult`
- `orchestrator/broadcaster.py` — `EventBroadcaster` (renamed from `PatchEventBroadcaster`): keyed by `channel_id` (any string — task_id for task SSE, a UUID for chat SSE); single streaming mechanism for all SSE in the system
- `chat/agent.py` — `ChatAgent`: explore → classify → respond coroutine; `_draft_plan_markdown()` for small_change path
- `chat/storage.py` — `ChatThreadStore`: SQLite multi-thread storage; `resolve_diff_card(inline_task_id, resolution)` patches diff card resolved state; `append_plan_card(thread_id, task_id, md)` appends a plan version (dedups vs the task's latest) — see "Live cards" below
- `chat/classifier.py` — `IntentClassifier`: qa / small_change / large_change
- `chat/models.py` — `ChatMessage`, `ChatThread` dataclasses
- `chat/app_factory.py` — test-only `build_app()` — keeps provider transport init out of import path; uses `ScriptedReasoningEngine` and `_NullTransport`

### TypeScript packages
- `editor-client/src/contracts/task-contracts.ts` — canonical Zod schemas + `BackendTaskClient` interface + `StreamEvent` discriminated union (covers both task SSE and chat SSE events); source of truth for all API shapes
- `editor-client/src/client/http-backend-client.ts` — `HttpBackendClient`: snake_case↔camelCase mapping, all API calls
- `editor-client/src/domain/` — `types.ts`, `schemas.ts`, `task-state.ts`
- `vscode-extension/src/controller.ts` — `AiEditorController`: orchestrates all user actions; pure business logic, no VS Code API dependencies
- `vscode-extension/src/extension.ts` — VS Code activation, command registration, wires controller to UI
- `vscode-extension/src/review-panel.ts` — WebView panel for task review

**Build order**: `vscode-extension` types off `editor-client`'s compiled `dist/index.d.ts`, not source. After changing `editor-client`, run `npm run -w @ai-editor/editor-client build` before running `vscode-extension` typecheck — otherwise you'll get stale-type errors that don't exist in source.

### Chat interface
Routes registered when `chat_agent` is non-None in `build_router()`:
- `GET /v1/chat/threads?workspace=<path>` — list threads for a workspace. Summaries are enriched at query time (no schema change): `message_count`, `updated_at` (last message timestamp, falls back to `created_at`), and `status` — a history-chip value (`running | review | done | failed | null`) derived from the thread's `active_task_id` task via `thread_status_chip` in `chat/live_state.py`
- `POST /v1/chat/threads` — create thread (`{workspace, title}`)
- `GET /v1/chat/threads/{thread_id}` — get thread with messages
- `POST /v1/chat/threads/{thread_id}/message` — SSE stream. Body: `{content, step_review?}` — `step_review` (bool) is the composer's "Review each step" toggle, sent with every message; it's applied only when the turn creates a task (`large_change`): `step_review_auto_accept = not step_review`, frozen into the TaskRecord at creation (flipping the checkbox mid-task changes nothing). When omitted/non-bool the `AI_EDITOR_STEP_REVIEW_AUTO_ACCEPT` env default applies

#### Reactive controller (`AI_EDITOR_CHAT_CONTROLLER=1`) — supersedes the legacy `ChatAgent` flow below

Merged on `main` via PR #2 (2026-06-21), flag-gated. `controller_factory.select_chat_handler` returns `ChatController` (`chat/controller.py`) instead of the legacy `ChatAgent` when the flag is truthy; both expose the same `handle_message(...)` surface + `_store`/`_broadcaster` attrs. **There is no explore→classify→route pre-classification.** Each turn runs one `ControllerLoop` (ReAct, mirrors `PlanningLoop`; `chat/controller_loop.py`): explore tools → **decide** one action — `answer` | `clarify` | `propose_mode` | `edit` | `submit_changes`. Phases are owned by `ControllerPhaseSM` (`chat/controller_phase.py`): `DECIDE → EDIT | EXPLAIN`; per-phase action-type filtering + a tight `oneOf` response schema on providers with `supports_oneof_grammar` (flat fallback otherwise). Edits run in a `TurnEditSession` (ACID shadow, **instant-promote on accept**, restore on reject). Prompts in `chat/controller_prompts.py`.

- **Reactive routing of a "large feature":** for an ambitious request (e.g. "build X") in an empty workspace the controller does NOT force inline; it emits `propose_mode` → a **ModeGate** carrying `{plan_sketch, recommended, reason, options:[{mode,label,description}]}` (typically `edit` vs `explain`, recommending `edit`). Accepting `edit` (via `POST /mode-decision` or the live ModeGate buttons — labels are **model-authored**, e.g. "Create index.html now") starts the EDIT phase, which writes the file into the turn shadow and parks at an **EditGate** when review is on; **Accept = instant-promote** to the real workspace. (Verified live this session: single-file Three.js game built end-to-end on TQP `qwen3.6:35b-a3b-q4_K_M` through ModeGate→EditGate→promote.)
- **Class-A live gates:** the active gate is on the thread as `pending_controller_gate` and surfaced by `GET /v1/chat/threads/{id}/live` → `{turn_active, pending_gate:{kind: mode|edit|clarify|command, payload}, status, plan, failure_summary, run_summary, task_narrative}`. Same render-from-`/live`, durable-breadcrumb model as the task gates. Turns are detached/durable (`/message`, `/mode-decision` & `/clarify-decision` 409-guard + subscribe-relay; `POST /stop` to halt; live-resume re-subscribes on reload).
- **Clarify gate (sibling of ModeGate):** the `clarify` action is NOT a chat bubble — it renders as a Class-A live gate (`PendingGate(kind="clarify")`). The action schema carries a model-authored `options` array (2-4 candidate answers); the UI (`ClarifyGate.tsx`) appends a free-text "Something else…" escape and disables the composer ("Answer on the card above"). `_finish` routes clarify → `_present_clarify_choice` (sets the gate, persists pills, NO question bubble — the question lives in the card). Resolved by `POST /v1/chat/threads/{id}/clarify-decision {answer}` (streamed, mirrors mode-decision) → `ChatController.resolve_clarify` writes ONE combined `❓ q → a` breadcrumb and re-enters the loop with the answer as the user reply. **EDIT-resume:** a clarify raised mid-EDIT carries `resume_phase="EDIT"` in the gate payload (resolve_clarify reads it to resume in EDIT) — this replaced the old `_edit_clarify_pending` side-map. `PendingGate.kind` gained `"clarify"` in BOTH `chat/models.py` AND the editor-client Zod enum (the `.min(1)`-class footgun). Spec+plan: `docs/superpowers/specs|plans/2026-06-26-clarify-interactive-gate*`. (Verified live in-situ: an ambiguous "which tax module?" prompt on TQP produced the gate with options `[src/tax.py, src/taxutil.py]`.)
- **GOTCHA — the controller is workspace-frozen at startup.** `main.py` reads `AI_EDITOR_WORKSPACE_PATH` (default cwd) **once** and passes it into `select_chat_handler`; `ChatController._workspace_path` is used for the shadow root, all file ops, and retrieval **on every turn**. The thread's `workspace_path` column is stored and used for thread *listing* but is **ignored per-turn** — one backend process serves exactly one editing workspace. To test a specific workspace, point both the backend env AND the dev-host's opened folder at the same path. And **always quote `--workspace`** to `start-backend.sh`: an unquoted path with a space corrupts `$WORKSPACE` and every derived var (`DB_PATH`, `SHADOW_ROOT`, `ARTIFACTS_ROOT`, …) to the pre-space prefix.
- **Debug artifacts:** `<workspace>/.agentd/artifacts/chat/<thread_id>/<turn_id>/controller-turn-NN.json` (exact per-iteration LLM bytes) + `turn-trace.json` — the controller analog of the task path's `plan-turn`/`tool-trace`.
- **Todo ledger (multi-feature completion):** in EDIT/DECIDE the controller can call the `write_todos` tool (a `TodoToolSource` over a per-request `TodoLedger`, `chat/todo_ledger.py` + `chat/todo_source.py`; 5 states pending/in_progress/done/blocked/cancelled, full-list-rewrite) to track a large/multi-part change. `submit_changes` is **hard-blocked** in `ControllerLoop` while any item is pending/in_progress (blocked/cancelled/done never deadlock it; the block is NOT counted as malformed — only `max_iters` bounds it). The status is re-surfaced into the payload tail (`todo_status`) every iteration; persisted on `chat_threads.controller_todo_json` (request-scoped — survives DECIDE→EDIT + clarify resume; cleared on terminal); exposed via `/live` (`ThreadLiveState.todos`, `ChatThread.controller_todos`) and rendered as a flat read-only `TodoCard` in the live slot (**`todos` MUST be in controller.ts `lastLiveSignature`** or updates are deduped away). Discretionary: model creates it only for multi-part work (steered via the propose_mode "enumerate every part" rule + a TODO LIST POLICY block; `done` requires evidence cited in `note`). `AI_EDITOR_CONTROLLER_MAX_ITERS` (default 500) is the loop cap — the real within-turn limit is the context window until the agent-memory module lands. **No completion backstop yet** (deferred, spec §7); op-deltas/nesting/action-form/manual-approval/event-log deferred (spec §9). Spec+plan: `docs/superpowers/specs|plans/2026-06-23-controller-todo-ledger*`.
- **Task subsystem flag (`AI_EDITOR_TASK_SUBSYSTEM`, default OFF):** gates the entire task-based path. OFF (default): the controller offers only `edit | explain`; `create_task`/`resume` teaching is omitted from the controller system prompt (`format_controller_system_prompt(task_subsystem_enabled=…)` swaps `_PROPOSE_MODE_MODES_{ENABLED,DISABLED}`), the offered-mode set in `ControllerLoop` is `{edit, explain}` (`_propose_mode_correction(resp, allowed_modes)`), `ChatController.resolve_mode` rejects task modes, and the extension hides the task UI (`startTask`) via the `aiEditor.taskSubsystemEnabled` `when`-context key fed by `GET /v1/config`. `/v1/tasks` routes stay registered but **dormant** (no hard-404 guard). OFF requires `AI_EDITOR_CHAT_CONTROLLER=1` (startup WARNING otherwise — `warn_if_incoherent_flags`). Inline `edit` is now the PRIMARY path for changes of ANY size (large via the todo ledger), not a small-change-only path. Resolver: `chat/controller_factory.py::is_task_subsystem_enabled`. Existing `create_task` tests opt in via `monkeypatch.setenv("AI_EDITOR_TASK_SUBSYSTEM","1")` / `task_subsystem_enabled=True`. Deferred: turning the task path into a sub-agent execution path. Spec+plan: `docs/superpowers/specs|plans/2026-06-26-flag-gate-task-subsystem*`.

#### Project instructions (AGENTS.md) + prompt files (P1, copilot-parity roadmap)

Two independent, flag-gated parity features. Spec/plan: `docs/superpowers/specs/2026-06-29-project-instructions-prompt-files-design.md` + `…/plans/2026-06-29-project-instructions-prompt-files.md`.

- **Project instructions (backend, controller-only):** `agentd/instructions/loader.py::ProjectInstructionsLoader` is an mtime-cached reader for `<workspace>/AGENTS.md` (mirrors the GraphWalker cache discipline — cheap NOOP until the file's mtime changes, so an edit **self-updates mid-session without a restart**; thread-safe; best-effort — any IO error degrades to `None` so instructions never break a turn; size-capped). `DefaultReasoningEngine` takes an optional `project_instructions_loader` and, in `create_controller_step`, resolves `loader.load()` and passes it to `format_controller_system_prompt(..., project_instructions=…)`, which appends a labeled `_INSTRUCTIONS_BLOCK_TEMPLATE` (uses `.replace`, **not** `.format` — AGENTS.md may contain literal `{ }`). `controller_factory.select_chat_handler` builds the loader from the **frozen** `workspace_path` when `is_project_instructions_enabled()`. **AGENTS.md is the ONLY source** (no `.github/copilot-instructions.md` fallback, no nested files); injection is **controller-only** — the planning/task prompt path is untouched. Env: `AI_EDITOR_PROJECT_INSTRUCTIONS` (default **ON**, kill-switch — off only for `0/false/no/off`) + `AI_EDITOR_INSTRUCTIONS_MAX_CHARS` (default `16000`; over-budget truncates with a marker + `logger.warning`).
- **Prompt files (frontend-only, expand-before-send):** `.ai-editor/prompts/<name>.md` snippets expanded inline in the composer via `/name [args]`. **No backend route, no editor-client contract change.** Pure helpers in `apps/vscode-extension/src/prompt-files.ts` (`substitutePrompt` — `$ARGUMENTS` = full arg string, `$1..$N` = whitespace-split positional, unfilled → empty; `parseSlashCommand`; `listPromptNames`; `loadPromptBody` — rejects path-traversal names). `controller.ts` (stays vscode-free; node `fs` ok) exposes `listPrompts()`/`expandPrompt(name,args)→{found,text}` over `<ws>/.ai-editor/prompts`. Host plumbing in `chat-panel.ts` routes `listPrompts`/`expandPrompt` webview messages → posts `promptList`/`promptExpanded` (handlers wired in `extension.ts`). `InputArea.tsx` intercepts an un-expanded `/name` on Enter: it posts `expandPrompt` instead of sending; the `promptExpanded` reply fills the draft so the user reviews/edits, then a second Enter (now non-slash) really sends (`found=false` is a soft no-op). The webview keeps a local mirror `webview-ui/src/slash.ts` of `parseSlashCommand` (it's a separate Vite bundle that doesn't import the extension's `src/`).

SSE event types from the chat message endpoint:

| Event | Description |
|-------|-------------|
| `chat_agent_thinking` | Thinking status text (explore phase, classifying, drafting…) |
| `explore_tool_call` | A tool call during explore phase |
| `intent_classified` | `intent`, `likely_targets`, `files_examined` |
| `tool_call` | Execution agent tool call during ToolLoop; payload includes `tool`, `thought`, `args` dict (use `args.path` for filename display) |
| `patch_applied` | A patch op was applied in the shadow workspace |
| `diff_ready` | Inline change ready; payload has `inline_task_id`, `diff_entries` |
| `thread_title_updated` | Thread auto-named from first user message; payload: `{thread_id, title}` |
| `chat_response` | QA answer chunk |
| `chat_breadcrumb` | Durable record of a resolved gate/plan action (`{text, task_id}`), pushed live so it lands in history without a reload. Persisted as an `agent/text` message with `metadata.breadcrumb=true`. Broadcast to BOTH the chat channel and the task channel. |
| `plan_card` | Read-only plan version (`{task_id, plan_markdown}`), pushed to the transcript live. Persisted via `append_plan_card`. |
| `chat_done` | Turn complete |

`ChatAgent` flow per message (legacy path, `AI_EDITOR_CHAT_CONTROLLER` off — see reactive controller above for the flag-on path):
1. **Explore phase** — up to 5 tool calls via `PlanningToolRegistry`; results go into `context`
2. **Classify** — `IntentClassifier` → `qa / small_change / large_change`
3. **Respond** — `qa` → `generate_text` answer; `small_change` → `run_inline_change`; `large_change` → `create_task_from_chat`

`ChatMessage.metadata` fields used at runtime:
- `thinking_log: list[str]` — all thinking/tool-call snippets from the agent turn; stored on both QA messages and diff cards; rendered as a collapsible "Show thinking" pane
- `diff_entries: list[DiffEntry]` — files changed in an inline change (on `diff_card` messages). Each entry carries `unified_diff`: capped diff text (400 lines AND 24k chars per file, whichever hits first, truncation marker appended — `_cap_unified_diff` in `engine.py`) rendered as tabbed `DiffPanes` in DiffCard/StepGate; the full diff stays available via the native editor diff against `temp_path`. Entries without `unified_diff` (pre-cap persisted messages) fall back to the `FileRow` list
- `resolved: "applied" | "discarded"` — patched in by `resolve_diff_card()` after promote/discard; controls rendered state of diff card buttons
- `taskId: str` — inline task id on diff card messages
- `step_id` / `step_title` — on step-review record `diff_card` messages (`_write_chat_step_diff_record`): the durable transcript copy of a resolved step gate, persisted with `resolved` pre-set (`applied`/`discarded`) so it renders inert, written BEFORE the decision breadcrumb (reads: diff → ✓ accepted). NOT broadcast live — the live StepGate card already showed the diff. Auto-accepted steps get a `✓ Step completed: <goal>` breadcrumb instead of a record
- `breadcrumb: true` — marks a gate/plan-action transcript breadcrumb (rendered as a normal agent text line)
- `tool_events: list[dict]` — durable tool pills in the webview's `ToolEventView` shape (`{id, tool, args, thought?, source, output?, isError?, done}` — frontend-camelCase keys). Live pills stream over SSE and die on reload; this is the persisted record (`chat/tool_events.py::trace_to_tool_events`, outputs capped at 4000 chars). Writers: engine `_write_chat_tool_events` (one pills-only `agent/text` message per step attempt with `step_id`/`step_title`, and per planning round — initial, feedback regen, delta replan), `ChatAgent` (explore pills on QA/clarify messages, pills-only message before task cards), and inline-change diff cards (explore + execution, re-id'd). Deliberately NOT broadcast live — the raw `tool_call`/`tool_result` events already render the live pills and there's no shared id to dedup against the bubble.

`ChatThreadStore.resolve_diff_card(inline_task_id, resolution)` scans all threads for the matching `diff_card` message by `task_id` and patches `metadata.resolved` in-place — called from the promote and discard API routes.

#### Live cards, gates & breadcrumbs (Class-A model)
The chat UI separates **interactive** affordances from the **durable transcript** — conflating them caused a cluster of bugs (cards vanishing with no record, reappearing on reload, 409 on re-accept, a crash).
- **Interactive gate/plan cards** render *only* in the pinned `/live` slot (`renderLiveGate` / `renderLivePlan` in `chat.js`), driven by `GET /v1/chat/threads/{id}/live`. `resolve_live_state` (`chat/live_state.py`) derives the one active gate from the task **status** via `_GATE_FIELD` (`AWAITING_{COMMAND,STEP,SCOPE,VALIDATION}_DECISION` → `pending_*` field) and the plan only at `AWAITING_PLAN_APPROVAL`. The slot auto-clears when status advances. The 1s `liveStateTimer` poll is the durable render path (survives reload + resume task-id churn); SSE events only *poke* a re-fetch. Step-review cards in chat have **no** SSE poke — they render purely from the poll.
- **Durable records** are persisted chat messages, also broadcast live so the transcript fills in real time (`/live` carries gate+plan only, never the message list): a `✓/✗/↻` **breadcrumb** (`write_chat_breadcrumb` → `chat_breadcrumb` event + `agent/text` message) for every resolved gate/plan action, and a read-only **`plan_card`** version.
- **`plan_card` is a version history.** `ChatThreadStore.append_plan_card(thread_id, task_id, md)` appends a new version (feedback regenerates → old plan stays, new appends after the `↻ feedback` breadcrumb) but skips a write identical to the task's current latest (collapses the double-writer / re-presentation). `_write_chat_plan_card` is the **single** writer (the chat agent no longer writes one) and broadcasts to **both** the chat channel (presentation turn listens here) and the task channel (execution stream picks up a feedback-regenerated version on approval). Frontend dedups by task+content signature (`planSig` in `chat.js`), so versions coexist while a re-delivery (live + reload, or both channels) never duplicates. Interactive Implement/Feedback buttons live ONLY in the `/live` slot — persisted `plan_card` messages are **read-only**.

**Gate invariants (each caused a real bug):**
- **Gates clear in place.** `_pause_for_step_review`, `_pause_for_validation_decision`, (and the scope/command callbacks) must reset `pending_*` + transition the **caller's** task object — NOT re-fetch a fresh record and reset that. The validation gate variant: `run_task`'s `finally` writes the chat completion line from its own local, which `return await _pause_for_validation_decision(...)` never rebinds — a re-fetched copy left it stale at `AWAITING_VALIDATION_DECISION` and the transcript got "Execution failed: <diagnostics>" after an accept. `transition()` mutates in place and the caller (`_execute_plan`) holds the reference; re-fetching a divergent object leaves the caller stale at `AWAITING_STEP_REVIEW`, which re-saves the stale gate (card reappears on reload, 409 on re-accept) and crashes the next transition (`Invalid transition: AWAITING_STEP_REVIEW -> VALIDATING`). Safe because the decision routes (`/step-decision`, `/scope-decision`, …) only `future.set_result(...)` — they never mutate/persist the task, so nothing changes it during the `await`.
- **Gate-raise `except ValueError` swallows ONLY true re-entrancy** (`task.status == target`); re-raise otherwise. An invalid-source transition (e.g. raising a scope gate while parked in `AWAITING_STEP_REVIEW`) silently swallowed strands `pending_*` behind a stale status, so `/live` renders the wrong gate (or none) and the task blocks until its decision timeout (scope default 600s).

**`/live` poll dedup-signature invariant (controller.ts `pollThreadLiveState`):** the 1s poll is the durable backstop for any *missed SSE terminal* (`chat_done`, gate-clear). It dedups on a `lastLiveSignature` (JSON of `{taskId, status, turnActive, gate, plan, runSummary, narrative, failure}`) to avoid webview churn, then `return`s early when unchanged. **INVARIANT: every durable signal consumed after the dedup gate MUST be in the signature** — else its transition is swallowed and the webview never learns. This bit three times: `runSummary`/`narrative`/`failure` (Review/Error card never updated) and **`turnActive`** (a controller chat turn has no task, so `status`/`gate`/`plan` stay null the whole turn → the `true→false` end transition was deduped away → `sendLiveStatus(...,false)` never sent → composer wedged on "Agent is working…" forever). The one **documented exception** is the READY_FOR_REVIEW `getTaskResult` fields (modifiedFiles/shadowPath/plan), safe only because they're immutable at that terminal status. Reconciliation has a second half in the webview reducer (`useAppState.ts` `liveStatus` case): on `turnActive=false` **with `status===null`** (controller turn ended) it seals any lingering streaming bubble (no data loss) AND re-enables input *even with no bubble* (an error-before-broadcast turn has none); gated to `status===null` so it never touches input during task execution.

### Memory harness (Phase 1 compaction + Phase 2 recall/consolidation)

Self-contained module (`agentd/memory/`): `harness.py` (the only unit the loops see), `compactor.py` (token-trigger eviction + summarize), `store.py` (SQLite — `compaction_segments`, `anchored_summaries`, + P2 `memories`/sqlite-vec/FTS5), `consolidator.py` + `recall.py` + `embedder.py` + `tool_source.py` (P2), `models.py`, `config.py`. OFF by default (`AI_EDITOR_MEMORY_ENABLED`); P2 also needs a workspace scope (wired via `build_memory_harness(..., workspace_path=…)` in `main.py` + `controller_factory.py`).

- **Wiring:** both ReAct loops call `await self._memory_harness.prepare_turn(history, run_id)` at the top of each iteration and `history[:] = _prep.history` (in-place, same list object). Sites: `chat/controller_loop.py` (run_id = thread_id) and `tools/loop.py` (run_id = `{task_id}:{step.id}` — per-step, so one step's anchor never leaks into another). `NO_OP_HARNESS` is a byte-identical passthrough when disabled. **prepare_turn is best-effort** — any compactor exception is swallowed (memory must never break a loop iteration).
- **Budget model (the fracs are setpoints, NOT a partition):** `maybe_compact` fires when `history_tokens ≥ window_tokens × trigger_frac` (default 0.65); it evicts the oldest **whole turns** (lossless at turn boundaries via `_select_hot`) down to a hot floor of `window × hot_token_frac` (0.4), persists them as `compaction_segments` (BEFORE summarizing → lossless), and folds them into the `anchored_summaries` anchor. Steady state is a **sawtooth between 0.4 and 0.65**; the 0.35 above the trigger (up to the full window) is deliberate overshoot headroom, because compaction is only checked at iteration start and one fat turn can blow past the trigger mid-turn. `AI_EDITOR_MEMORY_HOT_TURNS` caps **message count**, not logical turns (misnomer; the token floor usually binds).
- **Anchor = running summary-of-summary:** each round feeds the prior anchor + newly-evicted text to `make_engine_summarizer` (`harness.py`). `upsert_anchor` bumps `version` per round (v1→v2→…). The injected head message is `[MEMORY] Summary of earlier conversation that was compacted:\n<anchor>`.
- **Summarizer hardening (weak-model failure modes, all fixed via TDD):** the summary call uses a **single-key** `{"transcript": <flat text>}` payload (a multi-key JSON dict shape gets echoed back verbatim by weak models), the prompt (`_SUMMARY_SYSTEM`) is a 9-section Claude-Code-style "note to your future self" requiring output wrapped in one `<summary>...</summary>` block, and the result is **extracted + validated**: `_extract_summary` pulls the block, `_is_echo` rejects empty/JSON-object output, and the summarizer **retries once then raises `SummarizerEchoError`** → the compactor degrades (keeps the prior anchor, marks `degraded`, logs) rather than persisting garbage. Carry-forward is **goal-relevant, not strictly lossless** — a fully-superseded file may be recency-triaged out (accepted by design; a durable file-ledger outside the LLM summary is the deferred fix).
- **Observability:** `compactor.maybe_compact` logs `[memory] compacted run=… anchor=vN evicted=K anchor_chars=…` on success (it previously logged only on failure). Both loops broadcast a `memory_compacted` SSE event (`{evicted, anchor_version}`) when `_prep.compacted` — typed in editor-client's `StreamEvent`, rendered by `controller.ts` as a live `🗜️ Compacted N earlier messages into memory (vK)` chat line.

#### Phase 2 — cross-session recall + write path

Spec/plans: `docs/superpowers/specs/2026-06-28-memory-harness-phase2-recall-design.md`, `docs/superpowers/plans/2026-06-28-memory-harness-phase2{a,b,c}-*.md`. Live-validated (see `docs/superpowers/2026-06-29-memory-phase2-live-smoke-plan.md`).

- **Data model (`store.py`, same `memory.sqlite3`):** `memories` (id, scope_kind/scope_id, kind, content, entities JSON, importance, bitemporal `valid_from`=event/`created_at`=ingestion, `valid_to`/`superseded_by` lifecycle, `source_kind`, `source_ref`, A+link `source_seq_lo/hi`) + a co-located **sqlite-vec** `vec_memories` (`float[384]`, bge-small) + an **FTS5** `memories_fts` mirror. **`global` scope is reserved but never written in P2** (workspace + thread only; workspace is the consolidation default).
- **sqlite-vec is guarded** — load wrapped in try/except → `_vec_enabled`; a missing extension degrades to FTS5-only and never crashes the store (Phase-1 compaction depends on it too).
- **Write path (`consolidator.py`): LLM proposes, Python disposes.** Background `Consolidator` runs one `generate_json` distill (Mem0-style few-shot prompt → `CandidateMemory{kind,content,entities,importance,contradicts?}`) then a deterministic, **await-free** post-process: embed (off the loop via `to_thread`), dedupe (cosine ≥ `MEMORY_DEDUP_THRESHOLD` 0.92 vs same kind+scope), supersede (LLM `contradicts` hint; **episodic never supersedes**), insert. Existing-context fed to the LLM is **capped** (top-20 by importance+recency) so the prompt can't grow unbounded. Deliberate path = the `remember()` tool. **Triggers** (all fire-and-forget, best-effort, refs held so tasks aren't GC'd): compaction events (distill the evicted slice via the A+link seq span), task terminal (deferred — dormant subsystem), and **edit-promoting controller turns** (`submit_changes`; QnA/answer/clarify excluded).
- **Read path (`recall.py`): `RecallEngine`** fuses semantic (sqlite-vec ANN, over-fetched `k*4` then live+scope filtered) + lexical (FTS5 BM25) + structural (entity overlap) + importance + exponential recency, each **min-max normalized**, with a relevance floor (`min_score`). Filters `valid_to IS NULL` + scope before scoring. `recall_grounded` optionally grounds the top 1-2 in the code graph (`query_graph`, best-effort). The harness fills `prepare_turn`'s recall slot **every turn, cached per query**; the loop drops `recalled_memories` into the **dynamic tail** of the payload (KV-safe, finding #13), omitted when empty.
- **GOTCHA — two recall bugs found live (both invisible to unit tests):** (1) a raw user query with paths/dots/colons/`AND`/`OR` is **not** a valid FTS5 MATCH expression and raised a syntax error that nuked the whole recall — `_fts_match_query` now tokenizes + quotes each term. (2) The recall **query source is `plan_context["goal"]`** (the current user message), NOT `history` — the message isn't in `history` on turn 1, so `prepare_turn(history, run_id, query=…)` takes it explicitly (falls back to a history scan). Recall failures log with `exc_info` now.
- **Tools + prompt teaching:** `MemoryHarness.memory_tool_source()` returns a `MemoryToolSource` (`remember` + `recall`, the latter only when a recall engine is wired); the controller registers it. `format_controller_system_prompt(..., memory_enabled=…)` (env-resolved via `is_memory_enabled()`) appends a MEMORY block teaching `recalled_memories`/`recall`/`remember` — gated like `task_subsystem_enabled`.
- **Embedder:** one shared `Embedder` (bge-small, unit-normalized, lazy load, degrade-not-raise) for the consolidator + recall; warmed in a daemon thread at build so the first turn doesn't eat the ~130MB load.

#### Phase 3 — reranker (3-A backend) + inspector panel (3-B frontend)

Spec: `docs/superpowers/specs/2026-06-29-memory-phase3-reranker-inspector-design.md`. Plans: `docs/superpowers/plans/2026-06-29-memory-phase3a-reranker-trace-backend.md` (backend) + `…-phase3b-inspector-panel-frontend.md` (frontend). 3.2 (global-prefs UI) deferred.

- **Reranker (`memory/reranker.py`):** local `sentence-transformers` CrossEncoder (`BAAI/bge-reranker-base`), **independent of `MEMORY_ENABLED`** (own flag `AI_EDITOR_MEMORY_RERANKER`, default OFF) and **degrade-not-raise** (model/lib absent → fused order, `available=False`). Slots into `RecallEngine` at the post-floor seam, **count-gated** (`AI_EDITOR_MEMORY_RERANK_MIN_CANDIDATES`, default 8) — reorders floor-passing candidates, never resurrects below-floor ones. `recall()` signature is **UNCHANGED**; `recall_with_trace()` does the work and `recall()` = `(await recall_with_trace(...))[0]`. Only the harness `_fill_recall` switched (every `_SpyRecall` test fake gained `recall_with_trace` in lockstep — same breakage class as P2's `prepare_turn(query=…)`).
- **Recall trace + persistence:** `RecallTrace`/`RecallTraceEntry` (`models.py`) capture per-candidate normalized signals (semantic/lexical/structural/importance/recency) + `fused_score` + `rerank_score` + `final_rank` + `injected`; entries cover **all** scored candidates (incl. below-floor `injected=false`), so a 0-candidate trace exposes the empty-query/FTS5 failure class directly. `TurnPreparation.recall_trace` is filled by `_fill_recall`; the **controller loop** persists it to `<workspace>/.agentd/artifacts/chat/<thread>/<turn>/memory-recall-NN.json` (best-effort).
- **Store browse helpers (`store.py`, read-only):** `list_memories(scope_kind, scope_id, kind=None, include_retired=False)`; `get_supersede_chain(memory_id)` (oldest→newest via `superseded_by`).
- **Three read-only GET routes (`api/routes.py`, gated by `is_memory_enabled()`):** `GET /v1/memory/inspect?thread_id=` → latest `RecallTrace` JSON or soft-empty `{entries:[]}`; `GET /v1/memory?scope_kind=&scope_id=&kind=&include_retired=` → `list[Memory]`; `GET /v1/memory/{id}/chain` → supersede chain. `/v1/config` gained `memory_enabled`. **GOTCHA (fixed 3-A):** the inspect route's artifact glob — `chat_turn_artifacts_root(thread_id, "", ws)` already returns `…/chat/<thread>` (`Path / ""` is a no-op join), so taking `.parent` over-strips to `…/chat` and the `base/*/memory-recall-*.json` glob probes one dir too shallow → always empty. Don't take `.parent`. (The soft-empty unit test masked it; `test_inspect_serves_persisted_trace` is the regression guard.)
- **Inspector panel (3-B, frontend):** a dedicated **`MemoryPanel`** is a **second Vite entry** in the React `webview-ui` (`memory.html` → `src/memory/{main,MemoryApp,RecallTraceTab,BrowserTab,types,vscodeApi}.tsx`), NOT the stale HTML-string "review-panel.ts" the spec named (chat fully replaced that with the `webview-ui` React app). `webview-ui` keeps **local mirror types** (it doesn't import editor-client). The host is split for testability: `src/memory-data.ts` (vscode-free — `handleMemoryMessage` + `MemoryDataSource`/`MemoryBrowseFilter`, unit-tested in the node-env vitest) and `src/memory-panel.ts` (the `vscode` `MemoryPanel` class, mirrors `chat-panel.ts` asset-rewrite+CSP). `controller.ts` stays **vscode-free**, exposing `memoryDataSource()`/`memoryThreadId()`/`memoryWorkspacePath()` (client built from the backend URL, session-independent like `attachToTask`); **`extension.ts` owns panel construction** (needs `context.extensionUri`). Command `aiEditor.openMemoryPanel` is gated by the `aiEditor.memoryEnabled` `when`-context fed from `/v1/config` (mirrors `taskSubsystemEnabled`). editor-client adds Zod `RecallTrace`/`RecallTraceEntry`/`MemoryView` + `getMemoryInspect`/`listMemories`/`getSupersedeChain` (snake→camel; routes return snake_case, signals keys pass through unmapped). Read-only; no live polling (Refresh button re-fetches).
- **Phase-3 config env vars:** `AI_EDITOR_MEMORY_RERANKER` (default off), `AI_EDITOR_MEMORY_RERANKER_MODEL` (default `BAAI/bge-reranker-base`), `AI_EDITOR_MEMORY_RERANK_MIN_CANDIDATES` (default 8).

### Retrieval pipeline
- `indexer-rs` writes `index-snapshot.json` with `nodes`/`edges`/`diagnostics`/`stats`
- `agentd-py` reads the snapshot per task via `retrieval/` module; if missing, auto-triggers one index run
- Retrieval context flows into `PlanningAgent.generate_plan()` as `initial_context` and into step execution via `patch_request_context`
- `graph_neighbor_files` (in the planner payload via `RetrievalContext.as_prompt_payload()`): files reached from the goal's matched/semantic seeds by one structural hop, surfaced as an initial reading list
- Stale/missing snapshots emit warning diagnostics but never block orchestration
- Both `PlanningToolRegistry` and `ToolRegistry` also give the agent live access to the workspace during their loops (not just the snapshot)
- **Gotcha — ignored ANCESTOR dirs silently disable indexing for a whole workspace.** `is_ignored_path` (`indexer-rs/src/service.rs`) matches its `IGNORED_DIRS` (`.git`, `node_modules`, `.venv`, `target`, `dist`, `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.agentd`, `.ai-editor`, `.tmp`) against components of the **full absolute path**, not the path relative to the workspace root. So if `--workspace` points *under* a dir with one of those names — e.g. a stress/smoke workspace at `.../.tmp/smoke-xxx` — **every file is filtered**: the watcher starts, LSP warms up, but the snapshot stays at 0 nodes and file-change events are ignored. The watcher is fine; the workspace location is the problem. Put real workspaces outside ignored-named ancestors (e.g. `workspaces/…`, like `shadow-forge-stress` → 4328 nodes), not under `.tmp/`. (The within-workspace ignore of these dir names IS intentional — only the ancestor match is the footgun.)

#### Symbol-graph edge resolution (LSP)
- The Rust parser emits `Calls`/`Inherits` edges to `external:<kind>:<name>` placeholders, then a resolver stage (`indexer-rs/src/resolver.rs`) queries the LSP (`textDocument/definition` + `implementation`) and rewrites them to workspace symbol nodes. `Implements` edges fan out from concrete impls to a Protocol/ABC/interface declaration. Python call bodies are walked for call sites; Python class bases and TS `extends`/`implements` are resolved to workspace classes via `definition`.
- **LSP must be ON for resolution.** `start-backend.sh` launches the self-updating watcher with `AI_EDITOR_LSP_ENABLED=true` (+ `AI_EDITOR_LSP_{PY,TS,RS}_CMD`, `AI_EDITOR_LSP_STARTUP_TIMEOUT_MS`, `AI_EDITOR_LSP_REQUEST_TIMEOUT_MS`). The watcher pays a one-time rust-analyzer warmup per launch, then resolves incrementally per changed file. The synchronous auto-index fallback (`retrieval/artifact_client._render_index_command`) forces LSP **off** (fast, tree-sitter-only) to avoid stalling a task; the watcher re-resolves and overwrites within ~a minute.
- **Resolution caveats:** pyright (open-source) does NOT implement `textDocument/implementation` (that's Pylance), so Python `Implements` fan-out is empty — use `Inherits` for nominal Python subclass discovery instead. Only NOMINAL subclassing is tracked; a class that conforms to a Protocol structurally without declaring it as a base is not in the graph.

#### `query_graph` tool (symbol-graph navigation)
- Registered in `PlanningToolRegistry` and `ToolRegistry` **only when an `index-snapshot.json` exists**; backed by `retrieval/graph_walker.py`. Reachable in all three context-gathering loops: planning, execution (must be in `verify_phase_sm._ALLOWED_TOOLS` for the current state — it is, for every state), and chat explore (must be in `chat/agent.py::_EXPLORE_SCHEMA` tool enum — it is).
- Two modes: file seed `node="<path>"` → distinct neighbour files grouped "depends on / connects out" vs "used by / connected in"; symbol seed `node="<path>:Symbol"` → symbol-level edges with line numbers. `edge_kinds` filters Calls/Imports/References/Inherits/Implements; `depth` (max 3), `limit` (max 60).
- Teaching blocks: `planning/prompts.py` (planning loop), `reasoning/tool_prompts.py` (execution loop), `chat/agent.py::_EXPLORE_PROMPT` (chat). The `tool` field in both response schemas is a free string — tool availability is the `registry.definitions() ∩ sm.allowed_tools()` intersection (execution) or the schema enum (chat), NOT a schema tool-enum.

### Testing patterns
- Python tests in `services/agentd-py/tests/` use `ScriptedPlanningEngine` / stub `Reasoner` classes + `InMemoryTaskStore` + `ShadowWorkspaceManager(tmp_path)`
- `ScriptedPlanningEngine` implements all three reasoning methods: `create_tool_step()`, `create_planning_step()`, and legacy `create_patch()`
- Scripted `create_tool_step()` responders must detect verify phase and return `verify_done` when entered. Pattern:
  ```python
  in_verify = any(
      isinstance(msg.get("content"), str) and "Patch applied successfully" in msg["content"]
      for msg in history
  )
  if in_verify:
      return {"type": "verify_done", "thought": "scripted", "verified": True, "test_output": ""}
  ```
- `pytest-asyncio` with `@pytest.mark.asyncio` for all async tests
- Integration-style tests (no mocks of the file system or HTTP) — real `tmp_path` shadows, real `PatchEngine`
- TypeScript tests use vitest; VS Code extension tests use a stub `ControllerUI` implementation
- **`InMemoryTaskStore.get()` returns the SAME object reference** (no copy), which MASKS stale-reference / object-divergence bugs. For a test that depends on production semantics — store returns a fresh copy, e.g. verifying a coroutine advanced the *caller's* task object — use `SQLiteTaskStore(tmp_path / "x.sqlite3")` instead.
- **Never `asyncio.get_event_loop().run_until_complete(coro)` in tests.** On Python 3.13 it raises `RuntimeError: There is no current event loop` once a prior `@pytest.mark.asyncio` test closes the loop, so the test passes in isolation but fails order-dependently in the full suite. Use `asyncio.run(coro)` or `@pytest.mark.asyncio async def`.
- **`pytest | tail` (or any pipe) masks pytest's exit code** with the pipe's last command's (always 0). Read the actual `FAILED`/summary lines, never trust the reported exit code of a piped run.
- A shifting failure set across full-suite runs = order/state pollution or environment dependence (e.g. `test_graph_walker_reachability` is `@requires_live_snapshot` and reflects `index-snapshot.json` freshness). Reproduce a suspect failure **in isolation** before attributing it to your change.

## Key Configuration

### Python backend env vars

**Core**
- `AI_EDITOR_REASONING_BACKEND` — LLM provider: `openai`, `anthropic`, `gemini`, `groq`, `ollama`, `watsonx`, `openrouter` (default: `openai`)
- `AI_EDITOR_DB_PATH` — SQLite database path (default: `.agentd/agentd.sqlite3`)
- `AI_EDITOR_SHADOW_ROOT` — shadow workspace root (default: `.agentd/shadows`)
- `AI_EDITOR_LOG_FILE` — path for the agentd file log (default: `.agentd/agentd.log` relative to uvicorn CWD); tailable with `tail -f services/agentd-py/.agentd/agentd.log`
- `AI_EDITOR_CHAT_DB_PATH` — SQLite path for chat threads (default: `.agentd/chat.sqlite3`)
- `AI_EDITOR_PROJECT_INSTRUCTIONS` — inject `<workspace>/AGENTS.md` into the controller system prompt (default **ON**; kill-switch — `0/false/no/off`). See "Project instructions (AGENTS.md) + prompt files".
- `AI_EDITOR_INSTRUCTIONS_MAX_CHARS` — size cap for the injected AGENTS.md (default `16000`; over-budget truncates with a marker).
- Provider API keys: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `GROQ_API_KEY`, etc.

**Model selection** (per provider)
- `AI_EDITOR_GEMINI_MODEL`, `AI_EDITOR_OPENAI_MODEL`, `AI_EDITOR_ANTHROPIC_MODEL`, `AI_EDITOR_GROQ_MODEL`, `AI_EDITOR_OLLAMA_MODEL`
- `AI_EDITOR_GEMINI_THINKING_LEVEL` — enables extended thinking for Gemini 2.5+ models (`none` | `low` | `medium` | `high`)

**Memory harness (see "Memory harness" under Architecture)**
- `AI_EDITOR_MEMORY_ENABLED` — master switch (default OFF; truthy = `1/true/yes/on`). When off, `prepare_turn` is a byte-identical passthrough. (Phase-2 recall/consolidation additionally needs a workspace scope, which the factories pass.)
- `AI_EDITOR_MEMORY_DB_PATH` — SQLite path (segments + anchors + memories) (default `.agentd/memory.sqlite3`)
- `AI_EDITOR_MEMORY_WINDOW_TOKENS` — effective context window the fracs are taken against (default `128000`)
- `AI_EDITOR_MEMORY_COMPACT_TRIGGER_FRAC` — fire compaction at this × window (default `0.65`)
- `AI_EDITOR_MEMORY_HOT_TOKEN_FRAC` — evict down to this × window of newest whole turns (default `0.4`)
- `AI_EDITOR_MEMORY_HOT_TURNS` — cap on **messages** (not logical turns) kept hot (default `10`)
- `AI_EDITOR_MEMORY_DEDUP_THRESHOLD` — cosine ≥ this dedupes a candidate vs same kind+scope (default `0.92`) · `AI_EDITOR_MEMORY_RECALL_TOKEN_BUDGET` — cap on injected recall (default `1500`) · `AI_EDITOR_MEMORY_WEIGHTS` — `w_sem,w_lex,w_struct` (default `0.5,0.3,0.2`) · `AI_EDITOR_MEMORY_GRAPH_GROUNDING` (default on) · `AI_EDITOR_EMBEDDING_MODEL` (reused — default `BAAI/bge-small-en-v1.5`). Needs `pip install -e '.[memory]'` (sqlite-vec + sentence-transformers).

**Tool loop**
- `AI_EDITOR_TOOL_LOOP_ENABLED` — set to `0` or `false` to fall back to single-shot `create_patch()` (default: `true`)
- `AI_EDITOR_TOOL_RESULT_MAX_CHARS` — max chars of tool output injected into loop context (default: `4000`)
- `AI_EDITOR_RIPGREP_CMD` — path to ripgrep binary used by `search_code` (default: `rg`)
- `AI_EDITOR_SHELL_POLICY` — `ask` (default, every `run_command` surfaces an Accept-once / Accept-and-remember-this-workspace / Reject card) or `allow_all` (skip the gate; run any command). Per-task override via the `shell_policy` field on the task submission. Replaces the old `AI_EDITOR_SHELL_ALLOWLIST` (removed).
- `AI_EDITOR_COMMAND_DECISION_TIMEOUT_SEC` — seconds to wait for the user's command decision; `0` (default) = wait forever. On timeout the command is rejected (returned as a tool-result error so the agent adapts).
- `AI_EDITOR_STEP_REVIEW_AUTO_ACCEPT` — workspace default for whether step diffs are auto-accepted (`true`, default) or surfaced for review (`false`). The submission payload's `step_review_auto_accept` field, when provided, overrides this; when omitted, the env value wins.

**Scope extension** (controls how out-of-scope file writes are handled)
- `AI_EDITOR_SCOPE_POLICY` — `strict` (auto-reject) | `ask` (pause + VS Code modal, **default via start-backend.sh**) | `auto` (silently approve + audit log)
- `AI_EDITOR_SCOPE_TRIGGER` — `any` (every out-of-scope file trips the gate, **default via start-backend.sh**) | `nearby` (only files in the same directory as a target or conventional names like `__init__.py`)
- `AI_EDITOR_SCOPE_REMEMBER` — `task` (approved files remembered for the rest of the task, **default**) | `none` (ask every time)
- `AI_EDITOR_SCOPE_TIMEOUT_SEC` — seconds before an `ask`-mode prompt auto-rejects (0 = wait forever, default)

**Important**: `start-backend.sh` always sets `ask` + `any` so every out-of-scope write shows a VS Code approval modal. The Python engine defaults (`strict` + `nearby`) are only relevant when the backend is started outside the script.

**Retrieval**
- `AI_EDITOR_RETRIEVAL_SNAPSHOT_PATH` — path to index-snapshot.json (default: `<workspace>/.ai-editor/index-snapshot.json`)
- `AI_EDITOR_RETRIEVAL_MAX_AGE_SEC` — max snapshot age before auto-reindex (default: `900`)
- `AI_EDITOR_INDEXER_INDEX_CMD` — command template for auto-indexing (`{workspace}`, `{snapshot_path}`)

**Validation**
- `AI_EDITOR_VALIDATION_COMMANDS_JSON` — JSON array of validation commands to run after execution; overrides auto-detection

### VS Code extension settings (package.json contributes.configuration)
- `aiEditor.backendBaseUrl` — default `http://localhost:8000`
- `aiEditor.defaultMode` — `inline | file_edit | project_edit | autonomous`
- `aiEditor.pollIntervalMs` — default `2000`

---

## Debugging Methodology

### Trace the full call path before asserting how code works

Do not describe (or design against) a code path from a single function. **Trace it end to end first** — caller → callee → the actual strings/values sent to the boundary. A recurring failure mode: reading one builder (e.g. `build_planning_step_payload`) and assuming the rest, when the system prompt is built by a *separate* function (`format_planning_system_prompt`) and the two are passed independently to `generate_json(system_instructions=…, user_payload=…)`.

Worked example — where does the planner's `initial_context` (retrieval) actually live?
- `orchestrator/engine.py`: `retrieval_context.as_prompt_payload()` → `initial_context=` in `plan_context` (pinned to round-1 on feedback via `task.planning_initial_context`).
- `reasoning/engine.py::create_planning_step`: calls `format_planning_system_prompt(...)` (system string = prompt text + `tools_json` + `max_calls`, **no retrieval**) AND `build_planning_step_payload(...)` (user payload — retrieval lands here, early, with `instruction`/`budget_status` LAST for KV-cache stability).
- Both strings go to `generate_json` as separate args.

The lesson: the system prompt and the user payload are built by different functions and carry different things; verify which builder owns a field before reasoning about prompt structure, caching, or staleness. Use `_debug_dump` artifacts (`plan-turn-NN`) to see the exact bytes sent.

### Starting the backend for local testing

Always use `start-backend.sh` rather than running uvicorn directly — it sets all env vars correctly:

```bash
# From repo root — pick a workspace and provider
export $(cat .env | grep -v "^#" | grep "=" | sed 's/"//g' | xargs)
bash scripts/stress/start-backend.sh \
  --backend gemini \
  --workspace "$PWD/workspaces/shadow-forge-stress" \
  --validation-profile none   # use 'full' when testing validation

# Verify it's up
curl -s http://localhost:8000/health
```

Log file lands in `.tmp/stress-<timestamp>/logs/agentd.log`. Tail it while running tasks:

```bash
tail -f .tmp/stress-*/logs/agentd.log | grep -v "GET /v1/tasks"   # filter out poll noise
```

### Opening the VS Code extension development host

```bash
code --extensionDevelopmentPath="$PWD/apps/vscode-extension" "$PWD/workspaces/shadow-forge-stress"
```

After any TypeScript change: `npm run build` then reload the extension host window (`Cmd+Shift+P` → Developer: Reload Window).

### Scripted end-to-end testing (acts as a human)

`scripts/verify/` has a three-stage flow that drives a task from submission to acceptance:

```bash
cd services/agentd-py && source .venv/bin/activate && cd -

# Stage 1 — submit task, wait for plan
python scripts/verify/01_create_task.py '<goal>' '<workspace_path>'

# Stage 2 — optional: provide feedback to regenerate plan
python scripts/verify/02_feedback.py '<feedback text>'

# Stage 3 — approve plan, wait for READY_FOR_REVIEW, accept patch
python scripts/verify/03_finalize.py
```

Task ID is persisted to `/tmp/ai-editor-verify-state/current_task_id.txt` between stages.

### Inspecting a task mid-flight

```bash
TASK_ID=task-xxxx
curl -s http://localhost:8000/v1/tasks/$TASK_ID | python3 -m json.tool
curl -s http://localhost:8000/v1/tasks/$TASK_ID/result | python3 -m json.tool
```

### Watching the SSE stream directly

```bash
curl -sN --no-buffer "http://localhost:8000/v1/tasks/$TASK_ID/stream-patch" \
  -H "Accept: text/event-stream"
```

Connect **before** approving the plan — the stream stays open through the entire execution. Event types:

| Event | When |
|-------|------|
| `planning_tool_call` | PlanningAgent calls a tool during exploration |
| `planning_tool_result` | Tool result returned to PlanningAgent |
| `planning_complete` | PlanningAgent emitted plan; includes `files_examined` and `confidence` |
| `tool_call` | Execution agent calls a tool within a step's ReAct loop |
| `tool_result` | Tool result returned to execution agent |
| `revision_needed` | Execution agent signalled plan is wrong; delta replan will fire |
| `operation_success` | A patch op applied successfully |
| `operation_error` | A patch op failed |
| `done` | Task reached a terminal state |

### Diagnosing a stuck task

| Symptom | Likely cause | Check |
|---------|-------------|-------|
| Stuck at `CONTEXT_READY` | PlanningAgent exploring (many tool calls) OR Gemini rate limit | Log: `[PLAN] PlanningAgent exploring` vs `Gemini transient error` |
| Stuck at `AWAITING_PLAN_APPROVAL` | Normal — waiting for user to approve/reject | Expected state; poll task status |
| Stuck at `PLANNED` after approval | Backend restarted mid-flight; orphan task | Start a new task or use `POST /resume` with `stage: execute` |
| Step loops in verify phase with non-zero exits | Pre-existing test failures keep SM in `TEST_FAILED` | Agent should run scoped test (e.g. `pytest tests/test_foo.py::test_bar`), not full suite — `_static_baseline` filters pre-existing errors from the postpatch comparison |
| `DUPLICATE PATCH BLOCKED` in tool history | SM dedup caught an exact-repeat `emit_patch` within a single state stay | Expected — model should read the file first; cache clears on the next transition |
| `VerifyPhaseExhausted` in logs | 5 consecutive engine failures from `PATCH_FAILED_CAN_RETRY` | Step attempt converted to `VerifyResult(verified=False)`; orchestrator retries step or marks failed per its retry policy |
| No `planning_tool_call` events but task advancing | Tool loop disabled (`AI_EDITOR_TOOL_LOOP_ENABLED=0`) | Single-shot patch mode active |
| SSE stream closes immediately | Replay buffer has stale `done`, or task is terminal | Check task status; start a new task |
| `revision_needed` event but task fails shortly after | Delta replan budget hit (`max_delta_replans`) | Check `delta_replans_used` in task execution_state |

### Asyncio race conditions to watch for

The orchestration engine is single-process asyncio. Key race windows:

- **`_running_tasks` vs SSE connect**: `run_task`/`continue_task` add to `_running_tasks` at their first line, but they run inside `asyncio.create_task()` — they don't start until the current coroutine yields. The route handler pre-adds `task_id` to `_running_tasks` before `create_task()` for the feedback route to close this window.
- **Replay buffer pollution**: `run_task` broadcasts `done` when pausing at `AWAITING_PLAN_APPROVAL`. This goes into the replay buffer and would cause any new SSE subscriber to close immediately. Fix: `clear_replay()` instead of `broadcast(done)` at the pause point.
- **`webview.html` coalescing**: VS Code coalesces rapid sequential writes to `webview.html` into one render. Use `postMessage` for incremental updates (patch events); only replace the full HTML on genuine state changes (status, result, files).

### Reading artifacts to understand what happened

Every task writes debug artifacts to `<workspace>/.agentd/artifacts/<task_id>/`. This is the primary source of ground truth when a task behaves unexpectedly — check here before guessing.

```
<task_id>/
  plan-evidence.json              # retrieval context fed to the planner
  planning-trace.json             # PlanningAgent tool call trace (initial plan)
  planning-trace-feedback.json    # PlanningAgent tool call trace (after user feedback)
  json-plan-draft.json            # raw output of markdown→JSON plan conversion
  plan.json                       # approved executable plan (PlanDocument)
  delta-replan-revision.json      # delta replan result when revision_needed fires
  full-validation.json            # validation output after all steps complete

  step-<id>/
    tool-trace.json               # execution agent tool call trace for this step
    attempt-<n>/
      patch-context.json          # full context sent for patch generation (tool-loop-disabled mode)
      patch.json                  # parsed patch candidate(s)
      preflight-<cN>.json         # preflight check result per candidate
      ranking.json                # candidate scoring and selection
```

**What to look at for each failure mode:**

| Problem | Artifact to read |
|---------|-----------------|
| Plan explores wrong files / misses key symbols | `planning-trace.json` — which tool calls did the planning agent make? What did it read? |
| Plan markdown looks correct but JSON plan is wrong | `json-plan-draft.json` — did the markdown→JSON conversion parse the steps correctly? |
| Step fails repeatedly with no progress | `step-<id>/tool-trace.json` — is the agent reading the right files before emitting? |
| Patch applies but logic is wrong | `step-<id>/tool-trace.json` — did the agent call `run_command` to verify before emitting? |
| `revision_needed` fires unexpectedly | `delta-replan-revision.json` — what evidence did the agent cite? What steps were revised? |
| Step fails preflight (file not found, policy violation) | `step-<id>/attempt-<n>/preflight-<cN>.json` |
| Wrong patch generated (bad search string) | `step-<id>/attempt-<n>/patch.json` + check `tool-trace.json` — did the agent read the file first? |
| Validation fails after patching | `full-validation.json` |

**Quick command to inspect the latest task's artifacts:**

```bash
TASK_ID=$(cat /tmp/ai-editor-verify-state/current_task_id.txt)
ARTIFACTS="<workspace>/.agentd/artifacts/$TASK_ID"

ls $ARTIFACTS
ls $ARTIFACTS/step-*/

cat $ARTIFACTS/planning-trace.json | python3 -m json.tool | less
cat $ARTIFACTS/step-s1/tool-trace.json | python3 -m json.tool
cat $ARTIFACTS/delta-replan-revision.json | python3 -m json.tool
```

The API also exposes artifacts:
```bash
curl -s http://localhost:8000/v1/tasks/$TASK_ID/artifacts | python3 -m json.tool
```

### Provider-specific notes

- **Gemini**: use `gemini-flash-latest` (stable alias). Preview models have lower quota and hit 429s. Set `AI_EDITOR_GEMINI_TIMEOUT_SEC=600` — the default 120s is too short for the PlanningAgent loop (many tool calls).
- **Model env var**: `AI_EDITOR_GEMINI_MODEL` in `.env` must be on its own line — concatenating it with another var (no newline) silently breaks the export.
- Transient 429/503 errors retry automatically (up to 4 attempts, exponential backoff). A task stuck at `CONTEXT_READY` with log lines `Gemini transient error (attempt N/4)` is retrying — wait it out.
- **Anthropic**: constrained JSON decoding is not available (no `response_json_schema`). The schema is stringified into the system prompt instead. Expect slightly lower schema compliance rate; the discriminated union constraints help enforce it at the prompt level.
- **Ollama** (qwen3-family models): qwen3 emits implicit thinking tokens even with no explicit `think` flag. When `format=<schema>` (structured output) is active, thinking can exhaust `num_predict` before the JSON is emitted, leaving `message.content` empty. Fix: `num_predict=-1` on JSON calls (unlimited); `num_predict=2048` on text calls caps runaway answers. The `ChatAgent` explore phase deliberately omits conversation history from structured-output payloads to reduce context size and lower the probability of this. Thinking support was intentionally removed from `OllamaJsonTransport` — `think=True`/`think=False` in the request body and the `AI_EDITOR_OLLAMA_THINKING_ENABLED` env var are gone.
- **Ollama — `start-backend.sh`**: run the script from the repo root that owns the `services/agentd-py` you want to test. If using `--agentd-dir` (worktree override), ensure `AI_EDITOR_WORKSPACE_PATH` is exported inside the env block — missing it causes `ChatAgent` to default to `cwd` (the agentd-py dir) instead of the workspace root.
