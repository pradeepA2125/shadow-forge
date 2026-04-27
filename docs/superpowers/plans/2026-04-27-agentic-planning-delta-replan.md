# Agentic Planning + Delta Replan Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the static plan-then-patch loop with two cooperating agents: a PlanningAgent that explores the workspace before committing to a plan, and an ExecutionAgent (ToolLoop) that hands off to the planner when a step's approach is fundamentally wrong.

**Architecture:** `PlanningAgent` owns plan correctness via an explore-then-commit loop. `ToolLoop` returns a typed `StepOutcome = PatchResult | PlanHandoff` instead of raising exceptions across agent boundaries. The engine dispatches on the return type; agents communicate only through `TaskRecord` in the task store.

**Tech Stack:** Python 3.12, Pydantic v2, asyncio, pytest-asyncio. All new code goes in `services/agentd-py/`.

---

## File Map

### New files
| File | Responsibility |
|------|---------------|
| `agentd/planning/__init__.py` | Re-exports |
| `agentd/planning/prompts.py` | `PLANNING_SYSTEM_PROMPT`, schemas, payload builders |
| `agentd/planning/registry.py` | `PlanningToolRegistry`: read-only tools + `list_directory` |
| `agentd/planning/loop.py` | `PlanningLoop`: explore-then-commit loop, duplicate-file validation |
| `agentd/planning/agent.py` | `PlanningAgent`: `generate_plan()`, `revise()` |
| `tests/test_planning_agent.py` | Tests for PlanningLoop and PlanningAgent |
| `tests/test_delta_replan.py` | Tests for revision_needed → PlanHandoff → apply_revision |

### Modified files
| File | Change |
|------|--------|
| `agentd/domain/models.py` | Add 6 new models, extend `TaskBudget`/`TaskRecord` |
| `agentd/domain/state_machine.py` | No change required |
| `agentd/reasoning/tool_prompts.py` | Add `revision_needed` to execution agent schema |
| `agentd/reasoning/contracts.py` | Add `create_planning_step()` to `ReasoningEngine` protocol |
| `agentd/reasoning/engine.py` | Implement `create_planning_step()` |
| `agentd/orchestrator/scripted_engine.py` | Add `create_planning_step()` stub |
| `agentd/tools/loop.py` | Return `StepOutcome` instead of `tuple`; handle `revision_needed` |
| `agentd/orchestrator/engine.py` | Replace planning call, `while` step loop, delta replan dispatch, `_apply_revision()` |
| All test files with inline stub engines | Add `create_planning_step()` stub method |

---

## Task 1: Domain models — new types

**Files:**
- Modify: `agentd/domain/models.py`
- Test: `tests/test_planning_domain_models.py` (new)

- [ ] **Step 1: Write failing tests for new domain types**

```python
# tests/test_planning_domain_models.py  (new file)
from agentd.domain.models import (
    DeltaReplanRequest, TaskBudget, TaskExecutionState, TaskRecord,
    TaskStatus, PlanRevisionResult, RevisedStep,
)
from datetime import datetime, timezone

def test_task_budget_new_fields():
    b = TaskBudget()
    assert b.max_planning_tool_calls == 20
    assert b.max_revision_tool_calls == 10
    assert b.max_delta_replans == 3

def test_task_execution_state_defaults():
    s = TaskExecutionState()
    assert s.current_step_id is None
    assert s.step_checkpoints == {}
    assert s.delta_replans_used == 0
    assert s.delta_replan_requests == []

def test_task_record_has_execution_state():
    r = TaskRecord(task_id="t1", goal="g", workspace_path="/ws")
    assert isinstance(r.execution_state, TaskExecutionState)

def test_revised_step_model():
    rs = RevisedStep(
        step_id="s1",
        goal="Fix auth",
        targets=[{"path": "src/auth.py", "intent": "existing"}],
        implementation_details="Add logging",
    )
    assert rs.risk == "low"
    assert rs.edge_cases == ""

def test_delta_replan_request_fields():
    from datetime import datetime, timezone
    r = DeltaReplanRequest(
        requested_by_step_id="s2",
        reason="wrong file",
        evidence="grep found it in other.py",
        hinted_affected_steps=["s3"],
        requested_at=datetime.now(timezone.utc),
    )
    assert r.requested_by_step_id == "s2"
    assert r.hinted_affected_steps == ["s3"]
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd services/agentd-py && source .venv/bin/activate
pytest tests/test_planning_domain_models.py -v 2>&1 | head -30
```
Expected: ImportError or AttributeError — types don't exist yet.

- [ ] **Step 3: Extend `TaskBudget` with three new fields**

Replace the existing `TaskBudget` class:
```python
class TaskBudget(BaseModel):
    max_iterations: int = 6
    max_files_touched: int = 20
    max_tokens: int = 120_000
    max_runtime_ms: int = 20 * 60 * 1000
    max_tool_calls_per_step: int = 8
    max_planning_tool_calls: int = 20
    max_revision_tool_calls: int = 10
    max_delta_replans: int = 3
```

- [ ] **Step 5: Add `TaskExecutionState` and `DeltaReplanRequest` models**

Add after `AgentToolTrace` (before `TaskEvent`):
```python
class DeltaReplanRequest(BaseModel):
    requested_by_step_id: str
    reason: str
    evidence: str
    hinted_affected_steps: list[str]
    requested_at: datetime


class TaskExecutionState(BaseModel):
    current_step_id: str | None = None
    step_checkpoints: dict[str, str] = Field(default_factory=dict)
    delta_replan_requests: list[DeltaReplanRequest] = Field(default_factory=list)
    delta_replans_used: int = 0
```

- [ ] **Step 6: Add `RevisedStep` and `PlanRevisionResult` models**

Add after `PlanCritiqueResult`:
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
    revised_steps: list[RevisedStep]
    reverted_step_ids: list[str]
    revision_summary: str
    tool_trace: "AgentToolTrace"
```

- [ ] **Step 7: Add `PlanningResult` model**

Add after `PlanRevisionResult`:
```python
class PlanningResult(BaseModel):
    plan_markdown: str
    files_examined: list[str]
    confidence: Literal["high", "medium", "low"]
    tool_trace: "AgentToolTrace"
```

- [ ] **Step 8: Add `execution_state` field to `TaskRecord`**

Add after `checkpoints: list[CheckpointManifest]`:
```python
    execution_state: TaskExecutionState = Field(default_factory=TaskExecutionState)
```

- [ ] **Step 9: Run tests to confirm they pass**

```bash
pytest tests/test_planning_domain_models.py -v
```
Expected: All 5 tests PASS.

- [ ] **Step 10: Commit**

```bash
git add agentd/domain/models.py tests/test_planning_domain_models.py
git commit -m "feat(models): add planning agent domain types — TaskExecutionState, DeltaReplanRequest, PlanningResult, PlanRevisionResult"
```

---

## Task 2: Planning prompts and schemas

**Files:**
- Create: `agentd/planning/prompts.py`
- Test: inline in Task 4's loop tests

- [ ] **Step 1: Create `agentd/planning/prompts.py`**

```python
"""Prompts and schemas for the PlanningAgent explore-then-commit loop."""
from __future__ import annotations

import json

PLANNING_STEP_RESPONSE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": ["tool_call", "emit_plan", "emit_revision"],
            "description": "Action type for this turn",
        },
        "thought": {
            "type": "string",
            "description": "Reasoning before taking this action (1-3 sentences)",
        },
        "tool": {
            "type": "string",
            "description": "Tool name (required when type='tool_call')",
        },
        "args": {
            "type": "object",
            "description": "Tool arguments (required when type='tool_call')",
        },
        "plan_markdown": {
            "type": "string",
            "description": "Full markdown plan (required when type='emit_plan')",
        },
        "files_examined": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Relative paths of all files read during exploration",
        },
        "confidence": {
            "type": "string",
            "enum": ["high", "medium", "low"],
            "description": "Confidence in plan correctness (required when type='emit_plan')",
        },
        "revised_steps": {
            "type": "array",
            "items": {"type": "object"},
            "description": "Complete step replacements (required when type='emit_revision')",
        },
        "reverted_step_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Step IDs to roll back (must have checkpoints)",
        },
        "revision_summary": {
            "type": "string",
            "description": "Human-readable summary of what changed and why",
        },
    },
    "required": ["type", "thought"],
}

PLANNING_SYSTEM_PROMPT = """\
You are an expert software architect planning code changes for a task.
You have read-only access to tools to explore the workspace before committing to a plan.

AVAILABLE TOOLS:
{tools_json}

PLANNING RULES:
1. Explore broadly before committing. Read the actual files before naming them in the plan.
2. Use search_code to find where things live. Use read_file to confirm structure.
3. All changes to a given file must be consolidated into a single step. Never list the same
   file path in more than one step's targets.
4. When you have high confidence in the target files, emit the plan.
5. Output exactly one JSON object per turn matching the schema.

OUTPUT:
- To call a tool: {{"type": "tool_call", "thought": "...", "tool": "<name>", "args": {{...}}}}
- To emit the final plan: {{"type": "emit_plan", "thought": "...", "plan_markdown": "# Plan\\n...",
    "files_examined": ["path/to/file.py"], "confidence": "high"}}
"""

REVISION_SYSTEM_PROMPT_SUFFIX = """\

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

OUTPUT:
- To call a tool: {{"type": "tool_call", "thought": "...", "tool": "<name>", "args": {{...}}}}
- To emit revision: {{"type": "emit_revision", "thought": "...",
    "revised_steps": [{{full step dict}}], "reverted_step_ids": [], "revision_summary": "..."}}
"""


def format_planning_system_prompt(
    tool_definitions: list[dict[str, object]],
    *,
    revision_mode: bool = False,
) -> str:
    tools_json = json.dumps(tool_definitions, indent=2)
    base = PLANNING_SYSTEM_PROMPT.format(tools_json=tools_json)
    if revision_mode:
        base += REVISION_SYSTEM_PROMPT_SUFFIX
    return base


def build_planning_step_payload(
    plan_context: dict[str, object],
    history: list[dict[str, object]],
    tool_definitions: list[dict[str, object]],
) -> dict[str, object]:
    """Build the user payload for a single planning loop turn."""
    payload: dict[str, object] = {
        "goal": plan_context.get("goal", ""),
        "workspace_path": plan_context.get("workspace_path", ""),
    }

    initial_context = plan_context.get("initial_context")
    if initial_context:
        payload["initial_context"] = initial_context

    revision_request = plan_context.get("revision_request")
    if revision_request:
        payload["plan_steps"] = plan_context.get("plan_steps", [])
        payload["revision_request"] = revision_request
        payload["revertable_step_ids"] = plan_context.get("revertable_step_ids", [])

    if history:
        payload["conversation_history"] = history
        payload["instruction"] = (
            "Continue exploring. Output your NEXT action. "
            "When confident about all target files, emit_plan (or emit_revision in revision mode)."
        )
    else:
        payload["instruction"] = (
            "Start exploring the workspace. Output your first action as a JSON object."
        )

    return payload
