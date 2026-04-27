# Agentic Planning + Delta Replan Design

**Date**: 2026-04-27  
**Status**: Approved  
**Scope**: Phase 5 — Agentic Planning Agent and Delta Replan Signal

---

## Problem Statement

The current planning system is static: retrieval fetches files once, the planner sees only what retrieval grabbed, and has no ability to go verify targets. This causes Scenario 1 (wrong file targets) to fail silently — the execution agent discovers the error mid-step with no clean path to correct it. The solution is two cooperating agents with clean handoffs through shared state.

---

## Architecture Overview

Two agents with distinct responsibilities:

```
PlanningAgent     — owns plan correctness. Explores the workspace before committing.
                    Invoked at task start and on delta replan requests.

ExecutionAgent    — owns code correctness. Implements one step at a time.
                    Signals PlanningAgent when a step's approach is fundamentally wrong.
```

Communication channel: `TaskRecord` in the task store. No exceptions cross agent boundaries. No in-memory object references passed between agents. The store is the single source of truth both agents read and write.

---

## Part 1 — PlanningAgent

### Loop Structure: Explore-then-Commit

Unlike `ToolLoop` (which iterates toward `emit_patch` as fast as possible), `PlanningAgent` separates into two phases within a single bounded loop:

```
Iterations 1..N  →  tool_call   (broad exploration: search, read, navigate)
Iteration N+1    →  emit_plan   (commit: produce the markdown plan)
```

The agent controls when it transitions. The system prompt instructs it to explore broadly before committing, not commit at the first opportunity. Budget is higher than execution: `max_planning_tool_calls = 20`.

### Tools: Read-Only

```
search_code      — ripgrep across shadow workspace
read_file        — file contents with optional line range
search_semantic  — vector similarity search
list_directory   — directory listing (planning-specific: navigate project structure)
```

No `run_command`. Planning is read-only — matching industry practice (Cursor, Copilot Workspace, Claude Code all keep planning read-only).

### Output Schema: Two Actions

**`tool_call`** — same shape as execution agent (tool + args + thought).

**`emit_plan`**:
```json
{
  "type": "emit_plan",
  "thought": "...",
  "plan_markdown": "# Plan\n...",
  "files_examined": ["src/auth/middleware.py", "src/api/routes.py"],
  "confidence": "high"
}
```

`files_examined` — shown to the user alongside the plan ("Agent examined 7 files before planning").  
`confidence: "low"` — surfaces as a warning diagnostic on the plan. Does not block approval. Tells the user the agent was uncertain.

### `emit_revision` (delta replan only)

```json
{
  "type": "emit_revision",
  "thought": "...",
  "revised_steps": [
    {
      "step_id": "s3",
      "targets": [{"path": "src/auth/middleware.py", "intent": "EXISTING"}],
      "goal": "Add structured logging to authenticate() in middleware.py",
      "implementation_details": "..."
    }
  ],
  "reverted_step_ids": ["s2"],
  "revision_summary": "authenticate() found in middleware.py. Steps s3 and s4 retargeted. s2 reverted."
}
```

`revised_steps` — partial updates only. Unmentioned fields stay as-is.  
`reverted_step_ids` — completed steps that must be rolled back. Planning agent may only list steps that have a checkpoint in `execution_state.step_checkpoints`. If no checkpoint exists for a completed step, the agent must adapt the plan to work forward from the current shadow state rather than reverting.

### SSE Events (Fully Visible)

All planning tool calls stream to VS Code in real-time, prefixed to distinguish from execution events:

```json
{"type": "planning_tool_call", "tool": "search_code", "thought": "Finding where authenticate() lives", "iteration": 1}
{"type": "planning_tool_result", "tool": "search_code", "output": "src/auth/middleware.py:12...", "iteration": 1}
{"type": "planning_complete", "files_examined": ["src/auth/middleware.py"], "confidence": "high"}
```

### One-Step-Per-File Constraint

Each file may appear in at most one step's `targets` across the entire plan. This is enforced at two levels:

