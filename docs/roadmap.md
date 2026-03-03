# Roadmap

## Phase 1: Stack Pivot (complete)
- TypeScript package narrowed to client/contracts role
- Python orchestration backend scaffolded
- Rust indexer scaffolded

## Phase 2: Deterministic Backend Completion (mostly complete)
- [x] shadow workspace (`real_repo` + `shadow_repo`)
- [x] patch preflight and forbidden path policy
- [x] SQLite task/event persistence
- [ ] rollback snapshots per loop iteration

## Phase 3: Retrieval + Intelligence (current milestone complete for structural retrieval)
- [x] parser registry for TS/Py/Rs symbol extraction in Rust
- [x] full symbol graph snapshot artifact (`nodes`, `edges`, diagnostics, stats)
- [x] deterministic graph query CLI (`index`, `query`)
- [x] artifact-first retrieval integration in orchestrator (single snapshot load per task)
- [ ] embeddings/semantic retrieval (deferred)

## Phase 4: Agent Reliability (partially complete)
- [x] OpenAI provider integration with strict schema parsing
- [x] diagnostics-aware repair loop
- [ ] loop observability and budget analytics
- [ ] Step-scoped patch generation planning/execution loop with bounded patch size (migrate away from full-plan single-shot patching)

## Phase 5: Product Surface (pending)
- [x] VS Code extension command surface and diff review UI (MVP)
- [x] approval workflow and patch accept/reject actions (via extension commands + review panel)
- [ ] user-visible timeline and execution traces
