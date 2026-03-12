# AI Editor

Production-grade AI editor foundation with a polyglot architecture.

## Program roadmap
- Active strategy: 24-week parity+ roadmap (reliability first, Cursor/Windsurf core parity baseline).
- Program plan: `docs/program/parity-plus-6-month-plan.md`
- Delivery model: `docs/program/linear-execution-model.md`
- Strategy/docs model: `docs/program/notion-structure.md`

## Service layout
- `apps/editor-client` (TypeScript): editor-facing contracts and HTTP client
- `apps/vscode-extension` (TypeScript): VS Code MVP command + review UI
- `services/agentd-py` (Python): deterministic orchestration backend
- `services/indexer-rs` (Rust): indexing and symbol graph service

## Why this split
- TypeScript fits VS Code/UI integration and schema sharing.
- Python fits agent orchestration and model/provider integrations.
- Rust fits high-throughput incremental indexing and graph updates.

## Quick start

### TypeScript client package
```bash
npm install
npm run typecheck
npm run test
npm run build
```

### VS Code extension package
```bash
npm run -w @ai-editor/vscode-extension typecheck
npm run -w @ai-editor/vscode-extension test
npm run -w @ai-editor/vscode-extension build
```

### Python backend
```bash
cd services/agentd-py
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
uvicorn agentd.main:app --reload --port 8000
```

### Rust indexer
```bash
cd services/indexer-rs
cargo run -- index --workspace /path/to/repo --snapshot-path /path/to/repo/.ai-editor/index-snapshot.json --watch 0
cargo run -- query --snapshot-path /path/to/repo/.ai-editor/index-snapshot.json --mode symbol_name --value build --depth 2 --limit 200
```

## Retrieval core (artifact-first)
- `indexer-rs` persists full graph artifacts to JSON snapshots (`nodes`, `edges`, diagnostics, stats).
- `agentd-py` reads snapshot artifacts directly per task and passes retrieval context into planning/patch prompts.
- If a snapshot is missing, `agentd-py` performs at most one auto-index attempt for the task, then continues best-effort.
- Stale/corrupt/missing artifacts emit warning diagnostics and do not fail task orchestration.

Key env vars:
- `AI_EDITOR_RETRIEVAL_SNAPSHOT_PATH` (default: `<workspace>/.ai-editor/index-snapshot.json`)
- `AI_EDITOR_RETRIEVAL_MAX_AGE_SEC` (default: `900`)
- `AI_EDITOR_INDEXER_INDEX_CMD` (optional command template with `{workspace}` and `{snapshot_path}`)

## Current focus
- [x] Pivot stack boundaries (TS client, Python orchestrator, Rust indexer)
- [x] Shadow workspace (`real_repo` + `shadow_repo`) in Python backend
- [x] Forbidden-path policy + patch preflight checks
- [x] SQLite task/event persistence for `agentd-py`
- [x] OpenAI reasoning provider integration (schema-constrained JSON outputs)
- [x] Deterministic validation command pipeline (configurable + auto-detected)
- [x] Patch review/promote lifecycle states (`READY_FOR_REVIEW` -> `PROMOTING` -> `SUCCEEDED`)
- [x] TaskResult parity on review endpoints (`/accept`, `/reject`)
- [x] TaskResult retrieval endpoint (`GET /v1/tasks/{task_id}/result`)
- [x] LSP session manager in Rust indexer (TS + Pyright + rust-analyzer, diagnostics-first, best-effort fallback)
- [x] Artifact-first retrieval core (parser registry, full graph snapshot, deterministic query CLI, orchestrator artifact integration)
- [x] VS Code MVP review loop (start task, poll status, review panel, real/shadow diff, accept/reject/refresh commands)
- [x] Step-scoped patch execution with bounded per-step patch calls
- [x] Deterministic patch preflight conflict detection for self-invalidating op order/anchors
- [x] Plan-target grounding with one-shot replan feedback for missing step paths

## Explicitly pending
- [x] Phase 0 eval harness and deterministic replay bundles
- [ ] Phase 1 patch engine v2 (hybrid CST/AST + simulated apply + ranking)
: delivered so far in Phase 1 kickoff: deterministic preflight dependency/anchor conflict checks, candidate ranking with deterministic selection, transactional checkpoints, and `/v1/tasks/{task_id}/artifacts`.
- [x] Phase 1 failure corpus + gate report tooling (`docs/benchmarks/phase1-failure-corpus.v1.json`, `ai-editor-eval phase1-gate-report`)
- [ ] Phase 2 planner/executor/critic v2 and rules/memory precedence
- [ ] Phase 3 parity surfaces (timeline/background/code review + MCP policy controls)
- [ ] Phase 4 workflow layer (issue-driven flows + knowledge spaces + collaboration metadata)
- [ ] Phase 5 differentiation (multi-agent orchestrator + retrieval v2 + autonomous refactors)