**1. Planning prompt rule** (`agentd/planning/prompts.py`):

> All changes to a given file must be consolidated into a single step. Never list the same file path in more than one step's targets.

This replaces the old `prompt_builder.py` guidance "Add imports before using symbols" which inadvertently encouraged splitting a file across steps (e.g., "step 1: add import; step 2: use it").

**2. Post-emit validation in `PlanningLoop`** (before returning `PlanningResult`):

```python
def _validate_no_duplicate_file_targets(plan: Plan) -> list[str]:
    seen: dict[str, str] = {}  # path → step_id
    errors = []
    for step in plan.steps:
        for target in step.targets:
            if target.path in seen:
                errors.append(
                    f"File '{target.path}' appears in both step '{seen[target.path]}' "
                    f"and step '{step.id}'. Consolidate all changes to this file into one step."
                )
            else:
                seen[target.path] = step.id
    return errors
```

If errors are found, `PlanningLoop` re-invokes the planning agent with the errors injected into context (up to 2 correction attempts before failing the task). This is the only post-plan validation remaining — it replaces `_validate_plan_grounding()` for this specific invariant.

The same validation runs inside `_apply_revision()` after step replacements, catching any revision that accidentally introduces a cross-step file collision.

### Static Retrieval as Seed Context

`load_context()` still runs before `PlanningAgent`. Its output is passed as `initial_context` — a head start, not a constraint. The planning agent can discover files retrieval missed.

The 3-round critique loop (`critique_markdown_plan`), `_validate_plan_grounding`, and `critique_json_plan` are removed. The planning agent verifies targets inline before committing — these checks exist solely as workarounds for static retrieval.

### `PlanningResult`

```python
class PlanningResult(BaseModel):
    plan_markdown: str
    files_examined: list[str]
    confidence: Literal["high", "medium", "low"]
    tool_trace: AgentToolTrace  # written to <task_id>/planning-trace.json
```

---

## Part 2 — Integration with Engine

### What Changes in `run_task()` / `continue_task()`

**Before** (current):
```
load_context()                           ← static retrieval
_generate_repo_grounded_markdown_plan()  ← single LLM call + critique loop
→ AWAITING_PLAN_APPROVAL
```

**After**:
```
load_context()                 ← still runs, produces seed context only
PlanningAgent.generate_plan()  ← agentic loop replaces both above
→ AWAITING_PLAN_APPROVAL
```

`_generate_repo_grounded_markdown_plan()` is deleted along with the critique and grounding validation logic inside it.

### Two Approval Gates

**Gate 1 — Markdown approval** (existing, mandatory):  
Planning agent emits markdown → task transitions to `AWAITING_PLAN_APPROVAL` → user reviews in VS Code panel → approves.

**Gate 2 — JSON plan review** (optional, VS Code flag):  
After markdown approval, `create_plan()` generates the executable JSON plan. If `aiEditor.jsonPlanReviewMode = true`, VS Code asks "Review JSON plan?" — user can inspect individual steps and trigger delta step edits (user-initiated, pre-execution) before confirming. If flag is off (default), JSON is generated and execution starts automatically.

Gate 2 is handled entirely in the VS Code extension. The backend state machine is unchanged: `AWAITING_PLAN_APPROVAL` → `PLANNED` → `EXECUTING`.

### `ReasoningEngine` Contract Additions

```python
async def create_planning_step(
    self,
    plan_context: dict[str, object],
    history: list[dict[str, object]],
    tool_definitions: list[dict[str, object]],
) -> dict[str, object]:
    """One turn of the planning ReAct loop. Returns tool_call or emit_plan/emit_revision."""
    ...
```

`ScriptedReasoningEngine` gets `create_planning_step()` alongside `create_tool_step()`.

### Budget Extensions to `TaskBudget`

