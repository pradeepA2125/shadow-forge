# Task Board

## Milestone: Polyglot Pivot

### Done
- [x] Reposition TypeScript package as editor-client/contracts (`apps/editor-client`)
- [x] Scaffold Python orchestrator service (`services/agentd-py`)
- [x] Scaffold Rust indexer service (`services/indexer-rs`)
- [x] Update architecture docs and service boundaries
- [x] Implement shadow workspace model in `agentd-py`
- [x] Add forbidden-path policy checks in `agentd-py`
- [x] Replace in-memory store with SQLite persistence
- [x] Integrate OpenAI reasoning provider with strict JSON schema parsing
- [x] Add deterministic validator command pipeline (syntax/type/lint/test)
- [x] Replace default runtime wiring with OpenAI provider + command validator (`agentd/main.py`)
- [x] Add patch review/promotion lifecycle states in orchestrator + API
- [x] Add `TaskResult` API parity on `/accept` and `/reject` endpoints
- [x] Expose richer task result retrieval endpoint (`GET /v1/tasks/{task_id}/result`)
- [x] Implement full LSP session manager in Rust indexer (TS + Python + Rust) with best-effort fallback
- [x] Implement real index watch loop in Rust (`notify`-driven incremental updates)
- [x] Implement parser registry for TS/Py/Rs symbol extraction in Rust indexer
- [x] Persist full graph payload (`nodes` + `edges`) in index snapshot artifact with stats
- [x] Add deterministic graph query CLI (`index`, `query`) in `indexer-rs`
- [x] Integrate artifact-first retrieval context into orchestrator planning + patch prompts
- [x] Add one-shot auto-index attempt on missing snapshot with best-effort warning fallback
- [x] Add retrieval artifact and orchestrator retrieval-context tests
- [x] Add VS Code extension package scaffold (`apps/vscode-extension`)
- [x] Implement VS Code commands for start/open/accept/reject/refresh task flow
- [x] Implement review panel UI with plan/patch payload preview and diagnostics
- [x] Implement real-vs-shadow file diff open action from review panel
- [x] Wire extension to existing backend APIs via `@ai-editor/editor-client`
- [x] Add extension unit/integration tests for command handling, polling stop behavior, and diff path mapping

### In Progress
- [ ] Add end-to-end extension smoke test against a running scripted `agentd-py`

### Next
- [ ] Rollback snapshots per repair-loop iteration
- [ ] Timeline/execution trace API surface
- [ ] Loop analytics and budget telemetry
- [ ] Embeddings/hybrid semantic retrieval layer (deferred from current milestone)
- [ ] Refactor create_patch flow from full-plan single-shot patching to step-scoped patch generation with bounded patch size responses (track progress with completed_step_ids)
