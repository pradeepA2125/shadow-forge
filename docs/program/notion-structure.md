# Notion Program Structure (Parity+ Program)

Updated: March 12, 2026

## Top-Level Pages
1. `AI Editor 6-Month Strategy (PRD)`
2. `AI Editor Architecture Program`
3. `AI Editor Decision Log (ADR)`
4. `AI Editor Benchmark and Competitor Review`

## Page Templates

### PRD Template
- Goal and user segment
- Success metrics
- Non-goals
- Phase map (0..5)
- Risks and mitigations
- Exit gates by phase

### Architecture Phase Template
- Phase objective
- Existing behavior
- Planned API/contract changes
- Data model changes
- Failure modes and safety controls
- Test strategy
- Rollout and fallback

### ADR Template
- Context
- Decision
- Alternatives considered
- Consequences
- Rollback trigger
- Related Linear tickets

### Competitor Review Template
- Snapshot date
- Competitor capability map
- Parity status (`missing`, `partial`, `met`, `exceeds`)
- Evidence links
- Priority changes for next month

## Sync Rules (Linear <-> Notion)
- Linear issue links to one Notion design/spec page.
- Notion design/spec page lists linked Linear issue IDs.
- ADR page must be linked for every architecture-affecting milestone decision.
- Monthly competitor review updates roadmap priorities and links impacted Linear tickets.
- Sync activity and exceptions are tracked in `docs/program/notion-sync-log.md`.