```python
class TaskBudget(BaseModel):
    ...existing fields...
    max_planning_tool_calls: int = 20   # planning agent budget (generate_plan)
    max_revision_tool_calls: int = 10   # planning agent budget per revise() call
    max_delta_replans: int = 3          # guard against infinite planning ↔ execution loops
```

---

## Part 3 — Delta Replan Signal and Handoff

### The Signal: `revision_needed` in Execution Agent Schema

New action type added to `AGENT_STEP_RESPONSE_SCHEMA`:

```json
{
  "type": "revision_needed",
  "thought": "authenticate() is in middleware.py not routes.py — step cannot be completed as planned",
  "reason": "Step target is wrong: function is not in the planned file",
  "evidence": "search_code('def authenticate') → src/auth/middleware.py:12",
  "affected_steps": ["s3"]
}
```

`affected_steps` is the execution agent's hint. The planning agent uses it as a starting point but is not bound by it — it may determine other steps are also affected after re-exploring.

### `ToolLoop.run()` Returns a Discriminated Union

No exceptions cross agent boundaries. `ToolLoop.run()` returns a typed result:

```python
@dataclass
class PatchResult:
    patch_document: dict[str, object]
    tool_trace: AgentToolTrace

@dataclass
class PlanHandoff:
    step_id: str
    reason: str
    evidence: str
    hinted_affected_steps: list[str]
    tool_trace: AgentToolTrace

StepOutcome = PatchResult | PlanHandoff
```

When the agent emits `revision_needed`, `ToolLoop.run()` builds a `PlanHandoff` and returns it. The `PlanHandoff` is a first-class output — the same pattern as a "handoff tool" in OpenAI Agents SDK.

### `PlanRevisionResult`

What `PlanningAgent.revise()` returns:

```python
class RevisedStep(BaseModel):
    step_id: str
    goal: str
    targets: list[dict[str, str]]
    implementation_details: str
    edge_cases: str = ""
    testing_strategy: str = ""
    risk: str = "low"

class PlanRevisionResult(BaseModel):
    revised_steps: list[RevisedStep]   # full step replacements; unmentioned steps unchanged
    reverted_step_ids: list[str]       # completed steps to roll back (must have checkpoints)
    revision_summary: str              # human-readable; shown in VS Code activity log
    tool_trace: AgentToolTrace         # written to delta-replan-<N>.json artifact
```

The planning agent's `emit_revision` JSON is parsed into `PlanRevisionResult` by `PlanningLoop`. Each `RevisedStep` is a complete step definition — `PlanStep` is replaced wholesale. Steps not listed in `revised_steps` are left exactly as-is.

### Shared State: `TaskExecutionState`

Both agents communicate through `TaskRecord` in the task store:

```python
class TaskExecutionState(BaseModel):
    current_step_id: str | None = None
    step_checkpoints: dict[str, str] = {}        # step_id → checkpoint_path
    delta_replan_requests: list[DeltaReplanRequest] = []
    delta_replans_used: int = 0

class DeltaReplanRequest(BaseModel):
    requested_by_step_id: str
    reason: str
    evidence: str
    hinted_affected_steps: list[str]
    requested_at: datetime
```

`TaskRecord` gains `execution_state: TaskExecutionState`.

### `PlanningAgent` Instantiation

`PlanningAgent` is created once per task execution in `_execute_plan()`, alongside `ToolLoop`:

```python
planning_registry = PlanningToolRegistry(shadow_root=shadow_path)
planning_agent = PlanningAgent(
    reasoning_engine=self._reasoning_engine,
    registry=planning_registry,
    broadcaster=self.broadcaster,
    task_id=task.task_id,
)
```

It is stateless — `revise()` creates a fresh loop each call. No state is held between delta replans on the instance itself; all state lives in `TaskRecord`.

### Orchestrator Dispatch

