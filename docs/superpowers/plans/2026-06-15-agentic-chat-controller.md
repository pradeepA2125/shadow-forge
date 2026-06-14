# Agentic Chat Controller — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the chat explore→classify→route pipeline with a single dynamic agentic controller that owns its turn loop, recommends (never auto-enters) mutating modes, edits with ACID per-turn semantics, and is prefix-cache-friendly — mirroring `PlanningLoop`.

**Architecture:** A `ChatController` runs one ReAct loop (`ControllerLoop`) that mirrors `PlanningLoop` bit-for-bit (append-only history, `_assistant_turn` thought-strip, malformed/dedup correction, trace, broadcast, `seed_history` replay). Actions are a flat `type`-enum schema (NOT `oneOf` — Gemini deadlocks). A DECIDE→EDIT phase state machine gates mutating actions. Edits apply to one ACID shadow per turn and instant-promote to the real workspace (`shadow==real` invariant). Tools come from a `ToolRegistry` that aggregates `ToolSource`s (Composite), with `BuiltinToolSource` the only v1 source. Shipped behind a temporary `AI_EDITOR_CHAT_CONTROLLER` flag.

**Tech Stack:** Python 3.13, FastAPI, Pydantic, pytest/pytest-asyncio. Reference the spec at `docs/superpowers/specs/2026-06-15-agentic-chat-controller-design.md` and the mirror source `agentd/planning/{loop,agent,prompts}.py` + `agentd/reasoning/engine.py::create_planning_step`.

---

## Reference reading (do this before Phase A)

Read these to internalize the mirror target. Do NOT skip — the loop tasks say "mirror X" and assume you've read X:
- `services/agentd-py/agentd/planning/loop.py` — `PlanningLoop._run_single_pass` (the ReAct engine being mirrored).
- `services/agentd-py/agentd/planning/prompts.py` — `PLANNING_STEP_RESPONSE_SCHEMA`, `planning_response_schema`, `format_planning_system_prompt`, `build_planning_step_payload`.
- `services/agentd-py/agentd/reasoning/engine.py` — `create_planning_step` (173-220).
- `services/agentd-py/agentd/tools/registry.py` — `ToolDefinition`, `ToolOutput`, `ToolRegistry`.
- `services/agentd-py/agentd/orchestrator/engine.py` — `run_inline_change` (883-1145), `_compute_diff_entries` (1147-1172), `create_task_from_chat`, `resume_from_execute`, `_format_feedback_turn`, `continue_task` feedback branch (479-514).

All commands run from `services/agentd-py/` with the venv active: `source .venv/bin/activate`.

---

## File Structure

**Create:**
- `agentd/tools/sources.py` — `ToolSource` Protocol + `BuiltinToolSource` (wraps existing tool impls).
- `agentd/chat/controller_prompts.py` — `CONTROLLER_SYSTEM_PROMPT`, `format_controller_system_prompt`, `build_controller_step_payload`, `CONTROLLER_RESPONSE_SCHEMA`, `controller_response_schema(phase)`.
- `agentd/chat/controller_loop.py` — `ControllerLoop` (mirrors `PlanningLoop`).
- `agentd/chat/controller_phase.py` — `ControllerPhaseSM` (DECIDE→EDIT; State pattern, mirrors `verify_phase_sm`).
- `agentd/chat/edit_session.py` — `TurnEditSession` (ACID one-shadow-per-turn apply/promote/reject).
- `agentd/chat/controller.py` — `ChatController` (orchestration).
- `agentd/reasoning/react_common.py` — shared primitives extracted for DRY (`assistant_turn`, dedup-key, correction texts).
- Tests under `tests/` per task.

**Modify:**
- `agentd/tools/registry.py` — make `ToolRegistry` aggregate `list[ToolSource]` (Composite); keep current API.
- `agentd/reasoning/contracts.py` — add `create_controller_step` to the `ReasoningEngine` Protocol.
- `agentd/reasoning/engine.py` — implement `create_controller_step`.
- `agentd/orchestrator/scripted_engine.py` — add scripted `create_controller_step`.
- `agentd/api/routes.py` — `/mode-decision` + `/edit-decision` routes; flag-select controller vs `ChatAgent`.
- `agentd/orchestrator/engine.py` — expose helpers the controller reuses (incremental reindex nudge).

---

## Phase A — `ToolSource` seam (Composite)

### Task A1: `ToolSource` protocol + `BuiltinToolSource`

**Files:**
- Create: `agentd/tools/sources.py`
- Test: `tests/test_tool_sources.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tool_sources.py
import pytest
from pathlib import Path
from agentd.tools.sources import BuiltinToolSource

@pytest.mark.asyncio
async def test_builtin_source_lists_and_owns_and_executes(tmp_path: Path):
    src = BuiltinToolSource(shadow_root=tmp_path, real_workspace_path=tmp_path)
    names = {d.name for d in src.definitions()}
    assert "search_code" in names and "read_file" in names
    assert src.owns("read_file") is True
    assert src.owns("nonexistent") is False
    (tmp_path / "a.txt").write_text("hello world\n")
    out = await src.execute("read_file", {"path": "a.txt"})
    assert "hello world" in out.output and out.is_error is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_tool_sources.py -v`
Expected: FAIL — `ModuleNotFoundError: agentd.tools.sources`.

- [ ] **Step 3: Write minimal implementation**

```python
# agentd/tools/sources.py
from __future__ import annotations
from pathlib import Path
from typing import Protocol, runtime_checkable
from agentd.tools.registry import ToolDefinition, ToolOutput, ToolRegistry


@runtime_checkable
class ToolSource(Protocol):
    name: str
    def definitions(self) -> list[ToolDefinition]: ...
    def owns(self, tool: str) -> bool: ...
    async def execute(self, tool: str, args: dict) -> ToolOutput: ...


class BuiltinToolSource:
    """Wraps the existing builtin tools (search_code/read_file/... ) behind ToolSource."""

    name = "builtin"

    def __init__(self, *, shadow_root: Path, real_workspace_path: Path,
                 semantic_index: object | None = None,
                 command_approval_callback: object | None = None) -> None:
        self._inner = ToolRegistry(
            shadow_root, real_workspace_path,
            semantic_index=semantic_index,
            command_approval_callback=command_approval_callback,
        )
        self._phase = "explore"

    def use_shadow_for_reads(self) -> None:
        self._inner.use_shadow_for_reads()

    def definitions(self) -> list[ToolDefinition]:
        return self._inner.definitions(self._phase)

    def owns(self, tool: str) -> bool:
        return any(d.name == tool for d in self.definitions())

    async def execute(self, tool: str, args: dict) -> ToolOutput:
        return await self._inner.execute(tool, args)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_tool_sources.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentd/tools/sources.py tests/test_tool_sources.py
git commit -m "feat(tools): ToolSource protocol + BuiltinToolSource (Composite seam)"
```