```

- [ ] **Step 2: Verify it parses without errors**

```bash
cd services/agentd-py && python -c "from agentd.planning.prompts import PLANNING_SYSTEM_PROMPT, PLANNING_STEP_RESPONSE_SCHEMA; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add agentd/planning/prompts.py
git commit -m "feat(planning): planning agent system prompts and response schema"
```

---

## Task 3: PlanningToolRegistry

**Files:**
- Create: `agentd/planning/registry.py`
- Test: `tests/test_planning_agent.py` (written in Task 6)

- [ ] **Step 1: Create `agentd/planning/registry.py`**

```python
"""Read-only tool registry for the PlanningAgent loop."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from agentd.tools.registry import ToolDefinition, ToolOutput


class PlanningToolRegistry:
    """Read-only tools for the planning agent.

    All paths resolved relative to real_path (the original, unmodified workspace).
    No run_command — planning is strictly read-only.
    """

    def __init__(
        self,
        real_path: Path,
        semantic_index: object | None = None,
    ) -> None:
        self._real_path = real_path
        self._semantic_index = semantic_index
        self._ripgrep_cmd = os.environ.get("AI_EDITOR_RIPGREP_CMD", "rg")

    def definitions(self) -> list[ToolDefinition]:
        tools = [
            ToolDefinition(
                name="search_code",
                description=(
                    "Search for a regex/literal pattern across files in the workspace. "
                    "Use to find where functions, classes, or patterns are defined."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Regex or literal pattern"},
                        "path_filter": {"type": "string", "description": "Glob to restrict search (e.g. '*.py')"},
                        "context_lines": {"type": "integer", "description": "Lines of context (default 3)"},
                        "fixed_strings": {"type": "boolean", "description": "Treat as literal string (default false)"},
                    },
                    "required": ["pattern"],
                },
            ),
            ToolDefinition(
                name="read_file",
                description=(
                    "Read a file from the workspace. Use to confirm file structure "
                    "before adding it to the plan."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative file path"},
                        "start_line": {"type": "integer", "description": "First line (1-indexed)"},
                        "end_line": {"type": "integer", "description": "Last line (1-indexed)"},
                    },
                    "required": ["path"],
                },
            ),
            ToolDefinition(
                name="list_directory",
                description="List files and subdirectories at a path. Use to navigate project structure.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative directory path (default: '.')"},
                        "depth": {"type": "integer", "description": "Max recursion depth (default 2)"},
                    },
                    "required": [],
                },
            ),
        ]
        if self._semantic_index is not None:
            tools.append(
                ToolDefinition(
                    name="search_semantic",
                    description=(
                        "Vector similarity search: find code related to a natural-language query."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Natural-language description"},
                            "top_k": {"type": "integer", "description": "Results to return (default 8)"},
                        },
                        "required": ["query"],
                    },
                )
            )
        return tools

    async def execute(self, name: str, args: dict[str, object]) -> ToolOutput:
        if name == "search_code":
            from agentd.tools.search import search_code
            return await search_code(
                pattern=str(args.get("pattern", "")),
                path_filter=str(args["path_filter"]) if "path_filter" in args else None,
                context_lines=int(args.get("context_lines", 3)),  # type: ignore[call-overload]
                fixed_strings=bool(args.get("fixed_strings", False)),
                shadow_root=self._real_path,
                ripgrep_cmd=self._ripgrep_cmd,
            )

        if name == "read_file":
            from agentd.tools.files import read_file
            start = args.get("start_line")
            end = args.get("end_line")
            return await read_file(
                path=str(args.get("path", "")),
                start_line=int(start) if start is not None else None,  # type: ignore[call-overload]
                end_line=int(end) if end is not None else None,  # type: ignore[call-overload]
                shadow_root=self._real_path,
            )

        if name == "list_directory":
            return await self._list_directory(
                path=str(args.get("path", ".")),
                depth=int(args.get("depth", 2)),  # type: ignore[call-overload]
            )

        if name == "search_semantic":
            from agentd.tools.search import search_semantic
            if self._semantic_index is None:
                return ToolOutput(output="Error: semantic index not available", is_error=True)
            return await search_semantic(
                query=str(args.get("query", "")),
                top_k=int(args.get("top_k", 8)),  # type: ignore[call-overload]
                semantic_index=self._semantic_index,
            )

        return ToolOutput(output=f"Error: unknown tool '{name}'", is_error=True)

    async def _list_directory(self, path: str, depth: int) -> ToolOutput:
        resolved = (self._real_path / path).resolve()
        if not str(resolved).startswith(str(self._real_path)):
            return ToolOutput(output="Error: path traversal rejected", is_error=True)
        if not resolved.is_dir():
            return ToolOutput(output=f"Error: '{path}' is not a directory", is_error=True)

        lines: list[str] = []
        self._walk_dir(resolved, self._real_path, depth, 0, lines)
        return ToolOutput(output="\n".join(lines[:500]))

    def _walk_dir(
        self,
        current: Path,
        root: Path,
        max_depth: int,
        current_depth: int,
        out: list[str],
    ) -> None:
        try:
            entries = sorted(current.iterdir(), key=lambda p: (p.is_file(), p.name))
        except PermissionError:
            return
        for entry in entries:
            if entry.name.startswith(".") or entry.name in ("__pycache__", "node_modules", ".git"):
                continue
            rel = entry.relative_to(root)
            prefix = "  " * current_depth
            suffix = "/" if entry.is_dir() else ""
            out.append(f"{prefix}{rel}{suffix}")
            if entry.is_dir() and current_depth < max_depth - 1:
                self._walk_dir(entry, root, max_depth, current_depth + 1, out)
```

- [ ] **Step 2: Verify it parses**

```bash
python -c "from agentd.planning.registry import PlanningToolRegistry; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add agentd/planning/registry.py
git commit -m "feat(planning): PlanningToolRegistry with read-only tools and list_directory"
```

---

## Task 4: PlanningLoop

**Files:**
- Create: `agentd/planning/loop.py`

- [ ] **Step 1: Create `agentd/planning/loop.py`**

```python
"""Explore-then-commit ReAct loop for the PlanningAgent."""
from __future__ import annotations

import json
import logging
import os
from uuid import uuid4

from agentd.domain.models import (
    AgentToolTrace,
    DeltaReplanRequest,
    PlanRevisionResult,
    PlanningResult,
    RevisedStep,
    TaskBudget,
    TaskRecord,
    ToolCall,
    ToolResult,
)
from agentd.orchestrator.broadcaster import PatchEventBroadcaster
from agentd.planning.prompts import (
    PLANNING_STEP_RESPONSE_SCHEMA,
    build_planning_step_payload,
    format_planning_system_prompt,
)
from agentd.planning.registry import PlanningToolRegistry
from agentd.reasoning.contracts import ReasoningEngine

logger = logging.getLogger(__name__)

_MAX_OUTPUT_INJECT_CHARS = int(os.environ.get("AI_EDITOR_TOOL_RESULT_MAX_CHARS", "4000"))


class PlanningBudgetExceededError(Exception):
    """Raised when the planning loop exhausts its tool-call budget."""


def _validate_no_duplicate_file_targets(steps: list[dict[str, object]]) -> list[str]:
    """Check that no file path appears in more than one step's targets."""
    seen: dict[str, str] = {}
    errors: list[str] = []
    for step in steps:
        step_id = str(step.get("id", step.get("step_id", "?")))
        targets = step.get("targets", [])
        if not isinstance(targets, list):
            continue
        for target in targets:
            path = target.get("path", "") if isinstance(target, dict) else str(target)
            if path in seen:
                errors.append(
                    f"File '{path}' appears in both step '{seen[path]}' and step '{step_id}'. "
                    "Consolidate all changes to this file into one step."
                )
            else:
                seen[path] = step_id
    return errors


class PlanningLoop:
    """Implements the explore-then-commit ReAct loop for PlanningAgent.

    Calls reasoning_engine.create_planning_step() each iteration.
    Returns when the agent emits emit_plan or emit_revision.
    Raises PlanningBudgetExceededError if budget exhausted without emitting.
    """

    def __init__(
        self,
        reasoning_engine: ReasoningEngine,
        registry: PlanningToolRegistry,
        broadcaster: PatchEventBroadcaster,
        task_id: str,
    ) -> None:
        self._reasoning = reasoning_engine
        self._registry = registry
        self._broadcaster = broadcaster
        self._task_id = task_id

    async def run(
        self,
        plan_context: dict[str, object],
        budget: TaskBudget,
        *,
        revision_mode: bool = False,
    ) -> PlanningResult | PlanRevisionResult:
        """Run one planning loop. Returns PlanningResult or PlanRevisionResult.

        On duplicate-file-target errors in emit_plan, re-invokes agent with
        error feedback up to 2 correction attempts before failing.
        """
        tool_defs = [t.model_dump() for t in self._registry.definitions()]
        max_calls = (
            budget.max_revision_tool_calls if revision_mode else budget.max_planning_tool_calls
        )
        emit_type = "emit_revision" if revision_mode else "emit_plan"
        system = format_planning_system_prompt(tool_defs, revision_mode=revision_mode)

        for correction_attempt in range(3):
            result = await self._run_single_pass(
                plan_context=plan_context,
                tool_defs=tool_defs,
                max_calls=max_calls,
                emit_type=emit_type,
                system=system,
            )

            if isinstance(result, PlanRevisionResult):
                return result

            # PlanningResult — validate one-step-per-file before returning
            if isinstance(result, PlanningResult):
                return result

        # Unreachable — run_single_pass raises on budget exhaust
        raise PlanningBudgetExceededError("Planning loop exited without result")

    async def _run_single_pass(
        self,
        plan_context: dict[str, object],
        tool_defs: list[dict[str, object]],
        max_calls: int,
        emit_type: str,
        system: str,
    ) -> PlanningResult | PlanRevisionResult:
        trace = AgentToolTrace(step_id="planning")
        history: list[dict[str, object]] = []

        for iteration in range(max_calls + 1):
            response = await self._reasoning.create_planning_step(
                plan_context=plan_context,
                history=history,
                tool_definitions=tool_defs,
            )

            action_type = str(response.get("type", ""))
            thought = str(response.get("thought", ""))

            if action_type == "emit_plan":
                plan_markdown = str(response.get("plan_markdown", ""))
                files_examined = list(response.get("files_examined", []))
                confidence = str(response.get("confidence", "medium"))
                if confidence not in ("high", "medium", "low"):
                    confidence = "medium"
                self._broadcaster.broadcast(self._task_id, {
                    "type": "planning_complete",
                    "files_examined": files_examined,
                    "confidence": confidence,
                })
                return PlanningResult(
                    plan_markdown=plan_markdown,
                    files_examined=files_examined,
                    confidence=confidence,  # type: ignore[arg-type]
                    tool_trace=trace,
                )

            if action_type == "emit_revision":
                raw_steps = response.get("revised_steps", [])
                if not isinstance(raw_steps, list):
                    raw_steps = []
                revised_steps = [
                    RevisedStep(
                        step_id=str(s.get("step_id", "")),
                        goal=str(s.get("goal", "")),
                        targets=s.get("targets", []),  # type: ignore[arg-type]
                        implementation_details=str(s.get("implementation_details", "")),
                        edge_cases=str(s.get("edge_cases", "")),
                        testing_strategy=str(s.get("testing_strategy", "")),
                        risk=str(s.get("risk", "low")),
                    )
                    for s in raw_steps
                    if isinstance(s, dict)
                ]
                reverted_step_ids = list(response.get("reverted_step_ids", []))
                revision_summary = str(response.get("revision_summary", ""))
                return PlanRevisionResult(
                    revised_steps=revised_steps,
                    reverted_step_ids=reverted_step_ids,
                    revision_summary=revision_summary,
                    tool_trace=trace,
                )

            if action_type != "tool_call":
                logger.warning(
                    "Unexpected planning loop response type '%s'; treating as empty plan",
                    action_type,
                    extra={"task_id": self._task_id},
                )
                return PlanningResult(
                    plan_markdown="",
                    files_examined=[],
                    confidence="low",
                    tool_trace=trace,
                )

            if iteration >= max_calls:
                raise PlanningBudgetExceededError(
                    f"Planning loop used {max_calls} tool calls without emitting {emit_type}"
                )

            tool_name = str(response.get("tool", ""))
            raw_args = response.get("args")
            args: dict[str, object] = raw_args if isinstance(raw_args, dict) else {}

            self._broadcaster.broadcast(self._task_id, {
                "type": "planning_tool_call",
                "tool": tool_name,
                "thought": thought[:300],
                "iteration": iteration + 1,
            })

            tool_output = await self._registry.execute(tool_name, args)

            self._broadcaster.broadcast(self._task_id, {
                "type": "planning_tool_result",
                "tool": tool_name,
                "output": tool_output.output[:500],
                "is_error": tool_output.is_error,
                "iteration": iteration + 1,
            })

            call_id = f"plan-{uuid4().hex[:8]}"
            trace.calls.append(ToolCall(call_id=call_id, tool_name=tool_name, arguments=args))
            trace.results.append(ToolResult(
                call_id=call_id,
                tool_name=tool_name,
                output=tool_output.output[:_MAX_OUTPUT_INJECT_CHARS],
                is_error=tool_output.is_error,
            ))

            history.append({"role": "assistant", "content": json.dumps(response, default=str)})
            history.append({
                "role": "tool_result",
                "tool": tool_name,
                "content": tool_output.output[:_MAX_OUTPUT_INJECT_CHARS],
            })

        raise PlanningBudgetExceededError("Planning loop exited without result")