```python
outcome = await tool_loop.run(step, context, budget, usage)

if isinstance(outcome, PlanHandoff):
    # Guard: max delta replans
    if task.execution_state.delta_replans_used >= task.budget.max_delta_replans:
        task.diagnostics.append(Diagnostic(
            source="orchestrator",
            message=f"Delta replan budget exhausted ({task.budget.max_delta_replans} replans used). "
                    f"Last request: {outcome.reason}",
            level="error",
        ))
        task = transition(task, TaskStatus.FAILED, "delta replan budget exhausted")
        await self._store.save(task)
        return task

    # Write to shared state before handoff — planning agent reads this
    task.execution_state.delta_replan_requests.append(DeltaReplanRequest(
        requested_by_step_id=outcome.step_id,
        reason=outcome.reason,
        evidence=outcome.evidence,
        hinted_affected_steps=outcome.hinted_affected_steps,
        requested_at=datetime.now(timezone.utc),
    ))
    await self._store.save(task)

    # Hand off to planning agent automatically — no user gate.
    # Planning agent reads real_path (unmodified workspace); execution continues inline.
    revision = await planning_agent.revise(task, real_path)
    await self._apply_revision(task, revision, shadow_path)
    # Loop continues — _next_incomplete_step() returns the right step

elif isinstance(outcome, PatchResult):
    # existing patch processing path unchanged
```

### Step Execution Loop Structure

```python
while (step := self._next_incomplete_step(task)) is not None:
    outcome = await tool_loop.run(step, ...)

    if isinstance(outcome, PlanHandoff):
        ...handoff flow...
        # _apply_revision updates task.completed_step_ids and task.plan.steps
        # loop naturally restarts from earliest incomplete step

    elif isinstance(outcome, PatchResult):
        # apply patch, validate...
        task.completed_step_ids.append(step.id)
```

```python
def _next_incomplete_step(self, task: TaskRecord) -> PlanStep | None:
    completed = set(task.completed_step_ids)
    return next((s for s in task.plan.steps if s.id not in completed), None)
```

No `while True`, no `break`, no index tracking. State drives the loop. After `_apply_revision()` removes rolled-back steps from `completed_step_ids`, `_next_incomplete_step()` returns them naturally on the next iteration.

### `PlanningAgent.revise()`

Same explore-then-commit loop as `generate_plan()`, narrower mandate.

**Tools read from actual workspace (`real_path`), not shadow.** The planning agent always reasons about the original codebase. `PlanningToolRegistry` is constructed with `real_path`; `ToolRegistry` (execution) uses `shadow_path`. These never swap.

**Revision context payload:**

```python
{
    "goal": task.goal,
    "plan_steps": [
        {
            "step_id": s.id,
            "goal": s.goal,
            "targets": [{"path": t.path, "intent": t.intent} for t in s.targets],
            "implementation_details": s.implementation_details,
            "status": "completed" if s.id in completed_set
                      else "failed" if s.id == request.requested_by_step_id
                      else "pending",
        }
        for s in task.plan.steps
    ],
    "revision_request": {
        "failed_step_id": request.requested_by_step_id,
        "reason": request.reason,
        "evidence": request.evidence,
        "hinted_affected_steps": request.hinted_affected_steps,
    },
    "revertable_step_ids": list(task.execution_state.step_checkpoints.keys()),
}
```

**System prompt addition for revision mode** (appended to base planning prompt):

```
REVISION MODE:
You are fixing a specific failed step, not creating a new plan.

plan_steps shows status: completed / failed / pending.
- completed: do NOT modify unless also listed in reverted_step_ids
- failed: this is the step you MUST fix
- pending: revise freely if evidence shows they are also affected

You may only list a step in reverted_step_ids if it appears in
revertable_step_ids. If no checkpoint exists, write the revision
to work forward from its current output instead.

Read files from the actual workspace (original, unmodified).
Verify the evidence in revision_request before deciding what to change.
Only revise what the evidence justifies — do not restructure unaffected steps.
```

**Budget:** `max_revision_tool_calls: int = 10` in `TaskBudget` — separate from `max_planning_tool_calls = 20`. Less exploration needed since the execution agent already found the issue.

**`emit_revision` output — full step replacements, not partial diffs:**