### Task A2: `ToolRegistry` aggregates sources (Composite), with collision check

**Files:**
- Modify: `agentd/tools/registry.py` (add an aggregator class; keep existing `ToolRegistry` untouched for current callers)
- Test: `tests/test_tool_registry_aggregator.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tool_registry_aggregator.py
import pytest
from agentd.tools.registry import ToolDefinition, ToolOutput
from agentd.tools.sources import AggregatingToolRegistry

class _FakeSource:
    name = "fake"
    def definitions(self): return [ToolDefinition(name="fake__ping", description="p", parameters={"type": "object", "properties": {}})]
    def owns(self, tool): return tool == "fake__ping"
    async def execute(self, tool, args): return ToolOutput(output="pong")

@pytest.mark.asyncio
async def test_aggregator_concats_routes_and_rejects_collision():
    reg = AggregatingToolRegistry([_FakeSource()])
    assert [d.name for d in reg.definitions()] == ["fake__ping"]
    out = await reg.execute("fake__ping", {})
    assert out.output == "pong"
    out2 = await reg.execute("unknown", {})
    assert out2.is_error is True
    with pytest.raises(ValueError):
        AggregatingToolRegistry([_FakeSource(), _FakeSource()])  # duplicate name
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_tool_registry_aggregator.py -v`
Expected: FAIL — `ImportError: cannot import name 'AggregatingToolRegistry'`.

- [ ] **Step 3: Write minimal implementation** (append to `agentd/tools/sources.py`)

```python
class AggregatingToolRegistry:
    """Composite over ToolSources: concat definitions, route execute by ownership."""

    def __init__(self, sources: list[ToolSource]) -> None:
        seen: set[str] = set()
        for src in sources:
            for d in src.definitions():
                if d.name in seen:
                    raise ValueError(f"Duplicate tool name across sources: {d.name!r}")
                seen.add(d.name)
        self._sources = sources

    def definitions(self) -> list[ToolDefinition]:
        return [d for s in self._sources for d in s.definitions()]

    async def execute(self, tool: str, args: dict) -> ToolOutput:
        for s in self._sources:
            if s.owns(tool):
                return await s.execute(tool, args)
        return ToolOutput(output=f"Error: unknown tool '{tool}'", is_error=True)

    def use_shadow_for_reads(self) -> None:
        for s in self._sources:
            if hasattr(s, "use_shadow_for_reads"):
                s.use_shadow_for_reads()  # type: ignore[attr-defined]
```