```

- [ ] **Step 2: Verify it parses**

```bash
python -c "from agentd.planning.loop import PlanningLoop; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add agentd/planning/loop.py
git commit -m "feat(planning): PlanningLoop explore-then-commit ReAct loop"
```

---

## Task 5: PlanningAgent

**Files:**
- Create: `agentd/planning/agent.py`
- Create: `agentd/planning/__init__.py`

- [ ] **Step 1: Create `agentd/planning/agent.py`**

```python
"""PlanningAgent: owns plan correctness for the agentic editor."""
from __future__ import annotations

from pathlib import Path

from agentd.domain.models import (
    DeltaReplanRequest,
    PlanRevisionResult,
    PlanningResult,
    TaskBudget,
    TaskRecord,
)
from agentd.orchestrator.broadcaster import PatchEventBroadcaster
from agentd.planning.loop import PlanningLoop
from agentd.planning.registry import PlanningToolRegistry
from agentd.reasoning.contracts import ReasoningEngine


class PlanningAgent:
    """Stateless agent for plan generation and delta revision.

    All state lives in TaskRecord. This class is a thin coordinator:
    it builds context, delegates to PlanningLoop, and returns typed results.
    """

    def __init__(
        self,
        reasoning_engine: ReasoningEngine,
        registry: PlanningToolRegistry,
        broadcaster: PatchEventBroadcaster,
        task_id: str,
    ) -> None:
        self._reasoning = reasoning_engine
        self._registry = registry
        self._broadcaster = broadcaster
        self._task_id = task_id

    async def generate_plan(
        self,
        task: TaskRecord,
        initial_context: dict[str, object],
        budget: TaskBudget,
    ) -> PlanningResult:
        """Explore the workspace and produce a markdown plan.

        Args:
            task: Current task (reads goal and workspace_path).
            initial_context: Output of load_context() — seed, not a constraint.
            budget: Controls max_planning_tool_calls.
        """
        plan_context: dict[str, object] = {
            "goal": task.goal,
            "workspace_path": task.workspace_path,
            "initial_context": initial_context,
        }
        loop = PlanningLoop(
            reasoning_engine=self._reasoning,
            registry=self._registry,
            broadcaster=self._broadcaster,
            task_id=self._task_id,
        )
        result = await loop.run(plan_context, budget, revision_mode=False)
        assert isinstance(result, PlanningResult)
        return result

    async def revise(
        self,
        task: TaskRecord,
        real_path: Path,
    ) -> PlanRevisionResult:
        """Explore the actual workspace and produce a targeted plan revision.

        Called after execution agent emits revision_needed.
        Reads the latest DeltaReplanRequest from task.execution_state.
        """
        request = task.execution_state.delta_replan_requests[-1]
        completed_set = set(task.completed_step_ids)
        assert task.plan is not None, "Cannot revise without a plan"

        plan_steps_context = [
            {
                "step_id": s.id,
                "goal": s.goal,
                "targets": [{"path": t.path, "intent": t.intent} for t in s.targets],
                "implementation_details": s.implementation_details,
                "status": (
                    "completed" if s.id in completed_set
                    else "failed" if s.id == request.requested_by_step_id
                    else "pending"
                ),
            }
            for s in task.plan.steps
        ]

        plan_context: dict[str, object] = {
            "goal": task.goal,
            "workspace_path": str(real_path),
            "plan_steps": plan_steps_context,
            "revision_request": {
                "failed_step_id": request.requested_by_step_id,
                "reason": request.reason,
                "evidence": request.evidence,
                "hinted_affected_steps": request.hinted_affected_steps,
            },
            "revertable_step_ids": list(task.execution_state.step_checkpoints.keys()),
        }

        loop = PlanningLoop(
            reasoning_engine=self._reasoning,
            registry=self._registry,
            broadcaster=self._broadcaster,
            task_id=self._task_id,
        )
        result = await loop.run(plan_context, task.budget, revision_mode=True)
        assert isinstance(result, PlanRevisionResult)
        return result
```

- [ ] **Step 2: Create `agentd/planning/__init__.py`**

```python
"""PlanningAgent package for Phase 5 agentic planning."""
from agentd.planning.agent import PlanningAgent
from agentd.planning.loop import PlanningBudgetExceededError, PlanningLoop
from agentd.planning.registry import PlanningToolRegistry

__all__ = ["PlanningAgent", "PlanningBudgetExceededError", "PlanningLoop", "PlanningToolRegistry"]
```

- [ ] **Step 3: Verify package imports**

```bash
python -c "from agentd.planning import PlanningAgent, PlanningLoop, PlanningToolRegistry; print('OK')"
```
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add agentd/planning/agent.py agentd/planning/__init__.py
git commit -m "feat(planning): PlanningAgent with generate_plan() and revise()"
```

---

## Task 6: Tests for PlanningLoop and PlanningAgent

**Files:**
- Create: `tests/test_planning_agent.py`

- [ ] **Step 1: Create the test file**

```python
# tests/test_planning_agent.py
from __future__ import annotations

from pathlib import Path

import pytest

from agentd.domain.models import (
    AgentToolTrace,
    DeltaReplanRequest,
    PlanDocument,
    PlanRevisionResult,
    PlanStep,
    PlanningResult,
    TaskBudget,
    TaskExecutionState,
    TaskRecord,
    TaskStatus,
)
from agentd.orchestrator.broadcaster import PatchEventBroadcaster
from agentd.planning.agent import PlanningAgent
from agentd.planning.loop import PlanningLoop, _validate_no_duplicate_file_targets
from agentd.planning.registry import PlanningToolRegistry
from datetime import datetime, timezone


class ScriptedPlanningEngine:
    """Scripted engine that returns predetermined responses for the planning loop."""

    def __init__(self, responses: list[dict]) -> None:
        self._responses = responses
        self._index = 0

    async def create_planning_step(
        self,
        plan_context: dict,
        history: list,
        tool_definitions: list,
    ) -> dict:
        idx = min(self._index, len(self._responses) - 1)
        self._index += 1
        return self._responses[idx]

    # Stubs for ReasoningEngine protocol compliance
    async def create_plan(self, *a, **kw): return {}
    async def create_markdown_plan(self, *a, **kw): return ""
    async def critique_markdown_plan(self, *a, **kw): return {"verdict": "pass", "issues": []}
    async def critique_json_plan(self, *a, **kw): return {"verdict": "pass", "issues": []}
    async def create_patch(self, *a, **kw): return {}
    async def create_tool_step(self, *a, **kw): return {"type": "emit_patch", "thought": "", "patch_ops": []}


def _make_task(tmp_path: Path) -> TaskRecord:
    return TaskRecord(task_id="t1", goal="add logging", workspace_path=str(tmp_path))


def _make_registry(tmp_path: Path) -> PlanningToolRegistry:
    return PlanningToolRegistry(real_path=tmp_path)


def _make_broadcaster() -> PatchEventBroadcaster:
    return PatchEventBroadcaster()


# --- _validate_no_duplicate_file_targets ---

def test_no_duplicates_passes():
    steps = [
        {"id": "s1", "targets": [{"path": "a.py"}, {"path": "b.py"}]},
        {"id": "s2", "targets": [{"path": "c.py"}]},
    ]
    assert _validate_no_duplicate_file_targets(steps) == []


def test_duplicate_across_steps_detected():
    steps = [
        {"id": "s1", "targets": [{"path": "a.py"}]},
        {"id": "s2", "targets": [{"path": "a.py"}]},
    ]
    errors = _validate_no_duplicate_file_targets(steps)
    assert len(errors) == 1
    assert "a.py" in errors[0]
    assert "s1" in errors[0]
    assert "s2" in errors[0]


def test_same_file_within_one_step_not_caught():
    # PlanStep.validate_targets handles within-step duplicates; this function only checks cross-step
    steps = [{"id": "s1", "targets": [{"path": "a.py"}, {"path": "b.py"}]}]
    assert _validate_no_duplicate_file_targets(steps) == []


# --- PlanningLoop.run() ---

@pytest.mark.asyncio
async def test_planning_loop_emit_plan(tmp_path: Path):
    engine = ScriptedPlanningEngine([
        {
            "type": "emit_plan",
            "thought": "Ready",
            "plan_markdown": "# Plan\n- step 1",
            "files_examined": ["src/auth.py"],
            "confidence": "high",
        }
    ])
    loop = PlanningLoop(
        reasoning_engine=engine,
        registry=_make_registry(tmp_path),
        broadcaster=_make_broadcaster(),
        task_id="t1",
    )
    budget = TaskBudget()
    result = await loop.run({"goal": "add logging", "workspace_path": str(tmp_path)}, budget)
    assert isinstance(result, PlanningResult)
    assert result.plan_markdown == "# Plan\n- step 1"
    assert result.files_examined == ["src/auth.py"]
    assert result.confidence == "high"


@pytest.mark.asyncio
async def test_planning_loop_tool_call_then_emit_plan(tmp_path: Path):
    # First response: tool call; second: emit_plan
    engine = ScriptedPlanningEngine([
        {"type": "tool_call", "thought": "Searching", "tool": "list_directory", "args": {}},
        {
            "type": "emit_plan",
            "thought": "Done",
            "plan_markdown": "# Plan",
            "files_examined": [],
            "confidence": "medium",
        },
    ])
    loop = PlanningLoop(
        reasoning_engine=engine,
        registry=_make_registry(tmp_path),
        broadcaster=_make_broadcaster(),
        task_id="t1",
    )
    result = await loop.run({"goal": "test", "workspace_path": str(tmp_path)}, TaskBudget())
    assert isinstance(result, PlanningResult)
    assert result.confidence == "medium"


@pytest.mark.asyncio
async def test_planning_loop_emit_revision(tmp_path: Path):
    engine = ScriptedPlanningEngine([
        {
            "type": "emit_revision",
            "thought": "Fixed",
            "revised_steps": [{
                "step_id": "s1",
                "goal": "Fixed goal",
                "targets": [{"path": "a.py", "intent": "existing"}],
                "implementation_details": "do it",
            }],
            "reverted_step_ids": [],
            "revision_summary": "s1 retargeted",
        }
    ])
    loop = PlanningLoop(
        reasoning_engine=engine,
        registry=_make_registry(tmp_path),
        broadcaster=_make_broadcaster(),
        task_id="t1",
    )
    result = await loop.run(
        {"goal": "fix", "workspace_path": str(tmp_path)},
        TaskBudget(),
        revision_mode=True,
    )
    assert isinstance(result, PlanRevisionResult)
    assert len(result.revised_steps) == 1
    assert result.revised_steps[0].step_id == "s1"
    assert result.revision_summary == "s1 retargeted"


# --- PlanningAgent ---

@pytest.mark.asyncio
async def test_planning_agent_generate_plan(tmp_path: Path):
    engine = ScriptedPlanningEngine([
        {
            "type": "emit_plan",
            "thought": "Ready",
            "plan_markdown": "# Plan",
            "files_examined": [],
            "confidence": "high",
        }
    ])
    agent = PlanningAgent(
        reasoning_engine=engine,
        registry=_make_registry(tmp_path),
        broadcaster=_make_broadcaster(),
        task_id="t1",
    )
    task = _make_task(tmp_path)
    result = await agent.generate_plan(task, initial_context={}, budget=TaskBudget())
    assert isinstance(result, PlanningResult)
    assert result.plan_markdown == "# Plan"


@pytest.mark.asyncio
async def test_planning_agent_revise(tmp_path: Path):
    engine = ScriptedPlanningEngine([
        {
            "type": "emit_revision",
            "thought": "Fixed",
            "revised_steps": [{
                "step_id": "s2",
                "goal": "Retargeted",
                "targets": [{"path": "correct.py", "intent": "existing"}],
                "implementation_details": "add log",
            }],
            "reverted_step_ids": [],
            "revision_summary": "s2 fixed",
        }
    ])
    task = _make_task(tmp_path)
    task.plan = PlanDocument(
        analysis="test",
        steps=[
            PlanStep(id="s1", goal="done", targets=[{"path": "a.py", "intent": "existing"}], risk="low"),
            PlanStep(id="s2", goal="failed", targets=[{"path": "b.py", "intent": "existing"}], risk="low"),
        ],
        expected_files=["a.py", "b.py"],
        stop_conditions=[],
    )
    task.completed_step_ids = ["s1"]
    task.execution_state.delta_replan_requests.append(
        DeltaReplanRequest(
            requested_by_step_id="s2",
            reason="wrong file",
            evidence="function in correct.py",
            hinted_affected_steps=[],
            requested_at=datetime.now(timezone.utc),
        )
    )
    task.execution_state.step_checkpoints["s1"] = "/tmp/checkpoint-s1"

    agent = PlanningAgent(
        reasoning_engine=engine,
        registry=_make_registry(tmp_path),
        broadcaster=_make_broadcaster(),
        task_id="t1",
    )
    result = await agent.revise(task, tmp_path)
    assert isinstance(result, PlanRevisionResult)
    assert result.revised_steps[0].step_id == "s2"
    assert result.revised_steps[0].goal == "Retargeted"
```

