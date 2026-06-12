# Chat UI v2 — Handoff (2026-06-12, session budget cutoff)

**Where:** git worktree `.claude/worktrees/chat-ui-redesign`, branch `chat-ui-redesign`.
**Execution plan being followed:** `docs/superpowers/plans/2026-06-12-chat-ui-v2-tier-a.md` (committed `48c22b6`) — execute with superpowers:executing-plans, task-by-task, TDD. Every wire-format claim in it was verified against source on 2026-06-12; re-verify anchors if the tree has moved.

## Tier A status (the plan above)

| Plan task | Status | Commit |
|---|---|---|
| 1. Thread-list enrichment (chips, counts, updated_at) | ✅ done | `1551072` |
| 2. `unified_diff` on DiffEntry payloads (capped 400 lines/24k) | ✅ done | `fee912c` |
| 3. Tabbed DiffPanes in DiffCard + StepGate | ✅ done | `17cd87f` |
| 4. Step-review diff records + auto-accept breadcrumb | ⚠ backend done (`a575ae1`); **remaining: plan Task 4 Step 5** — one vitest case in `DiffCard.test.tsx` (resolved step-record renders inert with panes) | `a575ae1` |
| 5. "Review each step" composer toggle | ❌ not started — plan Task 5 has full plumbing code (route→agent→engine + client→panel→controller + InputArea checkbox) | — |
| Final: live smoke + CLAUDE.md docs | ❌ not started — smoke checklist in plan "Final" section | — |

**Verification state at cutoff:** agentd-py targeted suites green (`test_thread_summaries`, `test_unified_diff_wire`, `test_step_review_record`, `test_gate_breadcrumbs`, chat route suites); webview 152 tests green (9 files) + built; editor-client rebuilt (extension types off its dist — rebuild it after any contract change); extension typecheck OK. Full pytest has 11 PRE-EXISTING failures unrelated to this work (gemini/groq transports + `@requires_live_snapshot` graph-walker) — identical on clean tree.

**Resume Tier A:** open the plan doc, finish Task 4 Step 5, then Task 5 (all code is in the plan), then the Final smoke (backend on :8001 via worktree `scripts/stress/start-backend.sh --backend turboquant`, dev-host recipe + Playwright caveats in auto-memory `project_chat_ui_redesign_plan_status.md`; `browser_wait_for` does NOT pierce webview iframes — snapshot+grep instead). Update CLAUDE.md chat section per the Final step list.

## Tier B — needs ONE brainstorming session before any code (user owns the semantics)

Source list: "Deferred to v2" in `docs/superpowers/plans/2026-06-09-chat-ui-redesign.md:1065`. Brainstorm these TOGETHER (all are facets of task-lifecycle semantics + run persistence; separate designs will conflict):

1. **Cooperative task abort (F12)** — abort flag checked between ToolLoop iterations/steps, ABORTED-aware saves, shadow cleanup after coroutine ack. Unlocks the Stop button during execution. Open questions: what does Stop mean mid-step (finish step? checkpoint rollback?); interaction with partial promote.
2. **Final-review redesign (F8)** — READY_FOR_REVIEW is hollow (`engine.py` partial-promote TODO): collapse accept→SUCCEEDED, or true final reject via checkpoint-based workspace restore. Decides what ReviewCard's buttons honestly promise.
3. **Durable failure detail (F9) + durable run summary (F8)** — persist `failure_summary` and `run_summary` on the task, expose via `/live`/TaskResult, so ErrorCard and ReviewCard survive reloads without extension-observed state. Design as one "durable task telemetry" shape.
4. (From the same list, decide placement during brainstorm:) structured plan steps at the approval gate — requires moving markdown→JSON conversion before approval; previously judged not worth it.

## Tier C — independent product extras (separate plan, later)

Token/cost per turn, @mentions, model selector, light-theme adaptation (map surfaces to `--vscode-*`, violet ramp stays brand). No design dependencies on A/B.

## Context a fresh session needs

- Read auto-memory `project_chat_ui_redesign_plan_status.md` first — it has the VS-Code-via-Playwright-MCP driving recipe, the 2026-06-12 session's commits (breadcrumbs `22d0e5d`, tool pills `7a163d9`, smoke fixes `bba9223`, command-only steps `7771a0c`, contract+classifier `7903c92`), and the live-smoke caveats.
- CLAUDE.md (worktree copy) documents all chat invariants (Class-A live cards, breadcrumbs, tool_events, command-only steps).
- The whole branch (~30 commits from `3ee8040`) is unmerged; merge/PR decision deferred until Tier A lands.
- Background processes possibly still running from the smoke session: agentd on :8001, TurboQuant llama-server on :11435, CDP VS Code on :9335.