```json
{
  "type": "emit_revision",
  "thought": "Confirmed authenticate() is in middleware.py. Replacing s3 entirely.",
  "revised_steps": [
    {
      "step_id": "s3",
      "goal": "Add structured logging to authenticate() in src/auth/middleware.py",
      "targets": [{"path": "src/auth/middleware.py", "intent": "existing"}],
      "implementation_details": "Import structlog. In authenticate(), add log.info('auth_attempt', user=user) before return.",
      "edge_cases": "Do not log passwords or tokens.",
      "testing_strategy": "Verify log line appears in test output.",
      "risk": "low"
    }
  ],
  "reverted_step_ids": [],
  "revision_summary": "authenticate() is in middleware.py not routes.py. Step s3 retargeted."
}
```

Each `revised_steps` entry is a complete step definition. Steps not listed are left exactly as-is.

**Merging into plan — wholesale replacement:**

```python
step_index = {s.id: i for i, s in enumerate(task.plan.steps)}
for revised in revision.revised_steps:
    i = step_index[revised.step_id]
    task.plan.steps[i] = PlanStep(
        id=revised.step_id,
        goal=revised.goal,
        targets=[PlanTarget(**t) for t in revised.targets],
        implementation_details=revised.implementation_details,
        edge_cases=revised.edge_cases,
        testing_strategy=revised.testing_strategy,
        risk=revised.risk,
    )
```

### `_apply_revision()`

```python
async def _apply_revision(self, task, revision, shadow_path) -> None:
    # 1. Roll back stale completed steps in reverse order
    for step_id in reversed(revision.reverted_step_ids):
        checkpoint_path = task.execution_state.step_checkpoints.get(step_id)
        if checkpoint_path:
            self._restore_shadow_checkpoint(shadow_path, checkpoint_path)
            task.completed_step_ids.remove(step_id)
            task.modified_files = self._recompute_modified_files(task)

    # 2. Replace steps wholesale (full definitions, not partial diffs)
    step_index = {s.id: i for i, s in enumerate(task.plan.steps)}
    for revised in revision.revised_steps:
        i = step_index[revised.step_id]
        task.plan.steps[i] = PlanStep(
            id=revised.step_id,
            goal=revised.goal,
            targets=[PlanTarget(**t) for t in revised.targets],
            implementation_details=revised.implementation_details,
            edge_cases=revised.edge_cases,
            testing_strategy=revised.testing_strategy,
            risk=revised.risk,
        )

    # 3. Increment counter
    task.execution_state.delta_replans_used += 1

    # 4. Persist — _next_incomplete_step() reads from this
    await self._store.save(task)

    # 5. Artifact + broadcast
    self._write_debug_artifact(task.task_id, "delta-replan", revision.model_dump())
    self.broadcaster.broadcast(task.task_id, {
        "type": "delta_replan_applied",
        "revised_steps": [s.step_id for s in revision.revised_steps],
        "reverted_steps": revision.reverted_step_ids,
        "summary": revision.revision_summary,
    })
```

---

## Configuration Flags (VS Code Settings)

| Setting | Type | Default | Effect |
|---|---|---|---|
| `aiEditor.jsonPlanReviewMode` | boolean | `false` | Show JSON plan before execution; allow delta edits |

Delta replan is always automatic — no user gate. The `delta_replan_applied` SSE event keeps the activity log visible without blocking execution.

---

## New Files

| File | Purpose |
|---|---|
| `agentd/planning/agent.py` | `PlanningAgent` class: `generate_plan()`, `revise()` |
| `agentd/planning/loop.py` | `PlanningLoop`: explore-then-commit loop |
| `agentd/planning/registry.py` | `PlanningToolRegistry`: read-only tool set + `list_directory` |
| `agentd/planning/prompts.py` | `PLANNING_SYSTEM_PROMPT`, `REVISION_SYSTEM_PROMPT`, output schemas |
| `agentd/planning/__init__.py` | Re-exports |

## Changed Files