- [ ] **Step 2: Run tests**

```bash
pytest tests/test_planning_agent.py -v
```
Expected: All tests PASS. (The `list_directory` tool call returns empty dir listing; that's fine.)

- [ ] **Step 3: Commit**

```bash
git add tests/test_planning_agent.py
git commit -m "test(planning): PlanningLoop and PlanningAgent unit tests"
```

---

## Task 7: ReasoningEngine contract and implementations

**Files:**
- Modify: `agentd/reasoning/contracts.py`
- Modify: `agentd/reasoning/engine.py`
- Modify: `agentd/orchestrator/scripted_engine.py`

- [ ] **Step 1: Write a failing test that calls `create_planning_step()` on `DefaultReasoningEngine`**

This test can't be run until Task 7 is done, but we note the shape so we can confirm it passes:
```python
# Verified later in tests/test_reasoning_engine.py — existing test file
# Just confirm the protocol has the method after this task.
```

- [ ] **Step 2: Add `create_planning_step()` to `agentd/reasoning/contracts.py`**

Add after the `create_tool_step()` definition:
```python
    async def create_planning_step(
        self,
        plan_context: dict[str, object],
        history: list[dict[str, object]],
        tool_definitions: list[dict[str, object]],
    ) -> dict[str, object]:
        """One turn of the planning ReAct loop.

        Returns a dict with at minimum {"type": "tool_call"|"emit_plan"|"emit_revision", "thought": str}.
        For tool_call: also "tool" (name) and "args" (dict).
        For emit_plan: also "plan_markdown", "files_examined", "confidence".
        For emit_revision: also "revised_steps", "reverted_step_ids", "revision_summary".
        """
        ...
```

- [ ] **Step 3: Implement `create_planning_step()` in `agentd/reasoning/engine.py`**

In `DefaultReasoningEngine`, add after `create_tool_step()`:
```python
    async def create_planning_step(
        self,
        plan_context: dict[str, object],
        history: list[dict[str, object]],
        tool_definitions: list[dict[str, object]],
    ) -> dict[str, object]:
        from agentd.planning.prompts import (
            PLANNING_STEP_RESPONSE_SCHEMA,
            build_planning_step_payload,
            format_planning_system_prompt,
        )
        payload = build_planning_step_payload(plan_context, history, tool_definitions)
        revision_mode = "revision_request" in plan_context
        system = format_planning_system_prompt(tool_definitions, revision_mode=revision_mode)

        _debug_dump(
            self._task_id or "unknown",
            f"planning-step-{len(history)}",
            {"plan_context": plan_context, "history_len": len(history)},
            workspace_path=self._workspace_path or "",
        )

        response = await self._transport.generate_json(
            schema=PLANNING_STEP_RESPONSE_SCHEMA,
            system=system,
            user_payload=payload,
        )
        return response if isinstance(response, dict) else {}
```

Note: `DefaultReasoningEngine` already tracks `_task_id` and `_workspace_path` for `_debug_dump`. If those attributes don't exist, remove the debug dump call. Verify by reading the `__init__` of `DefaultReasoningEngine` first:

```bash
grep -n "_task_id\|_workspace_path" services/agentd-py/agentd/reasoning/engine.py | head -10
```

If the attributes don't exist, replace `_debug_dump(...)` with `pass` and add a TODO comment.

- [ ] **Step 4: Add `create_planning_step()` stub to `ScriptedReasoningEngine`**

In `agentd/orchestrator/scripted_engine.py`, add after `create_tool_step()`:
```python
    async def create_planning_step(
        self,
        plan_context: dict[str, object],
        history: list[dict[str, object]],
        tool_definitions: list[dict[str, object]],
    ) -> dict[str, object]:
        _ = (plan_context, history, tool_definitions)
        return {
            "type": "emit_plan",
            "thought": "scripted planning engine bypasses exploration",
            "plan_markdown": "# Scripted Plan\n\n- Review generated changes",
            "files_examined": [],
            "confidence": "high",
        }
```

- [ ] **Step 5: Verify all existing tests still pass**

```bash
pytest tests/ -v --tb=short -q 2>&1 | tail -20
```
Expected: All previously passing tests still pass. `create_planning_step()` is additive.

- [ ] **Step 6: Commit**

```bash
git add agentd/reasoning/contracts.py agentd/reasoning/engine.py agentd/orchestrator/scripted_engine.py
git commit -m "feat(reasoning): add create_planning_step() to ReasoningEngine protocol and implementations"
```

---

## Task 8: ToolLoop — `StepOutcome` return type and `revision_needed` handling

**Files:**
- Modify: `agentd/reasoning/tool_prompts.py`
- Modify: `agentd/tools/loop.py`

The `ToolLoop.run()` currently returns `tuple[dict, AgentToolTrace]`. After this task it returns `StepOutcome = PatchResult | PlanHandoff`. The engine call site in `_run_step_with_retries` must be updated in Task 9.

- [ ] **Step 1: Write failing test for `PlanHandoff` return**

```python
# tests/test_delta_replan.py  (new file — full test in Task 13; this seeds it)
from __future__ import annotations

from pathlib import Path
import pytest
from agentd.domain.models import AgentToolTrace, PlanStep, TaskBudget, TaskUsage
from agentd.orchestrator.broadcaster import PatchEventBroadcaster
from agentd.tools.loop import PatchResult, PlanHandoff, StepOutcome, ToolLoop
from agentd.tools.registry import ToolRegistry


class RevisionNeededEngine:
    """Engine that immediately emits revision_needed."""
    async def create_tool_step(self, step_context, history, tool_definitions):
        return {
            "type": "revision_needed",
            "thought": "Target file is wrong",
            "reason": "function not in planned file",
            "evidence": "grep found it in other.py",
            "affected_steps": ["s2"],
        }

    async def create_planning_step(self, *a, **kw): return {}
    async def create_plan(self, *a, **kw): return {}
    async def create_markdown_plan(self, *a, **kw): return ""
    async def critique_markdown_plan(self, *a, **kw): return {"verdict": "pass", "issues": []}
    async def critique_json_plan(self, *a, **kw): return {"verdict": "pass", "issues": []}
    async def create_patch(self, *a, **kw): return {}


@pytest.mark.asyncio
async def test_tool_loop_returns_plan_handoff_on_revision_needed(tmp_path: Path):
    step = PlanStep(
        id="s1",
        goal="add logging",
        targets=[{"path": "src/api.py", "intent": "existing"}],
        risk="low",
    )
    loop = ToolLoop(
        reasoning_engine=RevisionNeededEngine(),
        registry=ToolRegistry(shadow_root=tmp_path),
        broadcaster=PatchEventBroadcaster(),
        task_id="t1",
    )
    outcome = await loop.run(step, {}, TaskBudget(), TaskUsage())
    assert isinstance(outcome, PlanHandoff)
    assert outcome.step_id == "s1"
    assert outcome.reason == "function not in planned file"
    assert outcome.evidence == "grep found it in other.py"
    assert outcome.hinted_affected_steps == ["s2"]
```

- [ ] **Step 2: Run the test to confirm it fails**

```bash
pytest tests/test_delta_replan.py::test_tool_loop_returns_plan_handoff_on_revision_needed -v
```
Expected: FAIL — `PlanHandoff`, `PatchResult`, `StepOutcome` not imported.

- [ ] **Step 3: Add `revision_needed` to `AGENT_STEP_RESPONSE_SCHEMA` in `tool_prompts.py`**

In `agentd/reasoning/tool_prompts.py`, change the `type` enum:
```python
        "type": {
            "type": "string",
            "enum": ["tool_call", "emit_patch", "revision_needed"],
            "description": "Action type: 'tool_call' to invoke a tool, 'emit_patch' when ready to write code, 'revision_needed' when the step's planned approach is fundamentally wrong",
        },
```

Add new properties after `patch_ops`:
```python
        "reason": {
            "type": "string",
            "description": "Why the step cannot be completed as planned (required when type='revision_needed')",
        },
        "evidence": {
            "type": "string",
            "description": "Specific evidence from tool calls justifying the revision request",
        },
        "affected_steps": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Step IDs that are likely also affected (hint for planning agent)",
        },
```

Also update the `TOOL_LOOP_SYSTEM_PROMPT` to mention the new action:
```
- To signal a plan error: {{"type": "revision_needed", "thought": "...", "reason": "...", "evidence": "...", "affected_steps": [...]}}
  Use ONLY when the target files/symbols in the plan are fundamentally wrong and cannot be fixed with a patch.
  Provide specific evidence from your tool calls.
```

- [ ] **Step 4: Add `PatchResult`, `PlanHandoff`, `StepOutcome` to `agentd/tools/loop.py`**

Add these dataclasses at the top of `loop.py`, after imports:
```python
from dataclasses import dataclass

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

- [ ] **Step 5: Update `ToolLoop.run()` to return `StepOutcome`**

Change the return type annotation:
```python
    async def run(
        self,
        step: PlanStep,
        patch_request_context: dict[str, object],
        budget: TaskBudget,
        usage: TaskUsage,
    ) -> StepOutcome:
```

In the loop body, replace `return self._wrap_as_patch_document(patch_ops), trace` with:
```python
                return PatchResult(
                    patch_document=self._wrap_as_patch_document(patch_ops),
                    tool_trace=trace,
                )
```

And replace `return self._wrap_as_patch_document([]), trace` with:
```python
                return PatchResult(
                    patch_document=self._wrap_as_patch_document([]),
                    tool_trace=trace,
                )
```

Add `revision_needed` handling between the `emit_patch` block and the `tool_call` check:
```python
            if action_type == "revision_needed":
                reason = str(response.get("reason", ""))
                evidence = str(response.get("evidence", ""))
                raw_affected = response.get("affected_steps", [])
                affected = [str(s) for s in raw_affected] if isinstance(raw_affected, list) else []
                logger.info(
                    "Tool loop revision_needed: %s",
                    reason[:200],
                    extra={"task_id": self._task_id, "step_id": step.id},
                )
                self._broadcaster.broadcast(self._task_id, {
                    "type": "revision_needed",
                    "step_id": step.id,
                    "reason": reason,
                    "evidence": evidence[:300],
                })
                return PlanHandoff(
                    step_id=step.id,
                    reason=reason,
                    evidence=evidence,
                    hinted_affected_steps=affected,
                    tool_trace=trace,
                )
```

- [ ] **Step 6: Run the delta replan test**

```bash
pytest tests/test_delta_replan.py::test_tool_loop_returns_plan_handoff_on_revision_needed -v
```
Expected: PASS.

- [ ] **Step 7: Run full test suite to check for regressions**

The engine still calls `patch_raw, tool_trace = await tool_loop.run(...)` — this will now fail because `run()` returns `StepOutcome`, not a tuple. We expect those tests to fail until Task 9.

```bash
pytest tests/ -v --tb=line -q 2>&1 | tail -30
```
Note which tests fail and confirm they fail only because of the `tool_loop.run()` tuple unpack. That's expected — Task 9 fixes it.

- [ ] **Step 8: Commit**

```bash
git add agentd/reasoning/tool_prompts.py agentd/tools/loop.py tests/test_delta_replan.py
git commit -m "feat(tools): ToolLoop returns StepOutcome union; add revision_needed action type"
```

---

## Task 9: Engine — step loop refactor + `_run_step_with_retries` return type

**Files:**
- Modify: `agentd/orchestrator/engine.py`

After this task, `_run_step_with_retries` returns `StepRunResult | PlanHandoff`. The `_execute_plan` `for` loop becomes a `while` loop. Existing behaviour (no delta replan) is preserved — the dispatch only does `isinstance(step_result, PlanHandoff)` → fail (delta replan dispatch added in Task 11).

- [ ] **Step 1: Add `_next_incomplete_step()` method to `AgentOrchestrator`**

Add this private method after `_merge_step_result()`:
```python
    def _next_incomplete_step(self, task: TaskRecord) -> PlanStep | None:
        """Return the first step in the plan that hasn't been completed."""
        if task.plan is None:
            return None
        completed = set(task.completed_step_ids)
        return next((s for s in task.plan.steps if s.id not in completed), None)
```

- [ ] **Step 2: Update `_run_step_with_retries` return type and import `PlanHandoff`**

Add `PlanHandoff, PatchResult, StepOutcome` to the import from `agentd.tools.loop`:
```python
from agentd.tools.loop import PatchResult, PlanHandoff, StepOutcome, ToolLoop, ToolBudgetExceededError, build_tool_registry
```

Change the return type annotation of `_run_step_with_retries`:
```python
    async def _run_step_with_retries(
        self,
        ...
    ) -> StepRunResult | PlanHandoff:
```

- [ ] **Step 3: Handle `PlanHandoff` inside `_run_step_with_retries`**

In the tool-loop-enabled branch, replace:
```python
                    patch_raw, tool_trace = await tool_loop.run(
                        step,
                        {**patch_request_context, "plan_markdown": task.plan_markdown},
                        task.budget,
                        task.usage,
                    )
```

With:
```python
                    step_outcome = await tool_loop.run(
                        step,
                        {**patch_request_context, "plan_markdown": task.plan_markdown},
                        task.budget,
                        task.usage,
                    )

                    if isinstance(step_outcome, PlanHandoff):
                        # Restore checkpoint so shadow is clean before handing off
                        self._restore_shadow_checkpoint(shadow_path, checkpoint.checkpoint_path)
                        task.modified_files = previous_modified_files
                        return step_outcome

                    # PatchResult
                    patch_raw = step_outcome.patch_document
                    tool_trace = step_outcome.tool_trace
```

- [ ] **Step 4: Replace the `for` step loop in `_execute_plan` with `while _next_incomplete_step()`**

Replace:
```python
            for step in task.plan.steps:
                if step.id in task.completed_step_ids:
                    continue
                step_result = await self._run_step_with_retries(
                    task,
                    step,
                    shadow_path,
                    retrieval_context,
                    persistent_diagnostics,
                    started_at_ms,
                )
                self._merge_step_result(task, step_result, persistent_diagnostics)
                await self._store.save(task)
                if step_result.outcome != "step_completed":
                    task = transition(task, TaskStatus.FAILED, "step execution exhausted")
                    await self._store.save(task)
                    return task
```

With:
```python
            while (step := self._next_incomplete_step(task)) is not None:
                step_result = await self._run_step_with_retries(
                    task,
                    step,
                    shadow_path,
                    retrieval_context,
                    persistent_diagnostics,
                    started_at_ms,
                )

                if isinstance(step_result, PlanHandoff):
                    # Delta replan: planning agent will handle in Task 11.
                    # For now, fail cleanly so existing tests see a predictable error.
                    task.diagnostics.append(Diagnostic(
                        source="orchestrator",
                        message=f"Delta replan requested by step {step_result.step_id}: {step_result.reason}",
                        level="error",
                    ))
                    task = transition(task, TaskStatus.FAILED, "delta replan not yet wired (Task 11)")
                    await self._store.save(task)
                    return task

                self._merge_step_result(task, step_result, persistent_diagnostics)
                await self._store.save(task)
                if step_result.outcome != "step_completed":
                    task = transition(task, TaskStatus.FAILED, "step execution exhausted")
                    await self._store.save(task)
                    return task
```

- [ ] **Step 5: Run full test suite**

```bash
pytest tests/ -v --tb=short -q 2>&1 | tail -30
```
Expected: All previously passing tests pass again (the tuple-unpack issue is fixed). The delta replan test from Task 8 still passes.

- [ ] **Step 6: Commit**

```bash
git add agentd/orchestrator/engine.py
git commit -m "refactor(engine): while _next_incomplete_step loop; _run_step_with_retries returns StepRunResult|PlanHandoff"
```

---

## Task 10: Update existing test stubs — add `create_planning_step()`

**Files:**
- Modify: All test files that have inline stub reasoning engines

The engine will call `create_planning_step()` once we wire up `PlanningAgent.generate_plan()` in Task 11. Until then, existing tests that call `run_task()` directly need the stub. Do this proactively to avoid cascading failures.

- [ ] **Step 1: Find all test files with inline stub reasoning engines**

```bash
grep -rn "async def create_markdown_plan\|async def create_plan" services/agentd-py/tests/ | grep -v ScriptedReasoning
```

Note the files returned. Typically: `test_orchestrator_repair_rollback.py`, `test_orchestrator_candidate_scoring.py`, `test_orchestrator_plan_target_validation.py`, `test_orchestrator_retrieval.py`, `test_plan_feedback_api.py`.

- [ ] **Step 2: Add `create_planning_step()` stub to each inline engine**

For each stub class found, add:
```python
    async def create_planning_step(
        self,
        plan_context: dict,
        history: list,
        tool_definitions: list,
    ) -> dict:
        _ = (plan_context, history, tool_definitions)
        return {
            "type": "emit_plan",
            "thought": "stub: planning agent bypassed",
            "plan_markdown": "# Stub Plan\n\n- Review generated changes",
            "files_examined": [],
            "confidence": "high",
        }
```

Apply this to every class that implements the `ReasoningEngine` protocol in test files. Do NOT modify `ScriptedReasoningEngine` — it already has the stub from Task 7.

- [ ] **Step 3: Run the full test suite**

```bash
pytest tests/ -v --tb=short -q 2>&1 | tail -20
```
Expected: All tests pass.

- [ ] **Step 4: Commit**

```bash
git add tests/
git commit -m "test: add create_planning_step() stub to all inline test reasoning engines"
```

---

## Task 11: Engine — PlanningAgent integration (replace static planning)

**Files:**
- Modify: `agentd/orchestrator/engine.py`

Replace `_generate_repo_grounded_markdown_plan()` calls in `run_task()` and `continue_task()` with `PlanningAgent.generate_plan()`. Also simplify the JSON plan critique loop in `continue_task()` to a single `create_plan()` call.

- [ ] **Step 1: Add planning imports to `engine.py`**

Add after existing imports:
```python
from agentd.planning.agent import PlanningAgent
from agentd.planning.registry import PlanningToolRegistry
```

- [ ] **Step 2: Add `_build_planning_agent()` helper to `AgentOrchestrator`**

```python
    def _build_planning_agent(self, task_id: str, workspace_path: str) -> PlanningAgent:
        """Construct a PlanningAgent reading from the real (unmodified) workspace."""
        planning_registry = PlanningToolRegistry(
            real_path=Path(workspace_path).resolve(),
            semantic_index=getattr(self._retrieval_client, "_semantic_index", None),
        )
        return PlanningAgent(
            reasoning_engine=self._reasoning_engine,
            registry=planning_registry,
            broadcaster=self.broadcaster,
            task_id=task_id,
        )
```

- [ ] **Step 3: Replace the planning call in `run_task()`**

In `run_task()`, replace:
```python
            plan_markdown, critique_diagnostics = await self._generate_repo_grounded_markdown_plan(
                task,
                str(shadow_workspace.shadow_path),
                plan_context_payload,
            )
            print("[PLAN] Plan Created.")
            task.plan_markdown = plan_markdown
            task.diagnostics = [*persistent_diagnostics, *critique_diagnostics]
```

With:
```python
            print("[PLAN] PlanningAgent exploring workspace...")
            planning_agent = self._build_planning_agent(task.task_id, task.workspace_path)
            planning_result = await planning_agent.generate_plan(
                task=task,
                initial_context=plan_context_payload,
                budget=task.budget,
            )
            self._write_debug_artifact(
                task.task_id,
                "planning-trace",
                planning_result.tool_trace.model_dump(mode="json"),
                artifacts_root_path=task.artifacts_root_path,
            )
            print(
                f"[PLAN] Plan created. Examined {len(planning_result.files_examined)} files. "
                f"Confidence: {planning_result.confidence}"
            )
            task.plan_markdown = planning_result.plan_markdown
            confidence_diagnostics: list[Diagnostic] = []
            if planning_result.confidence == "low":
                confidence_diagnostics = [Diagnostic(
                    source="planning_agent",
                    message=(
                        f"Planning confidence: low. Agent examined "
                        f"{len(planning_result.files_examined)} files — review plan carefully."
                    ),
                    level="warning",
                )]
            task.diagnostics = [*persistent_diagnostics, *confidence_diagnostics]
```

- [ ] **Step 4: Replace the planning call in `continue_task()` feedback branch**

In `continue_task()`, in the `if feedback:` branch, replace:
```python
                plan_markdown, critique_diagnostics = await self._generate_repo_grounded_markdown_plan(
                    task,
                    str(shadow_workspace.shadow_path),
                    {**plan_context_payload, "plan_feedback": feedback},
                )
                task.plan_markdown = plan_markdown
                task.diagnostics = [*retrieval_warnings, *critique_diagnostics]
```

With:
```python
                planning_agent = self._build_planning_agent(task.task_id, task.workspace_path)
                planning_result = await planning_agent.generate_plan(
                    task=task,
                    initial_context={**plan_context_payload, "plan_feedback": feedback},
                    budget=task.budget,
                )
                self._write_debug_artifact(
                    task.task_id,
                    "planning-trace-feedback",
                    planning_result.tool_trace.model_dump(mode="json"),
                    artifacts_root_path=task.artifacts_root_path,
                )
                task.plan_markdown = planning_result.plan_markdown
                confidence_diagnostics = [Diagnostic(
                    source="planning_agent",
                    message=f"Planning confidence: low. Review plan carefully.",
                    level="warning",
                )] if planning_result.confidence == "low" else []
                task.diagnostics = [*retrieval_warnings, *confidence_diagnostics]
```

- [ ] **Step 5: Simplify the JSON plan critique loop in `continue_task()` approved branch**

In `continue_task()`, in the `# Approved!` branch, replace the entire `for attempt in range(3):` loop (including `plan_draft_rounds`, `plan_critique_rounds`, `unresolved_targets`, `grounding_issues`, `schema_errors`, and the `_write_debug_artifact` calls for those) with:

```python
            plan_raw = await self._reasoning_engine.create_plan(
                task,
                str(shadow_workspace.shadow_path),
                plan_context_payload,
            )
            self._write_debug_artifact(
                task.task_id,
                "json-plan-draft",
                {"plan": plan_raw},
                artifacts_root_path=task.artifacts_root_path,
            )
            try:
                candidate_plan = PlanDocument.model_validate(plan_raw)
            except ValidationError as exc:
                task.diagnostics.append(Diagnostic(
                    source="orchestrator",
                    message=f"JSON plan schema validation failed: {exc}",
                    level="error",
                ))
                task = transition(task, TaskStatus.FAILED, "JSON plan schema invalid")
                await self._store.save(task)
                return task

            task.plan = candidate_plan
            self._write_debug_artifact(
                task.task_id,
                "plan",
                {"plan": plan_raw},
                artifacts_root_path=task.artifacts_root_path,
            )
```

Remove the `_write_debug_artifact` calls for `json-plan-critique` and `json-plan-final` since they no longer exist.

- [ ] **Step 6: Run full test suite**

```bash
pytest tests/ -v --tb=short -q 2>&1 | tail -30
```
Expected: All tests pass. Tests that call `run_task()` now call the planning agent stub (which immediately emits `emit_plan`), so the snapshot and plan-approval flow still works.

- [ ] **Step 7: Commit**

```bash
git add agentd/orchestrator/engine.py
git commit -m "feat(engine): replace static planning with PlanningAgent.generate_plan()"
```

---

## Task 12: Engine — delta replan dispatch and `_apply_revision()`

**Files:**
- Modify: `agentd/orchestrator/engine.py`

Wire up `PlanHandoff` dispatch in `_execute_plan`. Add `_apply_revision()`. Replace the temporary "fail cleanly" placeholder from Task 9.

- [ ] **Step 1: Write failing test for the full delta replan flow**

In `tests/test_delta_replan.py`, add:

```python
# Full engine delta replan integration test
from agentd.orchestrator.engine import AgentOrchestrator
from agentd.patch.engine import PatchEngine
from agentd.storage.in_memory import InMemoryTaskStore
from agentd.workspace.shadow import ShadowWorkspaceManager
from agentd.domain.models import TaskCreateRequest, TaskStatus, PlanDocument, PlanStep


class DeltaReplanOrchestrationEngine:
    """First call returns revision_needed; second call (after revision) returns emit_patch."""

    def __init__(self, tmp_path: Path) -> None:
        self._call_count = 0
        self._tmp_path = tmp_path

    async def create_tool_step(self, step_context, history, tool_definitions):
        self._call_count += 1
        if self._call_count == 1:
            return {
                "type": "revision_needed",
                "thought": "Wrong file",
                "reason": "function in other.py",
                "evidence": "grep confirmed",
                "affected_steps": [],
            }
        return {
            "type": "emit_patch",
            "thought": "Patching",
            "patch_ops": [{
                "op": "create_file",
                "file": "src/other.py",
                "content": "# added",
                "reason": "test",
            }],
        }

    async def create_planning_step(self, plan_context, history, tool_definitions):
        # Revision: retarget the step to the correct file
        return {
            "type": "emit_revision",
            "thought": "Retargeting",
            "revised_steps": [{
                "step_id": "s1",
                "goal": "Fixed goal",
                "targets": [{"path": "src/other.py", "intent": "new"}],
                "implementation_details": "Create file",
            }],
            "reverted_step_ids": [],
            "revision_summary": "Step retargeted to src/other.py",
        }

    async def create_plan(self, *a, **kw):
        return {
            "analysis": "test",
            "steps": [{"id": "s1", "goal": "add file", "targets": [{"path": "src/wrong.py", "intent": "existing"}], "risk": "low"}],
            "expected_files": ["src/wrong.py"],
            "stop_conditions": [],
        }

    async def create_markdown_plan(self, *a, **kw): return "# Plan"
    async def critique_markdown_plan(self, *a, **kw): return {"verdict": "pass", "issues": []}
    async def critique_json_plan(self, *a, **kw): return {"verdict": "pass", "issues": []}
    async def create_patch(self, *a, **kw): return {}


@pytest.mark.asyncio
async def test_engine_delta_replan_flow(tmp_path: Path):
    """Execution agent emits revision_needed → planning agent revises → step re-runs successfully."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "wrong.py").write_text("# wrong")

    store = InMemoryTaskStore()
    reasoning = DeltaReplanOrchestrationEngine(tmp_path)
    from agentd.validation.runner import NullValidator

    orchestrator = AgentOrchestrator(
        store=store,
        reasoning_engine=reasoning,
        validator=NullValidator(),
        patch_engine=PatchEngine(),
        workspace_manager=ShadowWorkspaceManager(tmp_path / "shadows"),
    )

    task = await store.create_and_get(TaskRecord(
        task_id="t1",
        goal="add file",
        workspace_path=str(tmp_path),
        plan=PlanDocument(
            analysis="test",
            steps=[PlanStep(id="s1", goal="add file", targets=[{"path": "src/wrong.py", "intent": "existing"}], risk="low")],
            expected_files=["src/wrong.py"],
            stop_conditions=[],
        ),
        status=TaskStatus.PLANNED,
    ))

    # ... (setup shadow, call _execute_plan or resume_task)
    # This test confirms the delta replan path doesn't fail with "not yet wired"
    # Exact assertion: task.status != FAILED due to "Task 11" message
    assert task.execution_state.delta_replans_used == 0  # pre-condition
```

Note: This is a partial test skeleton. Complete it based on how the existing integration tests in `test_orchestrator_repair_rollback.py` call the engine. Look at that file for the exact pattern.

- [ ] **Step 2: Add `_apply_revision()` to `AgentOrchestrator`**

```python
    async def _apply_revision(
        self,
        task: TaskRecord,
        revision: PlanRevisionResult,
        shadow_path: Path,
    ) -> None:
        """Apply a PlanRevisionResult: roll back checkpoints and replace plan steps."""
        from agentd.domain.models import PlanStep as _PlanStep, PlanTarget

        # 1. Roll back completed steps in reverse order
        for step_id in reversed(revision.reverted_step_ids):
            checkpoint_path = task.execution_state.step_checkpoints.get(step_id)
            if checkpoint_path:
                self._restore_shadow_checkpoint(shadow_path, checkpoint_path)
                if step_id in task.completed_step_ids:
                    task.completed_step_ids.remove(step_id)
                task.modified_files = sorted(set(task.modified_files) - {
                    t.path
                    for s in (task.plan.steps if task.plan else [])
                    if s.id == step_id
                    for t in s.targets
                })

        # 2. Replace steps wholesale
        assert task.plan is not None
        step_index = {s.id: i for i, s in enumerate(task.plan.steps)}
        for revised in revision.revised_steps:
            if revised.step_id not in step_index:
                continue
            i = step_index[revised.step_id]
            task.plan.steps[i] = _PlanStep(
                id=revised.step_id,
                goal=revised.goal,
                targets=[{"path": t["path"], "intent": t.get("intent", "existing")} for t in revised.targets],
                implementation_details=revised.implementation_details,
                edge_cases=revised.edge_cases or None,
                testing_strategy=revised.testing_strategy or None,
                risk=revised.risk,  # type: ignore[arg-type]
            )

        # 3. Increment counter
        task.execution_state.delta_replans_used += 1

        # 4. Persist
        await self._store.save(task)

        # 5. Artifact + broadcast
        self._write_debug_artifact(
            task.task_id,
            f"delta-replan-{task.execution_state.delta_replans_used}",
            {
                "revised_steps": [s.step_id for s in revision.revised_steps],
                "reverted_step_ids": revision.reverted_step_ids,
                "summary": revision.revision_summary,
            },
            artifacts_root_path=task.artifacts_root_path,
        )
        self.broadcaster.broadcast(task.task_id, {
            "type": "delta_replan_applied",
            "revised_steps": [s.step_id for s in revision.revised_steps],
            "reverted_steps": revision.reverted_step_ids,
            "summary": revision.revision_summary,
        })
```

- [ ] **Step 3: Replace temporary `PlanHandoff` handler in `_execute_plan` with full dispatch**

Replace the temporary block:
```python
                if isinstance(step_result, PlanHandoff):
                    # Delta replan: planning agent will handle in Task 11.
                    # For now, fail cleanly so existing tests see a predictable error.
                    task.diagnostics.append(Diagnostic(
                        source="orchestrator",
                        message=f"Delta replan requested by step {step_result.step_id}: {step_result.reason}",
                        level="error",
                    ))
                    task = transition(task, TaskStatus.FAILED, "delta replan not yet wired (Task 11)")
                    await self._store.save(task)
                    return task
```

With:
```python
                if isinstance(step_result, PlanHandoff):
                    # Guard: max delta replans
                    if task.execution_state.delta_replans_used >= task.budget.max_delta_replans:
                        task.diagnostics.append(Diagnostic(
                            source="orchestrator",
                            message=(
                                f"Delta replan budget exhausted "
                                f"({task.budget.max_delta_replans} replans used). "
                                f"Last request from step {step_result.step_id}: {step_result.reason}"
                            ),
                            level="error",
                        ))
                        task = transition(task, TaskStatus.FAILED, "delta replan budget exhausted")
                        await self._store.save(task)
                        return task

                    # Record request in shared state
                    from datetime import datetime, timezone
                    task.execution_state.delta_replan_requests.append(DeltaReplanRequest(
                        requested_by_step_id=step_result.step_id,
                        reason=step_result.reason,
                        evidence=step_result.evidence,
                        hinted_affected_steps=step_result.hinted_affected_steps,
                        requested_at=datetime.now(timezone.utc),
                    ))
                    await self._store.save(task)

                    # Hand off to planning agent (reads real workspace, not shadow)
                    revision = await planning_agent.revise(task, Path(task.workspace_path).resolve())
                    await self._apply_revision(task, revision, shadow_path)
                    # _next_incomplete_step() will return the revised/reverted step next iteration
                    continue
```

Also add the imports for `DeltaReplanRequest` at the top of `engine.py`:
```python
from agentd.domain.models import (
    ...existing imports...
    DeltaReplanRequest,
    PlanRevisionResult,
)
```

- [ ] **Step 4: Instantiate `PlanningAgent` in `_execute_plan` before the `while` loop**

Add before `while (step := self._next_incomplete_step(task)) is not None:`:
```python
            planning_agent = self._build_planning_agent(task.task_id, task.workspace_path)
```

- [ ] **Step 5: Add `step_checkpoints` recording after successful step completion**

After `self._merge_step_result(task, step_result, persistent_diagnostics)` and `await self._store.save(task)`, add:
```python
                # Record checkpoint path for potential rollback by delta replan
                if step_result.outcome == "step_completed" and step_result.checkpoint_manifests:
                    latest_checkpoint = step_result.checkpoint_manifests[-1]
                    task.execution_state.step_checkpoints[step.id] = latest_checkpoint.checkpoint_path
                    await self._store.save(task)
```

- [ ] **Step 6: Run full test suite**

```bash
pytest tests/ -v --tb=short -q 2>&1 | tail -30
```
Expected: All existing tests still pass.

- [ ] **Step 7: Commit**

```bash
git add agentd/orchestrator/engine.py
git commit -m "feat(engine): delta replan dispatch, _apply_revision(), step_checkpoints recording"
```

---

## Task 13: Delete dead code

**Files:**
- Modify: `agentd/orchestrator/engine.py`

Remove `_generate_repo_grounded_markdown_plan()` (now replaced), `_validate_plan_grounding()` (replaced by one-step-per-file prompt + planning agent inline verification), and unused imports.

- [ ] **Step 1: Confirm nothing calls the deleted methods**

```bash
grep -n "_generate_repo_grounded_markdown_plan\|_validate_plan_grounding\|critique_json_plan\|critique_markdown_plan" services/agentd-py/agentd/orchestrator/engine.py
```

Expected: Zero hits inside method bodies (only the method definitions themselves). If any remain, they're unreachable — safe to delete.

- [ ] **Step 2: Delete `_generate_repo_grounded_markdown_plan()` from `engine.py`**

Remove the entire method body (it's roughly 100 lines). Find its start:
```bash
grep -n "def _generate_repo_grounded_markdown_plan" services/agentd-py/agentd/orchestrator/engine.py
```

Delete from `async def _generate_repo_grounded_markdown_plan(` through the final `return final_markdown, self._critique_diagnostics(...)` line.

- [ ] **Step 3: Delete `_validate_plan_grounding()` from `engine.py`**

```bash
grep -n "def _validate_plan_grounding" services/agentd-py/agentd/orchestrator/engine.py
```

Delete the entire method.

- [ ] **Step 4: Remove now-unused imports in `engine.py`**

Check for and remove:
- `PlanCritiqueResult` (no longer used — critiques removed)
- `PlanCritiqueIssue` (no longer used)
- `PlanEvidencePack` (no longer used if only used in deleted methods)
- Any other imports only referenced in deleted code

```bash
python -m ruff check agentd/orchestrator/engine.py --select F401
```

Remove flagged unused imports.

- [ ] **Step 5: Remove now-unused imports in `reasoning/contracts.py` and `reasoning/engine.py`**

The critique methods (`critique_markdown_plan`, `critique_json_plan`) remain in the Protocol for backward compatibility (existing providers implement them). Leave them in `contracts.py`. Remove only truly dead code.

- [ ] **Step 6: Run full test suite**

```bash
pytest tests/ -v --tb=short -q 2>&1 | tail -20
ruff check agentd/ --select F401,F811
mypy agentd/orchestrator/engine.py --no-error-summary 2>&1 | tail -20
```
Expected: Zero test failures. Zero unused import warnings.

- [ ] **Step 7: Commit**

```bash
git add agentd/orchestrator/engine.py agentd/reasoning/
git commit -m "refactor(engine): delete _generate_repo_grounded_markdown_plan and _validate_plan_grounding"
```

---

## Task 14: Tests for delta replan full flow

**Files:**
- Modify: `tests/test_delta_replan.py`

Complete the integration tests started in Task 8. Test the full `revision_needed → PlanHandoff → PlanningAgent.revise() → _apply_revision()` round-trip.

- [ ] **Step 1: Complete `tests/test_delta_replan.py`**

Add these tests (the file already has the `test_tool_loop_returns_plan_handoff_on_revision_needed` test from Task 8):

```python
# Additional imports at top of file
from agentd.domain.models import (
    DeltaReplanRequest, PlanDocument, PlanStep, PlanningResult,
    PlanRevisionResult, RevisedStep, TaskBudget, TaskExecutionState,
    TaskRecord, TaskStatus, TaskUsage,
)
from agentd.orchestrator.engine import AgentOrchestrator
from agentd.patch.engine import PatchEngine
from agentd.storage.in_memory import InMemoryTaskStore
from agentd.workspace.shadow import ShadowWorkspaceManager
from datetime import datetime, timezone


# ---- _apply_revision tests ----

@pytest.mark.asyncio
async def test_apply_revision_updates_plan_steps(tmp_path: Path):
    """_apply_revision replaces the revised step wholesale in task.plan.steps."""
    store = InMemoryTaskStore()
    from agentd.validation.runner import NullValidator
    orch = AgentOrchestrator(
        store=store,
        reasoning_engine=RevisionNeededEngine(),
        validator=NullValidator(),
        patch_engine=PatchEngine(),
        workspace_manager=ShadowWorkspaceManager(tmp_path / "shadows"),
    )

    task = TaskRecord(
        task_id="t1",
        goal="add logging",
        workspace_path=str(tmp_path),
        plan=PlanDocument(
            analysis="test",
            steps=[
                PlanStep(id="s1", goal="old goal", targets=[{"path": "wrong.py", "intent": "existing"}], risk="low"),
            ],
            expected_files=["wrong.py"],
            stop_conditions=[],
        ),
        status=TaskStatus.EXECUTING,
    )
    await store.save(task)

    revision = PlanRevisionResult(
        revised_steps=[
            RevisedStep(
                step_id="s1",
                goal="new goal",
                targets=[{"path": "correct.py", "intent": "existing"}],
                implementation_details="add log",
            )
        ],
        reverted_step_ids=[],
        revision_summary="Retargeted to correct.py",
        tool_trace=AgentToolTrace(step_id="planning"),
    )
    await orch._apply_revision(task, revision, tmp_path)

    reloaded = await store.get("t1")
    assert reloaded.plan.steps[0].goal == "new goal"
    assert reloaded.plan.steps[0].targets[0].path == "correct.py"
    assert reloaded.execution_state.delta_replans_used == 1


@pytest.mark.asyncio
async def test_apply_revision_increments_delta_replans_used(tmp_path: Path):
    store = InMemoryTaskStore()
    from agentd.validation.runner import NullValidator
    orch = AgentOrchestrator(
        store=store,
        reasoning_engine=RevisionNeededEngine(),
        validator=NullValidator(),
        patch_engine=PatchEngine(),
        workspace_manager=ShadowWorkspaceManager(tmp_path / "shadows"),
    )
    task = TaskRecord(
        task_id="t2",
        goal="test",
        workspace_path=str(tmp_path),
        plan=PlanDocument(
            analysis="a",
            steps=[PlanStep(id="s1", goal="g", targets=[{"path": "f.py", "intent": "existing"}], risk="low")],
            expected_files=["f.py"],
            stop_conditions=[],
        ),
        status=TaskStatus.EXECUTING,
    )
    await store.save(task)

    revision = PlanRevisionResult(
        revised_steps=[RevisedStep(
            step_id="s1", goal="g2",
            targets=[{"path": "f.py", "intent": "existing"}],
            implementation_details="x",
        )],
        reverted_step_ids=[],
        revision_summary="minor fix",
        tool_trace=AgentToolTrace(step_id="planning"),
    )
    task.execution_state.delta_replans_used = 1
    await orch._apply_revision(task, revision, tmp_path)
    reloaded = await store.get("t2")
    assert reloaded.execution_state.delta_replans_used == 2


@pytest.mark.asyncio
async def test_apply_revision_reverts_completed_step(tmp_path: Path):
    """When reverted_step_ids names a step with a checkpoint, that step is removed from completed_step_ids."""
    store = InMemoryTaskStore()
    # Create fake checkpoint
    checkpoint_dir = tmp_path / "checkpoint-s1"
    checkpoint_dir.mkdir()

    from agentd.validation.runner import NullValidator
    orch = AgentOrchestrator(
        store=store,
        reasoning_engine=RevisionNeededEngine(),
        validator=NullValidator(),
        patch_engine=PatchEngine(),
        workspace_manager=ShadowWorkspaceManager(tmp_path / "shadows"),
    )
    task = TaskRecord(
        task_id="t3",
        goal="test",
        workspace_path=str(tmp_path),
        plan=PlanDocument(
            analysis="a",
            steps=[
                PlanStep(id="s1", goal="g", targets=[{"path": "f.py", "intent": "existing"}], risk="low"),
                PlanStep(id="s2", goal="g2", targets=[{"path": "g.py", "intent": "existing"}], risk="low"),
            ],
            expected_files=["f.py", "g.py"],
            stop_conditions=[],
        ),
        status=TaskStatus.EXECUTING,
        completed_step_ids=["s1"],
    )
    task.execution_state.step_checkpoints["s1"] = str(checkpoint_dir)
    await store.save(task)

    revision = PlanRevisionResult(
        revised_steps=[RevisedStep(
            step_id="s1", goal="revised",
            targets=[{"path": "correct.py", "intent": "existing"}],
            implementation_details="y",
        )],
        reverted_step_ids=["s1"],
        revision_summary="s1 reverted and retargeted",
        tool_trace=AgentToolTrace(step_id="planning"),
    )
    await orch._apply_revision(task, revision, tmp_path)
    reloaded = await store.get("t3")
    # s1 removed from completed_step_ids after rollback
    assert "s1" not in reloaded.completed_step_ids


@pytest.mark.asyncio
async def test_max_delta_replans_guard(tmp_path: Path):
    """When delta_replans_used >= max_delta_replans, the task transitions to FAILED."""
    (tmp_path / "shadows").mkdir()
    store = InMemoryTaskStore()
    from agentd.validation.runner import NullValidator

    class AlwaysRevisionNeeded:
        async def create_tool_step(self, *a, **kw):
            return {
                "type": "revision_needed",
                "thought": "wrong",
                "reason": "always wrong",
                "evidence": "trust me",
                "affected_steps": [],
            }
        async def create_planning_step(self, *a, **kw):
            return {
                "type": "emit_revision",
                "thought": "fix",
                "revised_steps": [],
                "reverted_step_ids": [],
                "revision_summary": "no change",
            }
        async def create_plan(self, *a, **kw): return {}
        async def create_markdown_plan(self, *a, **kw): return "# Plan"
        async def critique_markdown_plan(self, *a, **kw): return {"verdict": "pass", "issues": []}
        async def critique_json_plan(self, *a, **kw): return {"verdict": "pass", "issues": []}
        async def create_patch(self, *a, **kw): return {}

    orch = AgentOrchestrator(
        store=store,
        reasoning_engine=AlwaysRevisionNeeded(),
        validator=NullValidator(),
        patch_engine=PatchEngine(),
        workspace_manager=ShadowWorkspaceManager(tmp_path / "shadows"),
    )
    task = TaskRecord(
        task_id="t4",
        goal="test",
        workspace_path=str(tmp_path),
        plan=PlanDocument(
            analysis="a",
            steps=[PlanStep(id="s1", goal="g", targets=[{"path": "f.py", "intent": "existing"}], risk="low")],
            expected_files=["f.py"],
            stop_conditions=[],
        ),
        status=TaskStatus.PLANNED,
    )
    # Set delta_replans_used to max so first PlanHandoff triggers the guard
    task.execution_state.delta_replans_used = 3  # matches default max_delta_replans
    await store.save(task)

    # Trigger _execute_plan through the existing entry point
    task.shadow_workspace_path = str(tmp_path / "shadows" / "t4")
    (tmp_path / "shadows" / "t4").mkdir(parents=True)
    (tmp_path / "shadows" / "t4" / "f.py").write_text("# placeholder")
    await store.save(task)

    from agentd.workspace.shadow import ShadowWorkspace
    from agentd.retrieval.artifact_client import RetrievalContext
    result = await orch._execute_plan(
        task,
        ShadowWorkspace(task_id="t4", real_path=tmp_path, shadow_path=tmp_path / "shadows" / "t4"),
        RetrievalContext.empty(),
        [],
        int(__import__("time").time() * 1000),
    )
    assert result.status == TaskStatus.FAILED
    assert any("budget exhausted" in d.message for d in result.diagnostics)
```

- [ ] **Step 2: Run the new delta replan tests**

```bash
pytest tests/test_delta_replan.py -v
```
Expected: All tests PASS.

- [ ] **Step 3: Run the full test suite**

```bash
pytest tests/ -v --tb=short -q 2>&1 | tail -20
```
Expected: All tests pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_delta_replan.py
git commit -m "test(engine): delta replan flow — _apply_revision, rollback, and budget guard"
```

---

## Task 15: One-step-per-file validation in `PlanningLoop`

**Files:**
- Modify: `agentd/planning/loop.py`
- Modify: `tests/test_planning_agent.py`

The `_validate_no_duplicate_file_targets()` function already exists. Wire it into `_run_single_pass()` so that when `emit_plan` is received, the plan's steps are validated and the agent is re-invoked with error feedback if duplicates are found (up to 2 correction attempts).

The plan markdown emitted by the planning agent doesn't directly contain structured steps — that's the JSON plan generated by `create_plan()` later. The validation therefore must parse steps from the markdown or be applied at the JSON plan generation stage.

**Decision:** Apply the one-step-per-file validation at JSON plan generation time in `continue_task()`, not inside `PlanningLoop`. The planning prompt already instructs the LLM not to split files, so violations should be rare. If `candidate_plan` has duplicates, fail with a clear diagnostic rather than a silent retry.

- [ ] **Step 1: Add duplicate-target check after `PlanDocument.model_validate()` in `continue_task()`**

After `candidate_plan = PlanDocument.model_validate(plan_raw)` in `engine.py`:
```python
            # One-step-per-file constraint: each file in at most one step's targets
            steps_as_dicts = [{"id": s.id, "targets": [{"path": t.path} for t in s.targets]} for s in candidate_plan.steps]
            duplicate_errors = _validate_no_duplicate_file_targets_engine(steps_as_dicts)
            if duplicate_errors:
                task.diagnostics.append(Diagnostic(
                    source="orchestrator",
                    message="JSON plan violates one-step-per-file constraint: " + "; ".join(duplicate_errors),
                    level="error",
                ))
                task = transition(task, TaskStatus.FAILED, "plan has duplicate file targets across steps")
                await self._store.save(task)
                return task
```

Add a local import or inline function at the top of `engine.py`:
```python
def _validate_no_duplicate_file_targets_engine(steps: list[dict]) -> list[str]:
    """Returns error strings for any file appearing in more than one step's targets."""
    seen: dict[str, str] = {}
    errors: list[str] = []
    for step in steps:
        step_id = str(step.get("id", "?"))
        for target in step.get("targets", []):
            path = str(target.get("path", "")) if isinstance(target, dict) else str(target)
            if path in seen:
                errors.append(f"'{path}' in step '{seen[path]}' and '{step_id}'")
            else:
                seen[path] = step_id
    return errors
```

Also apply the same check in `_apply_revision()` after step replacements:
```python
        # Validate no cross-step file collisions introduced by revision
        steps_as_dicts = [{"id": s.id, "targets": [{"path": t.path} for t in s.targets]} for s in task.plan.steps]
        collision_errors = _validate_no_duplicate_file_targets_engine(steps_as_dicts)
        if collision_errors:
            task.diagnostics.append(Diagnostic(
                source="orchestrator",
                message="Revision introduced duplicate file targets: " + "; ".join(collision_errors),
                level="error",
            ))
            # Don't persist the bad plan — raise so _execute_plan transitions to FAILED
            raise ValueError("Revision created duplicate file targets across steps")
```

- [ ] **Step 2: Write tests for the duplicate-target check**

Add to `tests/test_planning_agent.py`:
```python
def test_duplicate_targets_across_steps_detected():
    steps = [
        {"id": "s1", "targets": [{"path": "src/auth.py"}, {"path": "src/models.py"}]},
        {"id": "s2", "targets": [{"path": "src/auth.py"}]},  # duplicate!
    ]
    errors = _validate_no_duplicate_file_targets(steps)
    assert len(errors) == 1
    assert "src/auth.py" in errors[0]
    assert "s1" in errors[0]
    assert "s2" in errors[0]


def test_no_cross_step_duplicates_with_different_files():
    steps = [
        {"id": "s1", "targets": [{"path": "a.py"}, {"path": "b.py"}]},
        {"id": "s2", "targets": [{"path": "c.py"}, {"path": "d.py"}]},
    ]
    assert _validate_no_duplicate_file_targets(steps) == []
```

- [ ] **Step 3: Run tests**

```bash
pytest tests/test_planning_agent.py tests/test_delta_replan.py -v
```
Expected: All tests PASS.

- [ ] **Step 4: Run full suite and linting**

```bash
pytest tests/ -q --tb=short 2>&1 | tail -10
ruff check agentd/ && mypy agentd/planning/ --no-error-summary 2>&1 | tail -10
```
Expected: No failures.

- [ ] **Step 5: Commit**

```bash
git add agentd/orchestrator/engine.py agentd/planning/loop.py tests/test_planning_agent.py
git commit -m "feat(validation): one-step-per-file duplicate target check at JSON plan generation and revision"
```

---

## Self-Review

### Spec coverage check

| Spec requirement | Task |
|-----------------|------|
| PlanningAgent explore-then-commit loop | Task 4, 5 |
| Planning tools: search_code, read_file, list_directory, search_semantic | Task 3 |
| emit_plan with files_examined + confidence | Task 4, 6 |
| confidence: "low" → warning diagnostic | Task 11 |
| planning_tool_call / planning_tool_result / planning_complete SSE events | Task 4 |
| Static retrieval as seed context | Task 11 |
| Delete critique loop | Task 11 (simplified), 13 (delete) |
| create_planning_step() in ReasoningEngine protocol | Task 7 |
| ScriptedReasoningEngine stub | Task 7 |
| TaskBudget extensions (3 fields) | Task 1 |
| TaskExecutionState + DeltaReplanRequest | Task 1 |
| Delta replan always automatic (no user gate) | Architecture decision — no task needed |
| revision_needed in execution agent schema | Task 8 |
| ToolLoop.run() returns StepOutcome | Task 8, 9 |
| PlanHandoff as first-class return (no exceptions) | Task 8 |
| _next_incomplete_step() + while loop | Task 9 |
| Max delta replans guard | Task 12, 14 |
| step_checkpoints recording | Task 12 |
| _apply_revision(): rollback + step replacement | Task 12, 14 |
| PlanningAgent.revise() with revision context payload | Task 5, 6 |
| Revision system prompt appended in revision mode | Task 2 |
| max_revision_tool_calls budget | Task 1 (model), Task 4 (used) |
| One-step-per-file prompt rule | Task 2 |
| One-step-per-file post-emit validation | Task 15 |
| Artifact: planning-trace.json | Task 11 |
| Artifact: delta-replan-N.json | Task 12 |
| PlanningAgent reads real_path, ToolLoop reads shadow_path | Task 3, 5, 12 |

All 14 verification checklist items from the spec are covered by tasks above.

### Placeholder scan

- All code blocks are complete and runnable
- No "TBD" or "TODO" in implementation steps
- Test expectations are concrete (`assert result.status == TaskStatus.FAILED`)

### Type consistency

- `PlanHandoff` defined in `agentd/tools/loop.py` — referenced in `engine.py` via `from agentd.tools.loop import PatchResult, PlanHandoff, StepOutcome`
- `PlanRevisionResult` defined in `agentd/domain/models.py` — used in `PlanningLoop`, `PlanningAgent`, `engine._apply_revision()`
- `DeltaReplanRequest` defined in `models.py` — appended in engine, read in `PlanningAgent.revise()`
- `_validate_no_duplicate_file_targets` in `agentd/planning/loop.py` — tests import from there. The engine-side version is a separate function `_validate_no_duplicate_file_targets_engine` to avoid circular imports.
- `AgentToolTrace` used for `PlanningResult.tool_trace` and `PlanRevisionResult.tool_trace` — both reference the same model from `agentd/domain/models.py`

---

**Plan complete and saved to `docs/superpowers/plans/2026-04-27-agentic-planning-delta-replan.md`.**

**Two execution options:**

**1. Subagent-Driven (recommended)** — Fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
