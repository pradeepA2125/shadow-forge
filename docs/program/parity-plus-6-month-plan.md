# AI Editor 6-Month Parity+ Program (Reliability First)

Updated: March 12, 2026

## Program Summary
- Horizon: 24 weeks (6 phases).
- Launch target: Indie + small teams.
- Baseline parity target: Cursor + Windsurf core capabilities.
- Operating model: Linear for delivery execution, Notion for product/architecture strategy.
- Principle: LLM does reasoning, deterministic systems enforce safety and correctness.

## Phase Plan and Exit Gates

### Phase 0 (Weeks 1-2): Productization Baseline + Evaluation Harness
Deliverables:
- Unified evaluation harness for plan quality, patch correctness, promotion safety, and UX reliability.
- Benchmark set frozen: 100 internal tasks + 50 OSS tasks (TypeScript/Python/Rust).
- Deterministic task bundle replay (`plan`, `patch attempts`, `preflight`, `validation`, `promotion`).
- Baseline metrics board (success rate, retries, unsafe mutation rate, accept rate).

Exit gate:
- Replay works for at least 95% of failed tasks.
- Baseline dashboard is live and used in weekly review.

### Phase 1 (Weeks 3-6): Patch Engine v2
Deliverables:
- Hybrid text + CST/AST patching path per language (TS/Py/Rs).
- Simulated apply preflight before mutation:
  - dependency/order checks across patch ops,
  - anchor stability checks,
  - language parse checks.
- Transactional checkpoint per step attempt with deterministic rollback and replay.
- Candidate patch ranking:
  - score = static validity + minimal diff + schema/contract fit.

Exit gate:
- Syntax/indent/anchor-drift step failures reduced by 70% vs Phase 0 baseline.

### Phase 2 (Weeks 7-10): Planner/Executor/Critic v2
Deliverables:
- Plan graph execution model with explicit `preconditions`, `postconditions`, and `verification`.
- Critic loop with typed failure taxonomy:
  `transport_format`, `anchor_invalid`, `semantic_mismatch`, `test_failure`, `policy_violation`.
- Rules/memory subsystem with deterministic precedence:
  global -> workspace -> repo -> task.

Exit gate:
- Benchmark success rate at least 60%.
- No unsafe file mutations outside policy scope.

### Phase 3 (Weeks 11-14): Cursor/Windsurf Core Parity Surface
Deliverables:
- VS Code timeline UX: per-attempt traces, preflight/validation deltas, rollback visibility.
- Background task mode with resume support and checkpoint restore.
- Code review assistant mode for file/PR review findings and suggestions.
- MCP tool policy controls:
  allowlist, path scope, network scope, audit metadata.

Exit gate:
- Users can submit, inspect timeline, rollback/restore, and accept with stable behavior.

### Phase 4 (Weeks 15-19): Copilot-Class Workflow Layer
Deliverables:
- Issue-driven flows: issue -> plan -> patch -> review artifact chain.
- Knowledge spaces: docs/memory ingestion pipeline with snapshot-backed references.
- Collaboration metadata:
  shareable run links, provenance of promoted patches, approval audit fields.

Exit gate:
- Cross-session continuity and team review workflow are stable in E2E runs.

### Phase 5 (Weeks 20-24): Differentiation
Deliverables:
- Deterministic multi-agent coordinator (Planner, Retriever, Patcher, Verifier).
- Retrieval v2 ranking:
  symbolic + semantic + freshness-aware blending.
- Long-running autonomous refactor mode with budget contracts and staged promotion.

Exit gate:
- Complex multi-file success improves by 20% over Phase 3 baseline.
- Unsafe mutation rate does not regress.

## Planned API and Contract Additions

### Task APIs (additive)
- `GET /v1/tasks/{task_id}/events`
- `GET /v1/tasks/{task_id}/timeline`
- `GET /v1/tasks/{task_id}/artifacts`
- `POST /v1/tasks/{task_id}/resume`
- `POST /v1/tasks/{task_id}/rollback`
- Optional: `GET /v1/tasks/{task_id}/stream` (SSE status stream)

### Planning/Patching Contracts
- `PatchDocumentV2`: language-aware patch ops + candidate set + scoring metadata.
- `PlanStepV2`: `preconditions`, `postconditions`, `verification`, `allowed_files`, `risk`.
- `FailureTaxonomyV2`: transport/preflight/validation/semantic/provider classes.

### Policy and Memory Contracts
- `RuleSet`: global/workspace/repo/task scopes with deterministic precedence.
- `ToolPolicy`: MCP allowlist, path/network scopes, audit metadata.

## Test and Acceptance Framework
- Reliability gates:
  preflight precision/recall corpus, replay determinism checks, rollback integrity tests.
- Quality gates:
  success rate, average attempts, unsafe mutation rate, human accept rate.
- UX/workflow gates:
  VS Code E2E (`submit -> timeline -> diff -> rollback/resume -> accept/reject`) and team review E2E.

## Execution Cadence
- Weekly:
  benchmark review and top 5 failure clusters.
- Biweekly:
  phase checkpoint against exit gate metrics.
- Monthly:
  competitor parity audit and gap refresh.

## Defaults and Guardrails
- Keep current polyglot architecture (TS UI, Python orchestrator, Rust indexer).
- Keep provider-agnostic reasoning transport layer.
- No breaking removals in existing APIs during this program; additive changes only.
- Keep deterministic safety enforcement as non-negotiable.

## Competitor Baseline References
- Cursor docs (rules, memories, MCP, background agent, bugbot): `https://cursor.com/docs`
- GitHub Copilot docs (coding agent, spaces/knowledge, enterprise controls): `https://docs.github.com/en/copilot`
- Windsurf docs (cascade/workflows, memories/rules, project behaviors): `https://docs.windsurf.com`