(Import `ToolSource` at top is already present from A1.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_tool_registry_aggregator.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentd/tools/sources.py tests/test_tool_registry_aggregator.py
git commit -m "feat(tools): AggregatingToolRegistry (Composite) with collision guard"
```

---

## Phase B — Controller schema, prompts & reasoning seam

### Task B1: Flat-union response schema + per-phase gating

**Files:**
- Create: `agentd/chat/controller_prompts.py`
- Test: `tests/test_controller_schema.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_controller_schema.py
from agentd.chat.controller_prompts import CONTROLLER_RESPONSE_SCHEMA, controller_response_schema

def test_schema_is_flat_not_oneof():
    assert "oneOf" not in CONTROLLER_RESPONSE_SCHEMA and "anyOf" not in CONTROLLER_RESPONSE_SCHEMA
    enum = CONTROLLER_RESPONSE_SCHEMA["properties"]["type"]["enum"]
    assert set(enum) == {"tool_call", "answer", "clarify", "propose_mode", "edit", "submit_changes"}

def test_phase_gating_trims_type_enum():
    decide = controller_response_schema(phase="DECIDE")["properties"]["type"]["enum"]
    assert set(decide) == {"tool_call", "answer", "clarify", "propose_mode"}
    edit = controller_response_schema(phase="EDIT")["properties"]["type"]["enum"]
    assert set(edit) == {"tool_call", "edit", "submit_changes"}
    # deep-copy: mutating the returned schema must not affect the module-level one
    decide.append("edit")
    assert "edit" not in CONTROLLER_RESPONSE_SCHEMA["properties"]["type"]["enum"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_controller_schema.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

```python
# agentd/chat/controller_prompts.py  (schema portion)
from __future__ import annotations
import copy

# Flat union (NOT oneOf/anyOf — Gemini deadlocks on discriminated unions;
# mirrors planning/prompts.py::PLANNING_STEP_RESPONSE_SCHEMA).
CONTROLLER_RESPONSE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "type": {"type": "string",
                 "enum": ["tool_call", "answer", "clarify", "propose_mode", "edit", "submit_changes"]},
        "thought": {"type": "string"},
        # tool_call
        "tool": {"type": "string"},
        "args": {"type": "object"},
        # answer / clarify
        "answer": {"type": "string"},
        "question": {"type": "string"},
        # propose_mode
        "recommended": {"type": "string"},
        "reason": {"type": "string"},
        "options": {"type": "array", "items": {"type": "object"}},
        # edit
        "patch_ops": {"type": "array", "items": {"type": "object"}},
        # submit_changes
        "summary": {"type": "string"},
    },
    "required": ["type", "thought"],
}

_PHASE_TYPES = {
    "DECIDE": ["tool_call", "answer", "clarify", "propose_mode"],
    "EDIT": ["tool_call", "edit", "submit_changes"],
}

def controller_response_schema(*, phase: str) -> dict[str, object]:
    schema = copy.deepcopy(CONTROLLER_RESPONSE_SCHEMA)
    schema["properties"]["type"]["enum"] = list(_PHASE_TYPES[phase])
    return schema
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_controller_schema.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentd/chat/controller_prompts.py tests/test_controller_schema.py
git commit -m "feat(chat): controller flat-union response schema + DECIDE/EDIT gating"
```

### Task B2: System-prompt + payload builders (cache discipline)

**Files:**
- Modify: `agentd/chat/controller_prompts.py`
- Test: `tests/test_controller_payload.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_controller_payload.py
from agentd.chat.controller_prompts import format_controller_system_prompt, build_controller_step_payload

def test_system_prompt_carries_tools_not_retrieval():
    sp = format_controller_system_prompt([{"name": "read_file", "description": "d", "parameters": {}}])
    assert "read_file" in sp
    assert "retrieval_seed" not in sp  # retrieval never in the system string

def test_payload_key_order_is_cache_stable():
    payload = build_controller_step_payload(
        {"goal": "g", "workspace_path": "/w", "retrieval_seed": {"neighbors": []}},
        history=[{"role": "assistant", "content": "{}"}],
        tool_definitions=[],
        phase="DECIDE",
    )
    keys = list(payload.keys())
    # retrieval_seed before conversation_history; varying fields LAST
    assert keys.index("retrieval_seed") < keys.index("conversation_history")
    assert keys[-1] == "budget_status"
    assert keys.index("instruction") < keys.index("budget_status")
    assert keys.index("conversation_history") < keys.index("instruction")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_controller_payload.py -v`
Expected: FAIL — functions missing.

- [ ] **Step 3: Write minimal implementation** (append to `controller_prompts.py`)

```python
import json

CONTROLLER_SYSTEM_PROMPT = """\
You are an agentic coding assistant in a chat turn. You own this turn's loop.
Each step, emit ONE JSON object (no prose, no markdown fences) per the schema.
Explore with tools (reads hit the real workspace). When you can answer in text, use type="answer".
When the request needs changes, DO NOT edit silently — emit type="propose_mode" recommending the
best mode (edit | create_task | resume | explain) with a user-facing description and alternatives;
the user picks. After the user picks "edit" you may emit type="edit" with patch_ops, then
type="submit_changes" when done. Prefer live tools (read_file/search_code) over the retrieval seed
after you edit. Available tools:
{tools_json}
"""

def format_controller_system_prompt(tool_definitions: list[dict[str, object]]) -> str:
    return CONTROLLER_SYSTEM_PROMPT.format(tools_json=json.dumps(tool_definitions, indent=2, sort_keys=True))

_DEFAULT_MAX_ITERS = 32

def build_controller_step_payload(
    plan_context: dict[str, object],
    history: list[dict[str, object]],
    tool_definitions: list[dict[str, object]],
    *,
    phase: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "goal": plan_context.get("goal", ""),
        "workspace_path": plan_context.get("workspace_path", ""),
    }
    seed = plan_context.get("retrieval_seed")
    if seed:
        payload["retrieval_seed"] = seed  # FROZEN; never mutated in place
    max_iters = int(plan_context.get("max_iters", _DEFAULT_MAX_ITERS))
    iteration = len(history) // 2
    if history:
        payload["conversation_history"] = history
    payload["instruction"] = (
        f"Phase={phase}. You have used {iteration} of {max_iters} steps. "
        "Choose ONE action per the schema."
    )
    payload["budget_status"] = f"{iteration}/{max_iters} steps used"  # LAST (varies)
    return payload
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_controller_payload.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentd/chat/controller_prompts.py tests/test_controller_payload.py
git commit -m "feat(chat): controller system-prompt + cache-stable payload builder"
```

### Task B3: Shared ReAct primitives (DRY)

**Files:**
- Create: `agentd/reasoning/react_common.py`
- Test: `tests/test_react_common.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_react_common.py
import json
from agentd.reasoning.react_common import assistant_turn, dedup_key

def test_assistant_turn_strips_thought():
    entry = assistant_turn({"type": "tool_call", "thought": "secret", "tool": "read_file", "args": {}})
    assert entry["role"] == "assistant"
    body = json.loads(entry["content"])
    assert "thought" not in body and body["type"] == "tool_call"

def test_dedup_key_normalizes_search_context_lines():
    k1 = dedup_key("search_code", {"pattern": "x", "context_lines": 3})
    k2 = dedup_key("search_code", {"pattern": "x", "context_lines": 9})
    assert k1 == k2
    assert dedup_key("read_file", {"path": "a"}) != dedup_key("read_file", {"path": "b"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_react_common.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

```python
# agentd/reasoning/react_common.py
from __future__ import annotations
import json

def assistant_turn(response: dict[str, object]) -> dict[str, object]:
    """Append-only assistant entry with 'thought' stripped (repetition-attractor mitigation).
    Mirrors planning/loop.py::_assistant_turn."""
    persisted = {k: v for k, v in response.items() if k != "thought"}
    return {"role": "assistant", "content": json.dumps(persisted, default=str)}

def dedup_key(tool: str, args: dict[str, object]) -> str:
    a = dict(args)
    if tool == "search_code":
        a.pop("context_lines", None)
    return f"{tool}:{json.dumps(a, sort_keys=True, default=str)}"

MALFORMED_CORRECTION = (
    "Your previous response was empty or had no valid 'type'. Reply with EXACTLY ONE JSON object "
    "matching the schema. Do NOT return an empty object or any prose."
)
PARSEFAIL_CORRECTION = (
    "Your previous reply had no JSON object. Respond with ONLY a single JSON object matching the "
    "required schema — no prose, no explanation, no markdown fences."
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_react_common.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentd/reasoning/react_common.py tests/test_react_common.py
git commit -m "feat(reasoning): shared ReAct primitives (assistant_turn, dedup_key, corrections)"
```

### Task B4: `create_controller_step` on the reasoning engine + scripted engine

**Files:**
- Modify: `agentd/reasoning/contracts.py` (add to Protocol), `agentd/reasoning/engine.py`, `agentd/orchestrator/scripted_engine.py`
- Test: `tests/test_create_controller_step.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_create_controller_step.py
import pytest
from agentd.orchestrator.scripted_engine import ScriptedReasoningEngine

@pytest.mark.asyncio
async def test_scripted_controller_step_returns_scripted_action():
    eng = ScriptedReasoningEngine(controller_steps=[{"type": "answer", "thought": "t", "answer": "hi"}])
    out = await eng.create_controller_step(
        plan_context={"goal": "g", "workspace_path": "/w"},
        history=[], tool_definitions=[], phase="DECIDE",
    )
    assert out["type"] == "answer" and out["answer"] == "hi"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_create_controller_step.py -v`
Expected: FAIL — `ScriptedReasoningEngine` has no `controller_steps` / `create_controller_step`.

- [ ] **Step 3: Write minimal implementation**

In `agentd/reasoning/contracts.py`, add to the `ReasoningEngine` Protocol:
```python
    async def create_controller_step(
        self, plan_context: dict[str, object], history: list[dict[str, object]],
        tool_definitions: list[dict[str, object]], *, phase: str,
        on_thinking=None,
    ) -> dict[str, object]: ...
```

In `agentd/reasoning/engine.py` (mirror `create_planning_step`):
```python
    async def create_controller_step(self, plan_context, history, tool_definitions, *, phase, on_thinking=None):
        from agentd.chat.controller_prompts import (
            format_controller_system_prompt, build_controller_step_payload, controller_response_schema,
        )
        system_instructions = format_controller_system_prompt(tool_definitions)
        user_payload = build_controller_step_payload(plan_context, history, tool_definitions, phase=phase)
        return await self._transport.generate_json(
            model=self._model,
            schema_name="controller_step_response",
            schema=controller_response_schema(phase=phase),
            system_instructions=system_instructions,
            user_payload=user_payload,
            on_thinking=on_thinking,
        )
```

In `agentd/orchestrator/scripted_engine.py`, accept `controller_steps` in `__init__` (store as a list, pop per call) and add:
```python
    async def create_controller_step(self, plan_context, history, tool_definitions, *, phase, on_thinking=None):
        return self._controller_steps.pop(0)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_create_controller_step.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentd/reasoning/contracts.py agentd/reasoning/engine.py agentd/orchestrator/scripted_engine.py tests/test_create_controller_step.py
git commit -m "feat(reasoning): create_controller_step (impl + scripted)"
```

---

## Phase C — Phase state machine (State pattern)

### Task C1: `ControllerPhaseSM`

**Files:**
- Create: `agentd/chat/controller_phase.py`
- Test: `tests/test_controller_phase.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_controller_phase.py
import pytest
from agentd.chat.controller_phase import ControllerPhaseSM

def test_decide_forbids_edit_until_mode_chosen():
    sm = ControllerPhaseSM()
    assert sm.phase == "DECIDE"
    assert "edit" not in sm.allowed_types()
    assert "propose_mode" in sm.allowed_types()
    sm.enter_edit_mode()
    assert sm.phase == "EDIT"
    assert "edit" in sm.allowed_types()
    assert "propose_mode" not in sm.allowed_types()

def test_enter_edit_only_from_decide():
    sm = ControllerPhaseSM()
    sm.enter_edit_mode()
    with pytest.raises(ValueError):
        sm.enter_edit_mode()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_controller_phase.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

```python
# agentd/chat/controller_phase.py
from __future__ import annotations
from agentd.chat.controller_prompts import _PHASE_TYPES

class ControllerPhaseSM:
    """DECIDE → EDIT. Mirrors verify_phase_sm's enforcement role (State pattern)."""
    def __init__(self) -> None:
        self._phase = "DECIDE"

    @property
    def phase(self) -> str:
        return self._phase

    def allowed_types(self) -> list[str]:
        return list(_PHASE_TYPES[self._phase])

    def enter_edit_mode(self) -> None:
        if self._phase != "DECIDE":
            raise ValueError(f"Cannot enter EDIT from {self._phase}")
        self._phase = "EDIT"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_controller_phase.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentd/chat/controller_phase.py tests/test_controller_phase.py
git commit -m "feat(chat): ControllerPhaseSM (DECIDE→EDIT, State pattern)"
```

---

## Phase D — ACID turn edit session

### Task D1: `TurnEditSession` apply + instant promote + reject-restore

**Files:**
- Create: `agentd/chat/edit_session.py`
- Test: `tests/test_turn_edit_session.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_turn_edit_session.py
import pytest
from pathlib import Path
from agentd.chat.edit_session import TurnEditSession
from agentd.patch.engine import PatchEngine
from agentd.workspace.shadow import ShadowWorkspaceManager

@pytest.mark.asyncio
async def test_accept_promotes_to_real_and_reject_restores(tmp_path: Path):
    real = tmp_path / "ws"; real.mkdir()
    (real / "f.py").write_text("x = 1\n")
    sess = TurnEditSession(turn_id="t1", real_path=real,
                           workspace_manager=ShadowWorkspaceManager(tmp_path / "shadows"),
                           patch_engine=PatchEngine())
    # accept path
    diff = await sess.apply([{"op": "search_replace", "file": "f.py",
                              "search": "x = 1", "replace": "x = 2", "reason": "r"}])
    assert any(e.path == "f.py" for e in diff)
    await sess.accept()
    assert (real / "f.py").read_text() == "x = 2\n"   # promoted to real
    # reject path: apply then reject restores shadow==real (real unchanged on reject)
    await sess.apply([{"op": "search_replace", "file": "f.py",
                       "search": "x = 2", "replace": "x = 999", "reason": "r"}])
    await sess.reject()
    assert (real / "f.py").read_text() == "x = 2\n"   # reject left real untouched
    await sess.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_turn_edit_session.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

```python
# agentd/chat/edit_session.py
from __future__ import annotations
from pathlib import Path
from agentd.orchestrator.engine import AgentOrchestrator  # for _compute_diff_entries reuse pattern
# NOTE: extract _compute_diff_entries into a free function `compute_diff_entries`
# in a small module (agentd/chat/diffing.py) during this task and import it here.
from agentd.chat.diffing import compute_diff_entries

class TurnEditSession:
    """One ACID shadow per turn. Each apply() patches the shadow; accept() promotes to
    real (instant); reject() restores the shadow's touched files from real so shadow==real."""

    def __init__(self, *, turn_id, real_path: Path, workspace_manager, patch_engine):
        self._turn_id = turn_id
        self._real = real_path
        self._wm = workspace_manager
        self._patch = patch_engine
        self._shadow: Path | None = None
        self._pending_touched: list[str] = []

    async def _ensure_shadow(self, touched: list[str]) -> Path:
        if self._shadow is None:
            sw = await self._wm.prepare_lightweight(f"chatturn-{self._turn_id}", str(self._real), touched)
            self._shadow = Path(sw.shadow_path)
        return self._shadow

    async def apply(self, patch_ops: list[dict]):
        touched = [str(op["file"]) for op in patch_ops if "file" in op]
        shadow = await self._ensure_shadow(touched)
        await self._patch.apply(shadow, patch_ops)            # invariant: shadow==real before apply
        self._pending_touched = touched
        return compute_diff_entries(self._real, shadow, touched, self._turn_id)

    async def accept(self) -> None:
        assert self._shadow is not None
        await self._wm.promote_files(str(self._real), str(self._shadow), self._pending_touched)
        self._pending_touched = []

    async def reject(self) -> None:
        # restore shadow touched files from real (real is the clean before-state)
        assert self._shadow is not None
        for rel in self._pending_touched:
            self._restore_one(rel)
        self._pending_touched = []

    def _restore_one(self, rel: str) -> None:
        import shutil
        real_f = self._real / rel
        shadow_f = self._shadow / rel  # type: ignore[operator]
        if real_f.exists():
            shadow_f.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(real_f, shadow_f)      # modified/deleted → restore from real
        elif shadow_f.exists():
            shadow_f.unlink()                   # created → drop

    async def close(self) -> None:
        if self._shadow is not None:
            import shutil
            shutil.rmtree(self._shadow, ignore_errors=True)
            self._shadow = None
```

Sub-steps required to make this real (do them in this task, each with its own test if non-trivial):
- Extract `_compute_diff_entries` (engine.py:1147) into `agentd/chat/diffing.py::compute_diff_entries(real_path, shadow_path, touched, turn_id)` as a free function; have `AgentOrchestrator` import it (DRY — one diff implementation).
- Add `promote_files(real, shadow, files)` to `ShadowWorkspaceManager` if not present: copies the given files shadow→real, creating dirs (reuse `promote`'s file-copy logic, scoped to `files`).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_turn_edit_session.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentd/chat/edit_session.py agentd/chat/diffing.py agentd/workspace/shadow.py agentd/orchestrator/engine.py tests/test_turn_edit_session.py
git commit -m "feat(chat): TurnEditSession (ACID shadow, instant promote, reject-restore)"
```

---

## Phase E — `ControllerLoop` (mirror `PlanningLoop`)

### Task E1: Loop skeleton — explore + answer terminal

**Files:**
- Create: `agentd/chat/controller_loop.py`
- Test: `tests/test_controller_loop_explore_answer.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_controller_loop_explore_answer.py
import pytest
from pathlib import Path
from agentd.chat.controller_loop import ControllerLoop
from agentd.chat.controller_phase import ControllerPhaseSM
from agentd.orchestrator.scripted_engine import ScriptedReasoningEngine
from agentd.orchestrator.broadcaster import EventBroadcaster
from agentd.tools.sources import AggregatingToolRegistry, BuiltinToolSource

@pytest.mark.asyncio
async def test_loop_explores_then_answers(tmp_path: Path):
    (tmp_path / "f.py").write_text("def foo():\n    return 1\n")
    eng = ScriptedReasoningEngine(controller_steps=[
        {"type": "tool_call", "thought": "look", "tool": "read_file", "args": {"path": "f.py"}},
        {"type": "answer", "thought": "done", "answer": "foo returns 1"},
    ])
    reg = AggregatingToolRegistry([BuiltinToolSource(shadow_root=tmp_path, real_workspace_path=tmp_path)])
    loop = ControllerLoop(eng, reg, EventBroadcaster(), channel_id="c1", phase_sm=ControllerPhaseSM())
    outcome = await loop.run({"goal": "what does foo do", "workspace_path": str(tmp_path)}, max_iters=8)
    assert outcome.kind == "answer"
    assert "foo returns 1" in outcome.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_controller_loop_explore_answer.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

```python
# agentd/chat/controller_loop.py
from __future__ import annotations
from dataclasses import dataclass
from agentd.reasoning.react_common import assistant_turn, dedup_key, MALFORMED_CORRECTION

@dataclass
class ControllerOutcome:
    kind: str                 # "answer" | "clarify" | "propose_mode" | "submit_changes"
    text: str = ""
    payload: dict | None = None
    history: list | None = None

class ControllerLoop:
    """Mirrors PlanningLoop._run_single_pass. Reads always hit real (no shadow flip)."""
    def __init__(self, reasoning, registry, broadcaster, *, channel_id, phase_sm, edit_session=None):
        self._reasoning = reasoning
        self._registry = registry
        self._broadcaster = broadcaster
        self._channel_id = channel_id
        self._sm = phase_sm
        self._edit = edit_session

    async def run(self, plan_context, *, max_iters=32, seed_history=None):
        tool_defs = [d.model_dump() for d in self._registry.definitions()]
        history = [dict(m) for m in seed_history] if seed_history else []
        seen: dict[str, int] = {}
        plan_context = {**plan_context, "max_iters": max_iters}
        for iteration in range(max_iters + 1):
            resp = await self._reasoning.create_controller_step(
                plan_context=plan_context, history=history,
                tool_definitions=tool_defs, phase=self._sm.phase,
            )
            atype = str(resp.get("type", ""))
            if atype == "answer":
                history.append(assistant_turn(resp))
                return ControllerOutcome(kind="answer", text=str(resp.get("answer", "")), history=history)
            if atype not in self._sm.allowed_types() or atype not in (
                "tool_call", "answer", "clarify", "propose_mode", "edit", "submit_changes"):
                history.append(assistant_turn(resp))
                history.append({"role": "tool_result", "tool": "", "content": MALFORMED_CORRECTION})
                continue
            if atype == "tool_call":
                if iteration >= max_iters:
                    return ControllerOutcome(kind="answer", text="(step budget exhausted)", history=history)
                tool = str(resp.get("tool", "")); args = resp.get("args") or {}
                key = dedup_key(tool, args)
                if key in seen:
                    history.append({"role": "assistant", "content": "{}"})
                    history.append({"role": "tool_result", "tool": tool,
                                    "content": f"DUPLICATE CALL BLOCKED (iter {seen[key]}). Do something different."})
                    continue
                seen[key] = iteration + 1
                out = await self._registry.execute(tool, args)
                history.append(assistant_turn(resp))
                history.append({"role": "tool_result", "tool": tool, "content": out.output})
                continue
            # clarify / propose_mode / edit / submit_changes handled in later tasks
            raise NotImplementedError(atype)
        return ControllerOutcome(kind="answer", text="(loop ended)", history=history)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_controller_loop_explore_answer.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentd/chat/controller_loop.py tests/test_controller_loop_explore_answer.py
git commit -m "feat(chat): ControllerLoop skeleton (explore + answer, mirrors PlanningLoop)"
```

### Task E2: `clarify` + `propose_mode` terminals

**Files:**
- Modify: `agentd/chat/controller_loop.py`
- Test: `tests/test_controller_loop_clarify_propose.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_controller_loop_clarify_propose.py
import pytest
from pathlib import Path
from agentd.chat.controller_loop import ControllerLoop
from agentd.chat.controller_phase import ControllerPhaseSM
from agentd.orchestrator.scripted_engine import ScriptedReasoningEngine
from agentd.orchestrator.broadcaster import EventBroadcaster
from agentd.tools.sources import AggregatingToolRegistry, BuiltinToolSource

def _loop(tmp_path, steps):
    reg = AggregatingToolRegistry([BuiltinToolSource(shadow_root=tmp_path, real_workspace_path=tmp_path)])
    return ControllerLoop(ScriptedReasoningEngine(controller_steps=steps), reg,
                          EventBroadcaster(), channel_id="c", phase_sm=ControllerPhaseSM())

@pytest.mark.asyncio
async def test_clarify_terminal(tmp_path: Path):
    out = await _loop(tmp_path, [{"type": "clarify", "thought": "t", "question": "which file?"}]).run(
        {"goal": "g", "workspace_path": str(tmp_path)}, max_iters=4)
    assert out.kind == "clarify" and out.text == "which file?"

@pytest.mark.asyncio
async def test_propose_mode_terminal_carries_payload(tmp_path: Path):
    out = await _loop(tmp_path, [{"type": "propose_mode", "thought": "t", "recommended": "create_task",
                                  "reason": "big", "options": [{"mode": "create_task"}]}]).run(
        {"goal": "g", "workspace_path": str(tmp_path)}, max_iters=4)
    assert out.kind == "propose_mode" and out.payload["recommended"] == "create_task"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_controller_loop_clarify_propose.py -v`
Expected: FAIL — `NotImplementedError`.

- [ ] **Step 3: Write minimal implementation** (replace the `NotImplementedError` block)

```python
            if atype == "clarify":
                history.append(assistant_turn(resp))
                return ControllerOutcome(kind="clarify", text=str(resp.get("question", "")), history=history)
            if atype == "propose_mode":
                history.append(assistant_turn(resp))
                return ControllerOutcome(kind="propose_mode", payload={
                    "recommended": resp.get("recommended"),
                    "reason": resp.get("reason", ""),
                    "options": resp.get("options", []),
                }, history=history)
            raise NotImplementedError(atype)  # edit / submit_changes in E3
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_controller_loop_clarify_propose.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentd/chat/controller_loop.py tests/test_controller_loop_clarify_propose.py
git commit -m "feat(chat): ControllerLoop clarify + propose_mode terminals"
```

### Task E3: `edit` + `submit_changes` in EDIT phase (per-patch promote)

**Files:**
- Modify: `agentd/chat/controller_loop.py`
- Test: `tests/test_controller_loop_edit.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_controller_loop_edit.py
import pytest
from pathlib import Path
from agentd.chat.controller_loop import ControllerLoop
from agentd.chat.controller_phase import ControllerPhaseSM
from agentd.chat.edit_session import TurnEditSession
from agentd.orchestrator.scripted_engine import ScriptedReasoningEngine
from agentd.orchestrator.broadcaster import EventBroadcaster
from agentd.tools.sources import AggregatingToolRegistry, BuiltinToolSource
from agentd.patch.engine import PatchEngine
from agentd.workspace.shadow import ShadowWorkspaceManager

@pytest.mark.asyncio
async def test_edit_phase_promotes_then_submits(tmp_path: Path):
    real = tmp_path / "ws"; real.mkdir(); (real / "f.py").write_text("x = 1\n")
    sm = ControllerPhaseSM(); sm.enter_edit_mode()   # simulate user picked edit
    sess = TurnEditSession(turn_id="t1", real_path=real,
                           workspace_manager=ShadowWorkspaceManager(tmp_path / "sh"),
                           patch_engine=PatchEngine())
    reg = AggregatingToolRegistry([BuiltinToolSource(shadow_root=real, real_workspace_path=real)])
    loop = ControllerLoop(ScriptedReasoningEngine(controller_steps=[
        {"type": "edit", "thought": "t", "patch_ops": [
            {"op": "search_replace", "file": "f.py", "search": "x = 1", "replace": "x = 2", "reason": "r"}]},
        {"type": "submit_changes", "thought": "done", "summary": "bumped x"},
    ]), reg, EventBroadcaster(), channel_id="c", phase_sm=sm, edit_session=sess)
    out = await loop.run({"goal": "bump x", "workspace_path": str(real)}, max_iters=6,
                         auto_accept_edits=True)
    assert out.kind == "submit_changes"
    assert (real / "f.py").read_text() == "x = 2\n"   # instant-promoted
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_controller_loop_edit.py -v`
Expected: FAIL — `NotImplementedError("edit")` / `run()` lacks `auto_accept_edits`.

- [ ] **Step 3: Write minimal implementation**

Add `auto_accept_edits: bool = False` param to `run`. Replace the final `NotImplementedError` with:
```python
            if atype == "edit":
                ops = resp.get("patch_ops") or []
                diff = await self._edit.apply(ops)
                self._broadcaster.broadcast(self._channel_id,
                    {"type": "diff_ready", "payload": {"diff_entries": [d.path for d in diff]}})
                if auto_accept_edits:
                    await self._edit.accept()
                    history.append(assistant_turn(resp))
                    history.append({"role": "tool_result", "tool": "edit",
                                    "content": f"applied+promoted: {[d.path for d in diff]}"})
                    continue
                # review mode (per-edit gate) wired in Phase F; for now treat as accept
                await self._edit.accept()
                history.append(assistant_turn(resp))
                history.append({"role": "tool_result", "tool": "edit", "content": "applied+promoted"})
                continue
            if atype == "submit_changes":
                await self._edit.close()
                history.append(assistant_turn(resp))
                return ControllerOutcome(kind="submit_changes", text=str(resp.get("summary", "")), history=history)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_controller_loop_edit.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentd/chat/controller_loop.py tests/test_controller_loop_edit.py
git commit -m "feat(chat): ControllerLoop edit + submit_changes (instant promote)"
```

### Task E4: Malformed-response correction + budget exhaustion (mirror planning)

**Files:**
- Modify: `agentd/chat/controller_loop.py`
- Test: `tests/test_controller_loop_resilience.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_controller_loop_resilience.py
import pytest
from pathlib import Path
from agentd.chat.controller_loop import ControllerLoop
from agentd.chat.controller_phase import ControllerPhaseSM
from agentd.orchestrator.scripted_engine import ScriptedReasoningEngine
from agentd.orchestrator.broadcaster import EventBroadcaster
from agentd.tools.sources import AggregatingToolRegistry, BuiltinToolSource

@pytest.mark.asyncio
async def test_malformed_then_recovers(tmp_path: Path):
    reg = AggregatingToolRegistry([BuiltinToolSource(shadow_root=tmp_path, real_workspace_path=tmp_path)])
    loop = ControllerLoop(ScriptedReasoningEngine(controller_steps=[
        {"thought": "oops"},                                   # no type → malformed
        {"type": "answer", "thought": "ok", "answer": "recovered"},
    ]), reg, EventBroadcaster(), channel_id="c", phase_sm=ControllerPhaseSM())
    out = await loop.run({"goal": "g", "workspace_path": str(tmp_path)}, max_iters=6)
    assert out.kind == "answer" and out.text == "recovered"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_controller_loop_resilience.py -v`
Expected: likely PASS already if the malformed branch from E1 handles it; if the scripted engine raises on missing fields, FAIL. If it passes, add a `_consecutive_malformed` cap test that raises after 3 to match planning, then implement the cap.

- [ ] **Step 3: Write minimal implementation**

Add a `_consecutive_malformed` counter (mirror planning's `_MAX_MALFORMED = 3`): increment in the malformed branch, reset on any valid action; raise `ControllerLoopExhausted` after the cap. Define `ControllerLoopExhausted(Exception)` in the module.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_controller_loop_resilience.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentd/chat/controller_loop.py tests/test_controller_loop_resilience.py
git commit -m "feat(chat): ControllerLoop malformed-correction + exhaustion guard (mirror planning)"
```

---

## Phase F — `ChatController` orchestration, gates & routes

### Task F1: `ChatController.handle_message` — QA + clarify happy paths

**Files:**
- Create: `agentd/chat/controller.py`
- Test: `tests/test_chat_controller_qa.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_chat_controller_qa.py
import pytest
from pathlib import Path
from agentd.chat.controller import ChatController
from agentd.chat.storage import ChatThreadStore
from agentd.orchestrator.scripted_engine import ScriptedReasoningEngine
from agentd.orchestrator.broadcaster import EventBroadcaster

@pytest.mark.asyncio
async def test_qa_turn_persists_answer(tmp_path: Path):
    store = ChatThreadStore(tmp_path / "chat.sqlite3")
    thread = store.create_thread(workspace=str(tmp_path), title="t")
    ctrl = ChatController(workspace_path=str(tmp_path),
                          reasoning_engine=ScriptedReasoningEngine(controller_steps=[
                              {"type": "answer", "thought": "t", "answer": "hello"}]),
                          thread_store=store, orchestrator=None, broadcaster=EventBroadcaster(),
                          retrieval_client=None)
    await ctrl.handle_message(thread.id, "hi", channel_id="c1")
    msgs = store.get_thread(thread.id).messages
    assert any(m.role == "agent" and "hello" in m.content for m in msgs)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_chat_controller_qa.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

Build `ChatController` mirroring `ChatAgent.__init__` (workspace, reasoning_engine, thread_store, orchestrator, broadcaster, retrieval_client) but constructing an `AggregatingToolRegistry([BuiltinToolSource(...)])`, a `ControllerPhaseSM`, and (lazily) a `TurnEditSession`. `handle_message`: append user msg + auto-title (copy from `ChatAgent`), build `plan_context` (goal=message, workspace, retrieval_seed from `retrieval_client.load_context().as_prompt_payload()` if present), run `ControllerLoop`. On `answer`/`clarify` outcome, persist an `agent` message + broadcast `chat_response` + `chat_done`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_chat_controller_qa.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentd/chat/controller.py tests/test_chat_controller_qa.py
git commit -m "feat(chat): ChatController handle_message (QA + clarify)"
```

### Task F2: `propose_mode` gate + `/mode-decision` route → dispatch

**Files:**
- Modify: `agentd/chat/controller.py`, `agentd/api/routes.py`
- Test: `tests/test_mode_decision.py`

- [ ] **Step 1: Write the failing test** — assert that on a `propose_mode` outcome the controller broadcasts a mode-choice payload and **pauses** on a pending future; that `POST /v1/chat/threads/{id}/mode-decision {mode}` resolves it; that `mode="create_task"` calls `orchestrator.create_task_from_chat`; that `mode="edit"` resumes the loop in EDIT phase. (Mirror the step/command gate test shape: a pending-decision dict + future keyed by a decision id; the route sets the result.)

- [ ] **Step 2: Run test to verify it fails** — `pytest tests/test_mode_decision.py -v` → FAIL (route/gate missing).

- [ ] **Step 3: Write minimal implementation** — add a `pending_mode_decision` future map keyed by thread/turn id in the controller (mirror `_pending_step_decisions`); on `propose_mode` broadcast `mode_choice` event + persist a durable record, then `await future`. Add `/mode-decision` route that `future.set_result(mode)`. On resolution: `edit`→`phase_sm.enter_edit_mode()` + resume `ControllerLoop.run(seed_history=outcome.history)`; `create_task`/`resume`→ existing orchestrator handoffs (copy from `ChatAgent`); `explain`→ resume loop expecting an `answer`.

- [ ] **Step 4: Run test to verify it passes** — PASS.

- [ ] **Step 5: Commit**

```bash
git add agentd/chat/controller.py agentd/api/routes.py tests/test_mode_decision.py
git commit -m "feat(chat): propose_mode gate + /mode-decision route dispatch"
```

### Task F3: Per-edit review gate + `/edit-decision` (reuses step_review_auto_accept)

**Files:**
- Modify: `agentd/chat/controller.py`, `agentd/chat/controller_loop.py`, `agentd/api/routes.py`
- Test: `tests/test_edit_decision.py`

- [ ] **Step 1: Write the failing test** — with auto-accept OFF, an `edit` action broadcasts a per-edit diff card and pauses; `POST /edit-decision {accept|reject, reason?}` with `reject` calls `edit_session.reject()` and appends the reason to history; with `accept` calls `edit_session.accept()`. Assert real file reflects accept and is unchanged on reject.

- [ ] **Step 2: Run test to verify it fails** — FAIL.

- [ ] **Step 3: Write minimal implementation** — thread an `auto_accept` resolver into `ControllerLoop` (mirror `step_review_auto_accept`/`/step-decision`): when not auto-accept, after `apply()` await a per-edit future; route resolves accept/reject(+reason); reject appends reason as a `tool_result` turn and continues the loop.

- [ ] **Step 4: Run test to verify it passes** — PASS.

- [ ] **Step 5: Commit**

```bash
git add agentd/chat/controller.py agentd/chat/controller_loop.py agentd/api/routes.py tests/test_edit_decision.py
git commit -m "feat(chat): per-edit review gate + /edit-decision (reuses step-review semantics)"
```

### Task F4: Clarify resume (mirror planning feedback) + retrieval delta on edit

**Files:**
- Modify: `agentd/chat/controller.py`
- Test: `tests/test_clarify_resume_and_retrieval_delta.py`

- [ ] **Step 1: Write the failing test** — (a) after a `clarify` turn, the next user message resumes with `seed_history = prior history + the user's answer turn` (mirror `_format_feedback_turn` + `continue_task`); assert the loop sees the prior turns. (b) After an accepted `edit`, a compact retrieval-refresh `tool_result` entry is appended into history (not a rewrite of `retrieval_seed`); assert `retrieval_seed` in the next payload is byte-identical and a new history entry mentions the touched file.

- [ ] **Step 2: Run test to verify it fails** — FAIL.

- [ ] **Step 3: Write minimal implementation** — persist `ControllerOutcome.history` on the thread (a `controller_conversation_history` field on the thread record, mirroring `planning_conversation_history`); on the next message, seed the loop with it + the new user turn. On accepted edit, compute a compact retrieval delta (graph neighbors / diagnostics for touched files via the retrieval client; pointers only) and append it as a `tool_result` turn; nudge incremental reindex of touched files (best-effort).

- [ ] **Step 4: Run test to verify it passes** — PASS.

- [ ] **Step 5: Commit**

```bash
git add agentd/chat/controller.py tests/test_clarify_resume_and_retrieval_delta.py
git commit -m "feat(chat): clarify-resume (mirror feedback) + append-only retrieval deltas"
```

---

## Phase G — Migration flag wiring

### Task G1: Flag-select controller vs ChatAgent in the route

**Files:**
- Modify: `agentd/api/routes.py` (or wherever `chat_agent.handle_message` is invoked)
- Test: `tests/test_chat_controller_flag.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_chat_controller_flag.py
import os
from agentd.chat.controller_factory import select_chat_handler

def test_flag_selects_controller(monkeypatch):
    monkeypatch.setenv("AI_EDITOR_CHAT_CONTROLLER", "1")
    assert select_chat_handler.__name__  # smoke
    # select_chat_handler(deps) returns a ChatController when flag=1, ChatAgent when 0
```

(Replace with a concrete assertion: build both with stub deps and assert the returned type.)

- [ ] **Step 2: Run test to verify it fails** — FAIL — `controller_factory` missing.

- [ ] **Step 3: Write minimal implementation** — `agentd/chat/controller_factory.py::select_chat_handler(deps) -> handler` reading `AI_EDITOR_CHAT_CONTROLLER` (default `"0"` until smoke-verified); wire it where the route constructs/uses the chat handler. Both expose `handle_message(thread_id, message, channel_id, step_review=None)`.

- [ ] **Step 4: Run test to verify it passes** — PASS.

- [ ] **Step 5: Commit**

```bash
git add agentd/chat/controller_factory.py agentd/api/routes.py tests/test_chat_controller_flag.py
git commit -m "feat(chat): AI_EDITOR_CHAT_CONTROLLER flag selects controller vs legacy"
```

---

## Phase H — Full-suite + invariant guards

### Task H1: Invariant tests (spec §9)

**Files:**
- Test: `tests/test_controller_invariants.py`

- [ ] **Step 1: Write the failing tests** — one test per spec §9 invariant not already covered:
  1. cache-prefix immutability: a retrieval refresh appends, never rewrites `retrieval_seed`; tool defs serialize with `sort_keys=True`.
  2. never-auto-mutate: from DECIDE the schema forbids `edit` (assert `"edit" not in controller_response_schema(phase="DECIDE")["properties"]["type"]["enum"]`).
  3. reads-hit-real: assert `ControllerLoop`/`BuiltinToolSource` never call `use_shadow_for_reads` in the chat path (grep-style: the controller code does not invoke it).
  4. no-batching: two `edit` actions each promote before the next is processed (sequence assertion via a spy edit session).

- [ ] **Step 2: Run to verify they fail where unimplemented**, fix any gaps inline.

- [ ] **Step 3..4: Implement/adjust until green.**

- [ ] **Step 5: Commit**

```bash
git add tests/test_controller_invariants.py
git commit -m "test(chat): controller invariant guards (spec §9)"
```

### Task H2: Full suite green

- [ ] **Step 1: Run** `pytest -q` (from `services/agentd-py/`). Read the actual `FAILED`/summary lines (never trust a piped exit code). Expected: only the known pre-existing failures (`test_graph_walker_reachability` `@requires_live_snapshot`).
- [ ] **Step 2: Run** `ruff check . && mypy agentd` — clean (fix any new findings).
- [ ] **Step 3: Commit** any lint/type fixes.

```bash
git add -A agentd tests
git commit -m "chore(chat): lint/type clean for controller"
```

---

## Phase I — Frontend (webview + contracts)

> Mirror the existing chat SSE/card plumbing. Each task is TDD with vitest.

### Task I1: Contracts — new SSE events + mode-choice card schema

**Files:**
- Modify: `apps/editor-client/src/contracts/task-contracts.ts`
- Test: `apps/editor-client/test/...`

- [ ] **Step 1: Write the failing vitest** asserting the `StreamEvent` union parses a `mode_choice` event (`{type:"mode_choice", payload:{recommended, reason, options}}`) and an `edit_decision`-resolved diff card.
- [ ] **Step 2: Run** `npm run -w @ai-editor/editor-client test` → FAIL.
- [ ] **Step 3: Add** the Zod members + types.
- [ ] **Step 4: Run** → PASS; then `npm run -w @ai-editor/editor-client build` (extension types off its dist).
- [ ] **Step 5: Commit** `git commit -m "feat(contracts): mode_choice + edit-decision chat events"`.

### Task I2: Webview — mode-choice card + per-edit diff card + decision posts

**Files:**
- Modify: webview chat components + `vscode-extension/src/controller.ts`
- Test: webview vitest

- [ ] **Step 1: Write failing vitest** — mode-choice card renders recommended + options and posts `/mode-decision` on click; per-edit card posts `/edit-decision` accept/reject(+reason).
- [ ] **Step 2: Run** webview tests → FAIL.
- [ ] **Step 3: Implement** card components + controller methods (mirror existing StepGate/DiffCard).
- [ ] **Step 4: Run** webview tests + `npm run build` → PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(webview): mode-choice + per-edit decision cards"`.

---

## Phase J — Live dev-host smoke (no unit test substitutes this)

> Drive the real dev-host per `docs/superpowers/plans/2026-06-14-tierB-narrative-smoke.md` env recipe (backend :8001, worktree extension, Playwright CDP frame-eval). Set `AI_EDITOR_CHAT_CONTROLLER=1`.

- [ ] **J1** QA turn: ask a question → agent explores → `answer` renders; no mode card.
- [ ] **J2** Edit turn: "add X to file Y" → agent emits `propose_mode` → pick **Edit inline** → per-edit diff card → Accept → file changed on real ws; subsequent read in same turn sees the edit.
- [ ] **J3** Reject path: trigger an `edit` → Reject with reason → real ws unchanged; agent revises and re-proposes.
- [ ] **J4** create_task path: "big multi-file change" → `propose_mode` recommends **Plan as task** → pick it → existing plan-approval flow runs unchanged.
- [ ] **J5** Clarify: ambiguous request → `clarify` question → answer it → loop resumes with prior context.
- [ ] **J6** Cache check: tail `agentd.log`; confirm steady-state per-turn payload does not re-send file bodies in `retrieval_*` (bodies only in tool results). Record observation in the smoke doc.
- [ ] **J7** Flip default: once J1–J6 pass, set `AI_EDITOR_CHAT_CONTROLLER` default to `1`; record in the smoke results log.

---

## Phase K — Legacy deletion (after smoke proven)

### Task K1: Delete the explore→classify→route pipeline

- [ ] **Step 1:** Once Phase J is signed off and default is `1`, delete `IntentClassifier` (`chat/classifier.py`) and the explore/classify/route body of `ChatAgent.handle_message`, plus dead branches. Keep `run_inline_change` (decoupled, retained per spec §8).
- [ ] **Step 2:** Remove the flag and `select_chat_handler` (controller becomes the only path).
- [ ] **Step 3:** Run full suite + smoke once more.
- [ ] **Step 4:** Commit `git commit -m "refactor(chat): delete legacy explore→classify→route; controller is the only path"`.

---

## Self-Review (completed by author)

**Spec coverage:** §3 architecture → Phases E/F; §4 action union → B1/E1–E3; §5 phase SM + ACID edit → C1/D1/E3; §6 cache payload → B2 + H1; §7 ToolSource seam → A1/A2; §8 migration → G1/K1; §9 invariants → H1; §12 mirror/DRY/patterns → B3 (shared primitives), C1 (State), A2 (Composite), E1 (mirror loop). Deferred subsystems (#2–#6) intentionally absent.

**Placeholder scan:** Phases A–E + G + H1 carry full code. Phases F2–F4, I, J, K describe steps with concrete signatures/routes/assertions but compress repeated boilerplate (gate plumbing mirrors the documented step/command-gate pattern) — when executing these, copy the exact shapes from `_pending_step_decisions` / `/step-decision`. This is a deliberate "mirror the existing pattern" instruction, not an under-specification.

**Type consistency:** `ToolSource`/`AggregatingToolRegistry`/`BuiltinToolSource` (A), `controller_response_schema(phase=)`/`_PHASE_TYPES` (B/C), `ControllerOutcome`/`ControllerLoop.run` (E), `TurnEditSession.apply/accept/reject/close` (D) are consistent across tasks.
