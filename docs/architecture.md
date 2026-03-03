# Architecture (Polyglot v1)

## Services
- `apps/editor-client` (TypeScript)
  - Shared JSON schemas and task contracts for editor UI
  - Typed HTTP client for backend task APIs
- `apps/vscode-extension` (TypeScript)
  - VS Code command surface for task lifecycle actions
  - Review panel UI for status, diagnostics, plan/patch payload, and diff actions
  - Real-vs-shadow diff opening for modified files during review
- `services/agentd-py` (Python)
  - Stateful task orchestration and deterministic control loop
  - Budget enforcement, lifecycle transitions, repair loop policy
  - Provider adapters (OpenAI and future providers)
- `services/indexer-rs` (Rust)
  - Incremental parse/index pipeline
  - Symbol graph materialization
  - LSP diagnostics enrichment
  - Snapshot artifact + deterministic graph query CLI

## Deterministic boundaries
- Model output is never executed directly.
- `agentd-py` validates plan/patch payloads into typed models.
- Patch application and validation gates remain deterministic.
- Retrieval context is artifact-backed and loaded once per task (no per-loop command chatter).

## API contracts
- `POST /v1/tasks`
- `GET /v1/tasks/{task_id}`
- `GET /v1/tasks/{task_id}/result`
- `POST /v1/tasks/{task_id}/cancel`
- `POST /v1/tasks/{task_id}/accept`
- `POST /v1/tasks/{task_id}/reject`

## Orchestration lifecycle
`QUEUED -> CONTEXT_READY -> PLANNED -> PATCHED -> VALIDATING -> REPAIRING -> READY_FOR_REVIEW -> PROMOTING -> SUCCEEDED|FAILED|ABORTED`

## Retrieval artifact flow
1. `indexer-rs index` writes `<workspace>/.ai-editor/index-snapshot.json` with schema/version metadata, full graph, diagnostics, and stats.
2. `agentd-py` loads snapshot artifact once after shadow workspace preparation.
3. If artifact is missing, `agentd-py` tries a single auto-index command and retries artifact load once.
4. Stale/corrupt/missing artifacts emit warning diagnostics; task execution continues with empty retrieval context when needed.
5. Plan/patch prompts receive compact retrieval context (`related_files`, `related_symbols`, neighbors, diagnostics excerpt, snapshot age/stats).

## Near-term implementation targets
1. Rollback snapshots per repair iteration for deterministic recoverability.
2. Task timeline/event query endpoints and richer timeline views.
3. Loop analytics (iteration counts, token budget consumption, failure causes, latency histograms).
4. Extension end-to-end smoke tests against a live scripted backend instance.
5. Introduce step-scoped patch generation orchestration: replace full-plan single-shot patching with one focused patch call per plan step (or small step group), enforce bounded patch size caps on ops/files, and track step progress via `completed_step_ids`.