| File | Change |
|---|---|
| `agentd/domain/models.py` | Add `TaskExecutionState`, `DeltaReplanRequest`, `PlanningResult`, `PlanHandoff`, `RevisedStep`, `PlanRevisionResult`; extend `TaskBudget` (3 new fields), `TaskRecord` |
| `agentd/tools/loop.py` | Return `StepOutcome = PatchResult \| PlanHandoff`; handle `revision_needed` action |
| `agentd/reasoning/tool_prompts.py` | Add `revision_needed` to execution agent schema |
| `agentd/reasoning/contracts.py` | Add `create_planning_step()` to `ReasoningEngine` protocol |
| `agentd/reasoning/engine.py` | Implement `create_planning_step()` |
| `agentd/orchestrator/scripted_engine.py` | Add `create_planning_step()` stub |
| `agentd/orchestrator/engine.py` | Replace `_generate_repo_grounded_markdown_plan()` with `PlanningAgent.generate_plan()`; replace step for-loop with `while _next_incomplete_step()`; add `PlanHandoff` dispatch; add `_apply_revision()` |

## Deleted

- `_generate_repo_grounded_markdown_plan()` and all critique/validation logic inside it
- `_validate_plan_grounding()`
- The 3-round `critique_json_plan` loop in `continue_task()`

---

## Artifacts

```
<task_id>/
  planning-trace.json          # tool calls made by planning agent during generate_plan()
  delta-replan-<N>.json        # revision result for Nth delta replan
  step-<id>/tool-trace.json    # execution agent tool calls (existing)
```

---

## Verification Checklist

1. **Planning agent discovers correct file**: Task "add logging to authenticate()". Planning trace shows `search_code("def authenticate")` before emitting plan. Plan targets `middleware.py`, not `routes.py`.
2. **Planning agent emits low-confidence warning**: Feed it a goal with ambiguous file targets. Confirm `confidence: "low"` appears as a diagnostic on the plan shown to user.
3. **Execution agent triggers delta replan**: Force execution agent to emit `revision_needed`. Confirm `PlanHandoff` returned (no exception). Confirm `delta_replan_requests` written to task store before planning agent is invoked.
4. **Planning agent reads shared state**: Confirm planning agent in revision mode reads `completed_step_ids` and does not list them in `reverted_step_ids`.
5. **Cascade rollback**: Delta replan reverts a completed step. Confirm shadow checkpoint restored, `completed_step_ids` updated, `_next_incomplete_step()` returns reverted step on next iteration.
6. **No-checkpoint constraint**: Completed step with no checkpoint listed in `reverted_step_ids`. Confirm `_apply_revision()` skips the rollback (checkpoint not found). Plan adapts forward.
7. **`_next_incomplete_step()` drives the loop**: Confirm step loop re-executes reverted steps without any explicit restart signal.
8. **Max delta replans guard**: Set `max_delta_replans = 1`, trigger two delta replans. Confirm second triggers `FAILED` with clear diagnostic.
9. **Automatic delta replan**: Confirm delta replan fires inline without user intervention. `delta_replan_applied` SSE event appears in the activity log with `revised_steps` and `summary`.
10. **`aiEditor.jsonPlanReviewMode = true`**: Confirm VS Code shows JSON plan after markdown approval, allows step edits, then starts execution on confirm.
11. **Deleted code paths removed**: Confirm `critique_markdown_plan`, `_validate_plan_grounding`, `critique_json_plan` loop are gone. All tests still pass.
12. **`ScriptedReasoningEngine`**: Confirm `create_planning_step()` stub added; existing tests unaffected.
13. **One-step-per-file validation**: Submit a task whose goal naturally touches one file in two steps. Confirm `PlanningLoop` catches the collision, re-invokes the agent with the error, and the second plan consolidates the file into one step.
14. **Delta replan one-step-per-file**: Force a revision that puts the same file in two steps. Confirm `_apply_revision()` validation rejects it and the task fails with a clear diagnostic rather than silently producing a corrupt plan.
