# Linear Execution Model (Parity+ Program)

Updated: March 12, 2026

## Program Projects
Create six Linear projects:
1. `AI Editor Phase 0 - Eval Harness`
2. `AI Editor Phase 1 - Patch Engine v2`
3. `AI Editor Phase 2 - Planner/Critic v2`
4. `AI Editor Phase 3 - Core Parity Surface`
5. `AI Editor Phase 4 - Workflow Layer`
6. `AI Editor Phase 5 - Differentiation`

Import seed:
- `docs/program/linear-backlog-seed.csv`

## Epic Taxonomy (used in every phase)
- `Reliability`
- `Planner`
- `Patch Engine`
- `UI/Review`
- `Policy/MCP`
- `Evaluation`

## Required Fields for Every Ticket
- `Phase` (0..5)
- `Epic` (from taxonomy above)
- `Failure taxonomy tag` (if defect-driven)
- `Benchmark impact` (expected metric movement)
- `Acceptance metric` (numeric or binary)
- `E2E scenario impacted` (link to scenario ID)

## Backlog Seed (First 8 Weeks)

### Phase 0 (Weeks 1-2)
- Build deterministic replay bundle format and loader.
- Freeze benchmark corpus (100 internal + 50 OSS tasks).
- Implement run scorer (success/retry/unsafe-ops/accept-rate).
- Add dashboard snapshot command and weekly report artifact.

### Phase 1 (Weeks 3-6)
- Patch preflight: op dependency and anchor stability graph.
- Patch preflight: language parse checks in dry-run mode.
- Checkpoint transaction manager for step-attempt rollback.
- Candidate patch ranking executor (`N` candidates, scored selection).
- Regression pack for syntax/indent/anchor-drift failures.

### Phase 2 (Weeks 7-8, first half)
- Plan graph schema v2 (`preconditions/postconditions/verification`).
- Critic taxonomy integration and targeted retry contract.
- Rules resolution engine (global/workspace/repo/task precedence).
- Failure telemetry table and top-cluster query surface.

## Milestone Rhythm
- Every 2 weeks:
  milestone review with pass/fail against phase exit gate.
- Every week:
  benchmark review and top 5 failure clusters.
- Monthly:
  competitor parity review and re-prioritization pass.
