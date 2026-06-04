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
- **testing_strategy vs test_command**: the planner sets `testing_strategy` on every code step as a natural-language hint (e.g. "run pytest on test_auth.py"). `test_command` is only set when the test file is itself a target of that step — this prevents running stale tests when the import hasn't been updated yet. The execution agent uses `testing_strategy` to discover the right command when `test_command` is absent.
- **read_file phased target**: in the explore phase `read_file`/`search_code` read the **real workspace** (pre-patch). Once the SM transitions out of `EXPLORE` (i.e. the first patch has been applied), `ToolRegistry.use_shadow_for_reads()` is called and subsequent reads return the **shadow workspace** — the model can see exactly what its patches produced before re-patching.
- **Pre-existing failure normalization**: `_normalize_error_message` in `engine.py` fingerprints pytest and cargo test output so that failures present before patching are filtered from the post-patch baseline comparison. Cargo failures are identified by extracting failed test names from the `failures:` block; shadow paths (`.agentd/shadows/task-xxx/`) are stripped before comparison.
- **Delta replan**: a `revision_needed` signal from the execution agent triggers `PlanningAgent.revise()`, which explores the workspace and emits targeted step revisions without restarting the task. Budget: `max_delta_replans` (default 3).
- **Milestone snapshot**: at every `AWAITING_PLAN_APPROVAL` transition the engine serializes the full task state into `plan_approval_snapshot`. Used verbatim to reconstruct the exact plan-review state for resume rollbacks.
- **Step execution** is bounded. Each step uses `completed_step_ids` to skip already-done work; failed steps checkpoint the shadow back before giving up.

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
- `chat/storage.py` — `ChatThreadStore`: SQLite multi-thread storage; `resolve_diff_card(inline_task_id, resolution)` patches diff card resolved state
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
- `GET /v1/chat/threads?workspace=<path>` — list threads for a workspace
- `POST /v1/chat/threads` — create thread (`{workspace, title}`)
- `GET /v1/chat/threads/{thread_id}` — get thread with messages
- `POST /v1/chat/threads/{thread_id}/message` — SSE stream

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
| `chat_done` | Turn complete |

`ChatAgent` flow per message:
1. **Explore phase** — up to 5 tool calls via `PlanningToolRegistry`; results go into `context`
2. **Classify** — `IntentClassifier` → `qa / small_change / large_change`
3. **Respond** — `qa` → `generate_text` answer; `small_change` → `run_inline_change`; `large_change` → `create_task_from_chat`

`ChatMessage.metadata` fields used at runtime:
- `thinking_log: list[str]` — all thinking/tool-call snippets from the agent turn; stored on both QA messages and diff cards; rendered as a collapsible "Show thinking" pane
- `diff_entries: list[DiffEntry]` — files changed in an inline change (on `diff_card` messages)
- `resolved: "applied" | "discarded"` — patched in by `resolve_diff_card()` after promote/discard; controls rendered state of diff card buttons
- `taskId: str` — inline task id on diff card messages

`ChatThreadStore.resolve_diff_card(inline_task_id, resolution)` scans all threads for the matching `diff_card` message by `task_id` and patches `metadata.resolved` in-place — called from the promote and discard API routes.

### Retrieval pipeline
- `indexer-rs` writes `index-snapshot.json` with `nodes`/`edges`/`diagnostics`/`stats`
- `agentd-py` reads the snapshot per task via `retrieval/` module; if missing, auto-triggers one index run
- Retrieval context flows into `PlanningAgent.generate_plan()` as `initial_context` and into step execution via `patch_request_context`
- `graph_neighbor_files` (in the planner payload via `RetrievalContext.as_prompt_payload()`): files reached from the goal's matched/semantic seeds by one structural hop, surfaced as an initial reading list
- Stale/missing snapshots emit warning diagnostics but never block orchestration
- Both `PlanningToolRegistry` and `ToolRegistry` also give the agent live access to the workspace during their loops (not just the snapshot)

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

## Key Configuration

### Python backend env vars

**Core**
- `AI_EDITOR_REASONING_BACKEND` — LLM provider: `openai`, `anthropic`, `gemini`, `groq`, `ollama`, `watsonx`, `openrouter` (default: `openai`)
- `AI_EDITOR_DB_PATH` — SQLite database path (default: `.agentd/agentd.sqlite3`)
- `AI_EDITOR_SHADOW_ROOT` — shadow workspace root (default: `.agentd/shadows`)
- `AI_EDITOR_LOG_FILE` — path for the agentd file log (default: `.agentd/agentd.log` relative to uvicorn CWD); tailable with `tail -f services/agentd-py/.agentd/agentd.log`
- `AI_EDITOR_CHAT_DB_PATH` — SQLite path for chat threads (default: `.agentd/chat.sqlite3`)
- Provider API keys: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `GROQ_API_KEY`, etc.

**Model selection** (per provider)
- `AI_EDITOR_GEMINI_MODEL`, `AI_EDITOR_OPENAI_MODEL`, `AI_EDITOR_ANTHROPIC_MODEL`, `AI_EDITOR_GROQ_MODEL`, `AI_EDITOR_OLLAMA_MODEL`
- `AI_EDITOR_GEMINI_THINKING_LEVEL` — enables extended thinking for Gemini 2.5+ models (`none` | `low` | `medium` | `high`)

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
