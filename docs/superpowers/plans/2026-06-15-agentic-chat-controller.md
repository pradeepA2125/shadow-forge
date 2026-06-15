# Agentic Chat Controller — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the chat explore→classify→route pipeline with a single dynamic agentic controller that owns its turn loop, recommends (never auto-enters) mutating modes, edits with ACID per-turn semantics, and is prefix-cache-friendly — mirroring `PlanningLoop`.

**Architecture:** A `ChatController` runs one ReAct loop (`ControllerLoop`) that mirrors `PlanningLoop` bit-for-bit (append-only history, `_assistant_turn` thought-strip, malformed/dedup correction, trace, broadcast, `seed_history` replay). Actions are a flat `type`-enum schema (NOT `oneOf` — Gemini deadlocks). A DECIDE→EDIT phase state machine gates mutating actions. Edits apply to one ACID shadow per turn and instant-promote to the real workspace (`shadow==real` invariant). Tools come from a `ToolRegistry` that aggregates `ToolSource`s (Composite), with `BuiltinToolSource` the only v1 source. Shipped behind a temporary `AI_EDITOR_CHAT_CONTROLLER` flag.

**Tech Stack:** Python 3.13, FastAPI, Pydantic, pytest/pytest-asyncio. Reference the spec at `docs/superpowers/specs/2026-06-15-agentic-chat-controller-design.md` and the mirror source `agentd/planning/{loop,agent,prompts}.py` + `agentd/reasoning/engine.py::create_planning_step`.

---

## ⏸️ RESUME / STATUS (handoff 2026-06-15) — read this first

**Execution mode:** inline TDD (executing-plans skill), per task: failing test → run red → impl → run green → `ruff check --fix` → commit. Code review at the F and H2 seams (user asked).

**Where I am — DONE & committed (worktree `.worktrees/feat-agentic-chat-controller`, branch `feat/agentic-chat-controller` off `main`@7be81ce; ~15 commits, all TDD-green, ruff-clean on new code):**
- ✅ **A** ToolSource seam (`tools/sources.py`: `ToolSource`, `BuiltinToolSource`, `AggregatingToolRegistry`)
- ✅ **B** `chat/controller_prompts.py` (flat schema + phase gating + builders), `reasoning/react_common.py`, `create_controller_step` (Protocol+impl+scripted)
- ✅ **C** `chat/controller_phase.py` (`ControllerPhaseSM` DECIDE→EDIT)
- ✅ **D0** `patch/diffing.py`+`patch/inline_apply.py`+`workspace/promote.py` (engine `_compute_diff_entries`/`_partial_promote` delegate; `_cap_unified_diff` re-exported) · ✅ **D1** `chat/edit_session.py` (`TurnEditSession`) — **code-reviewed clean**
- ✅ **E** `chat/controller_loop.py` (`ControllerLoop` + `ControllerOutcome` + `ControllerLoopExhausted`) — **Phase E complete (E1–E4):** explore (tool_call) + answer/clarify/propose_mode/edit/submit_changes terminals + dedup/malformed-correction (cap 3 → `ControllerLoopExhausted`) + EDIT-phase instant-promote (`run()` has `auto_accept_edits`, wired in F). Type-hardened mypy-clean (mirrors PlanningLoop annotations + `isinstance` arg-narrowing). All 6 loop tests green, ruff+mypy clean.

- ✅ **F** Phase F complete (F0–F4) **+ code-reviewed + fixes applied**: F0 thread-level controller gates (mode/edit) via `/live` (`pending_controller_gate` + `controller_gate_json` ALTER + `resolve_thread_live`); F1 `chat/controller.py` `ChatController.handle_message` (QA+clarify, seed caching, clarify-resume); F2 `propose_mode` Class-A gate + `resolve_mode` + `/mode-decision` streamed route; F3 per-edit held-stream gate (`_edit_decision_cb` future + timeout) + `resolve_edit` + `/edit-decision` route + loop `edit_decision_cb`; F4 clarify-resume regression + loop `retrieval_delta_cb` (append-only).
- **F review fixes (commit `a94cc63`):** [Crit] edit branch try/except so a bad search string feeds back vs crashes the turn; [Crit] `resolve_mode` pending-`mode`-gate precondition (asyncio-atomic read→clear) kills double-create; [Imp] `run()` try/finally closes turn-shadow on ANY exit (loop body → `_iterate`); [Imp] edit-mode `RuntimeError` when no orchestrator; [Min] TODO on unbounded `_histories`. Deferred/accepted: #5 `shadow==real` cross-file invariant → **H1 test**; #6 F4 static pointer-note (documented deviation); #7 resume degraded; #9 `type: ignore` route seam → resolves in G. **Full suite 755 passed / 3 skipped / 0 failed; new chat files mypy+ruff clean.**

- ✅ **G** `chat/controller_factory.py::select_chat_handler` reads `AI_EDITOR_CHAT_CONTROLLER` (default off): on → `ChatController` wrapping transport+model in `DefaultReasoningEngine`; off → legacy `ChatAgent`. `main.py` wired (was direct `ChatAgent`). 3 flag tests green.
- ✅ **H1** `tests/test_controller_invariants.py` — 5 spec-§9 guards (DECIDE-forbids-edit, deterministic tool-def serialization, no `use_shadow_for_reads`, no-batching via spy, `shadow==real` cross-file reject = review #5). ✅ **H2** full suite **757 passed / 3 skipped / 0 failed**; ALL new controller source files mypy+ruff clean (cleared leftover dict type-args + stale `type: ignore`s in sources/inline_apply/edit_session/controller_prompts + test E501/E702).

- ✅ **H2 CODE REVIEW (user asked) — cleared "Ready to merge: Yes", 0 Critical/0 Important.** 3 Minor, all no-fix-needed: max_iters non-int fallback is dead (loop always sets int); invariant-#3 grep is an accepted cheap guard; routes `resolve_mode` `type: ignore` pre-existing+safe. Reviewer confirmed all 4 post-F-review fixes correct (shadow-close on every exit, crash-safe edit w/ consistent session, atomic mode-gate guard, malformed-counter-as-param).

**Backend (Phases A–H) COMPLETE + both review seams cleared.** `npm install` at repo root already run (worktree has node_modules).

- ✅ **I1** (commit `b3f0d1d`): `LiveGateView.kind` += `mode`/`edit` in BOTH `webview-ui/src/types.ts:56` AND `vscode-extension/src/controller.ts:85`; `BackendTaskClient`+`HttpBackendClient` gain `postEditDecision` (plain `POST /edit-decision` body `{decision,reason}`) + `postModeDecision` (streamed `POST /mode-decision` body `{mode}`, consumed like `sendChatMessage`); **fixed `controller.ts:1456` `renderLiveGate`** — was `if (live.pendingGate && live.activeTaskId)` (controller gates have no task → never rendered) → now `if (live.pendingGate)` with `taskId: live.activeTaskId ?? threadId`. editor-client typecheck+build+30 tests + vscode-extension typecheck all GREEN.

- ✅ **I2 COMPLETE (commit `7f73688`):** `chat-panel.ts` ctor params `onModeDecision`/`onEditDecision` (after `onStepDecision`) + `registerHandlers` dispatch for `modeDecision`/`editDecision`; `extension.ts` two lambdas (matching ctor order) delegating to `controller.handleModeDecisionFromChat`/`handleEditDecisionFromChat`; **`webview-ui/src/components/messages/gates/ModeGate.tsx`** (`CardShell icon="bolt"`, plan_sketch + recommended-first option buttons → `{type:"modeDecision",threadId,mode}`; "keep typing to discuss/refine" hint) + **`EditGate.tsx`** (StepGate-shaped per-edit diff review → `{type:"editDecision",threadId,decision,reason:""}`); `LiveSlot.tsx` `GateDispatch` `mode`/`edit` cases; **`webview-ui/src/types.ts` `WebviewMessage` union extended** with `modeDecision`/`editDecision` (was the one real typecheck miss — caught by `webview-ui typecheck`, NOT the IDE noise). Tests: ModeGate (render sketch+recommended, pick posts modeDecision, one-shot) + EditGate (render, accept/reject post editDecision) added to `gates.test.tsx`; `handleEditDecisionFromChat` test + stub `postModeDecision`/`postEditDecision` in `controller.test.ts`. **Gates: vscode-extension typecheck clean + 32 tests; webview-ui typecheck clean + 172 tests (33 gates); dist rebuilt.** NOTE: webview-ui has its OWN node_modules (separate `npm install` in `apps/vscode-extension/webview-ui/` — was missing in this worktree; ran it). Decision tests live in `gates.test.tsx` (shared vscodeApi mock), not a separate `ModeGate.test.tsx` — DRY with the other gate tests.

**Verified frontend anchors (traced this session):** `StepGate.tsx` signature `{taskId, payload}` posts `{type:"stepDecision",taskId,decision}`; `GateDispatch` is in `webview-ui/src/components/LiveSlot.tsx` (NOT messages/), switch by `kind`, keyed remount `${taskId}:${kind}:${sig(payload)}`. ChatPanel handlers are CONSTRUCTOR-INJECTED callbacks delegating to `AiEditorController` methods (extension.ts:20-40 builds them) — NOT inline in chat-panel. `HttpBackendClient.fetchJson(path,{method,body})` prepends baseUrl, checks `.ok`, returns `.json()`. `sendChatMessage`(:365) is the SSE generator template; `sendStepDecision`(:188) the plain-POST template. **All TS IDE diagnostics ("Cannot find name 'Promise/Record'", "Cannot find module './x.js'") are IDE-isolation noise — IGNORE; the real gate is `npm run -w <pkg> typecheck`/`test`/`build`.** CLAUDE.md: after editor-client change, `npm run -w @ai-editor/editor-client build` BEFORE vscode-extension typecheck (already built this session).

**NEXT: J (live smoke — NEEDS THE HUMAN, interactive dev-host).** Frontend (I1+I2) + backend (A–H) all complete & green. J: flip `AI_EDITOR_CHAT_CONTROLLER=1`, launch the dev-host, run scenarios J1–J7 in plan (rebuild webview-ui dist first — already done this session). Then → **K** (delete legacy explore→classify→route at `=0` retirement).
- **F notes (verified this session):** ScriptedReasoningEngine `create_controller_step` CLAMPS index to last response (doesn't pop) — loops calling more times than scripted get the final response repeated. Gate template = `_pause_for_step_review` (engine.py 2058): set `pending_*` on caller's task → `transition()` (tolerate ONLY re-entrant `ValueError`) → `save` → broadcast → create future in `_pending_step_decisions` → `await` → pop in `finally`. Controller analog = `store.set_controller_gate(thread_id, gate|None)` (no task). Chat route `post_chat_message`@1114, `/step-decision`@729 (`resolve_pending_step_review` → `future.set_result`).

**Run/resume commands:** `cd services/agentd-py && source .venv/bin/activate` (venv already built with `pip install -e .[dev]`). Test: `python -m pytest tests/<file> -q`. Full suite at H2.

**Baseline gotchas (so you don't chase ghosts):**
- **mypy:** ~110 PRE-EXISTING errors across 19 provider files; only guard NEW findings — your new files must be mypy-clean.
- **ruff:** E501 (line-length 100) enforced; `engine.py`/`contracts.py` carry PRE-EXISTING E501 + an `InlineChangeResult` F401 — fix only NEW findings in your diff. Per-file-ignores: `tool_prompts.py`, `prompt_builder.py`, `tools/registry.py`, `planning/prompts.py`.
- **Pyright "import could not be resolved"** in the IDE = IDE not pointed at the worktree venv — ignore; pytest runs fine.
- **6 pre-existing test failures:** `test_graph_walker_reachability` (`@requires_live_snapshot`). Everything else green on `main`.

**BROAD/ULTRA CONTEXT — verified anchors (traced from source this session; the expensive part — DO NOT re-derive, just trust/spot-check):**
- **Patch apply:** there is NO `PatchEngine.apply()`. Use `patch/inline_apply.py::apply_ops(engine, base, ops, allowed_files)` → wraps `PatchDocumentV2({candidates:[{candidate_id, patch_ops}]})` → `apply_patch_candidate(base, candidate, allowed_files=)` → `.touched_files`. (`loop._apply_patch_inline` left intact — different error-return contract.)
- **Promote:** `workspace/promote.py::promote_files(shadow, real, touched)` (shadow→real copy). **Diff:** `patch/diffing.py::compute_diff_entries(real, shadow, touched, key)` + `cap_unified_diff`.
- **ScriptedReasoningEngine(plan, patches, *, controller_step_responses=[...])** — positional `plan, patches`.
- **create_task_from_chat(*, thread_id, goal, workspace_path, explore_context, store, step_review_auto_accept=None)** — keyword-only.
- **ChatThread.thread_id** (NOT `.id`); **ChatMessage.role ∈ {"user","agent"}**; `store.create_thread(workspace_path, title)`; `store.append_message(thread_id, ChatMessage)`; `update_title`.
- **Reasoning impl:** `ReasoningEngineImpl` has `self._transport`, `self._model`; `generate_json(model, schema_name, schema, system_instructions, user_payload, on_thinking)`. Orchestrator attrs: `self._patch_engine`, `self._workspace_manager`, `self._broadcaster`/`self.broadcaster`.
- **Mirror PlanningLoop** (`planning/loop.py`): `_assistant_turn` (strip `thought`), dedup guard (`_seen_calls` w/ canonical sorted-args key), `_consecutive_malformed` correction (cap 3), `seed_history` replay; two-builder split (`format_*_system_prompt` = prompt+tools vs `build_*_step_payload` = payload, varying fields LAST); **flat `type`-enum schema, NOT oneOf** (Gemini deadlocks) — gate per phase via deep-copy + enum-trim.
- **Gate model (Class-A, F0/F2/F3):** `resolve_live_state` (chat/live_state.py) derives gates from the active **task** status→`execution_state.pending_*` → `PendingGate(kind∈command/step/scope/validation)`. **Controller has NO task** → add `ChatThread.pending_controller_gate` + `PendingGate.kind += mode/edit` + a `resolve_thread_live` overlay used by `get_thread_live`. `propose_mode` = Class-A (set thread gate + `chat_done`; `/mode-decision` clears + dispatches via a NEW streamed turn); per-edit gate = **hold the SSE stream open** + in-memory future (mirror `_pause_for_step_review` + `_pending_step_decisions`), set the `edit` thread-gate while awaiting.
- **Chat route:** `post_chat_message` (routes.py ~1113), `channel_id=f"chat:{thread_id}"`, SSE held until `chat_done` (15s ping keepalive), agent task started inside the stream generator. Step-decision route pattern at routes.py:729 (`future.set_result`).
- **Frontend:** gates render in `LiveSlot.tsx::GateDispatch` by `kind` (add `mode`/`edit` cases → `ModeGate`/`EditGate`). `LiveGateView.kind` is declared in **BOTH** `vscode-extension/src/controller.ts:85` AND `webview-ui/src/types.ts:56` — extend both. Decision posts handled in **`chat-panel.ts::registerHandlers`** (`m["type"]` if-else, `stepDecision` at :134) → add `modeDecision`/`editDecision`. **`controller.ts:1456`** `renderLiveGate` is gated on `live.activeTaskId` (null on controller turns) → relax to render on `pendingGate` with `taskId: live.activeTaskId ?? threadId`. **`Icon` has NO `"lightbulb"`** (valid: …`diff`,`bolt`,`spark`,`warn`,`check`,`x`…). `HttpBackendClient` ctor = `{ baseUrl, fetchFn? }` (options object). `StreamEvent` is a plain TS discriminated union (not Zod). `CardShell` props: `icon`(req)/`title`/`subtitle?`/`borderColor?`/`headerTint?`; `BtnPrimary` has `flex?`, `BtnGhost` does not. Webview vitest: jsdom + `src/test/setup.ts`.
- **Spec:** `docs/superpowers/specs/2026-06-15-agentic-chat-controller-design.md` (esp. §4 schema, §6 seed+delta cache, §12 mirror/DRY/patterns). Memory: `project_agentic_chat_controller.md`, `feedback_trace_before_asserting.md`.

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
        "plan_sketch": {"type": "string"},   # lightweight "here's my approach" preview (NOT the concrete plan)
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
    _phase_hint = ("You are in EDIT mode: emit type='edit' (patch_ops) to make changes, "
                   "then type='submit_changes' when done. Do NOT propose_mode again."
                   if phase == "EDIT" else
                   "Explore with tools, then answer, clarify, or propose_mode.")
    payload["instruction"] = (
        f"Phase={phase}. {_phase_hint} You have used {iteration} of {max_iters} steps. "
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
    eng = ScriptedReasoningEngine(None, [], controller_step_responses=[{"type": "answer", "thought": "t", "answer": "hi"}])
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

> **Verified against source (2026-06-15):** `PatchEngine` has **no** `apply()` — patches go `_wrap_as_patch_document(ops)` → `PatchDocumentV2.model_validate(dict)` → `apply_patch_candidate(base_dir, candidate, allowed_files=) -> PatchResult(.touched_files)` (see `tools/loop.py::_apply_patch_inline` 1141-1175, `patch/engine.py::apply_patch_document` 313). There is **no** `ShadowWorkspaceManager.promote_files`; the scoped shadow→real copy is `AgentOrchestrator._partial_promote(shadow, real, touched)` (engine.py 2153). Diff is `AgentOrchestrator._compute_diff_entries(real, shadow, touched, task_id) -> list[DiffEntry]` (engine.py 1147). D0 extracts these into free functions so ToolLoop, `_partial_promote`, and `TurnEditSession` share one implementation (DRY).

### Task D0: Extract shared patch/promote/diff primitives (DRY)

**Files:**
- Create: `agentd/patch/inline_apply.py` — `apply_ops(patch_engine, base_dir, patch_ops, allowed_files) -> list[str]` (touched files).
- Create: `agentd/patch/diffing.py` — `compute_diff_entries(real_path, shadow_path, touched, key) -> list[DiffEntry]`.
- Create: `agentd/workspace/promote.py` — `promote_files(shadow_path, real_path, touched) -> None`.
- Modify: `agentd/tools/loop.py` (`_apply_patch_inline` delegates to `apply_ops`), `agentd/orchestrator/engine.py` (`_compute_diff_entries`/`_partial_promote` delegate to the new free functions).
- Test: `tests/test_inline_apply_primitives.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_inline_apply_primitives.py
import pytest
from pathlib import Path
from agentd.patch.engine import PatchEngine
from agentd.patch.inline_apply import apply_ops
from agentd.patch.diffing import compute_diff_entries
from agentd.workspace.promote import promote_files

@pytest.mark.asyncio
async def test_apply_ops_diff_and_promote(tmp_path: Path):
    real = tmp_path / "ws"; real.mkdir(); (real / "f.py").write_text("x = 1\n")
    shadow = tmp_path / "sh"; shadow.mkdir(); (shadow / "f.py").write_text("x = 1\n")
    touched = await apply_ops(
        PatchEngine(), shadow,
        [{"op": "search_replace", "file": "f.py", "search": "x = 1", "replace": "x = 2", "reason": "r"}],
        allowed_files={"f.py"},
    )
    assert touched == ["f.py"]
    entries = compute_diff_entries(real, shadow, touched, "k1")
    assert entries[0].path == "f.py" and entries[0].additions >= 1
    promote_files(shadow, real, touched)
    assert (real / "f.py").read_text() == "x = 2\n"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_inline_apply_primitives.py -v`
Expected: FAIL — modules missing.

- [ ] **Step 3: Write minimal implementation**

```python
# agentd/patch/inline_apply.py
from __future__ import annotations
from pathlib import Path
from agentd.domain.models import PatchDocumentV2
from agentd.patch.engine import PatchEngine

async def apply_ops(patch_engine: PatchEngine, base_dir: Path,
                    patch_ops: list[dict], allowed_files: set[str]) -> list[str]:
    """Apply raw patch_ops to base_dir via the candidate path. Returns touched rel paths.
    Single source of truth shared by ToolLoop and TurnEditSession."""
    doc = PatchDocumentV2.model_validate(
        {"candidates": [{"candidate_id": "inline-c1", "patch_ops": patch_ops}]}
    )
    candidate = doc.candidates[0]
    result = await patch_engine.apply_patch_candidate(base_dir, candidate, allowed_files=allowed_files)
    return list(result.touched_files)
```

```python
# agentd/patch/diffing.py
from __future__ import annotations
import difflib
from pathlib import Path
from agentd.domain.models import DiffEntry
from agentd.orchestrator.engine import _cap_unified_diff  # already module-level (engine.py:84)

def compute_diff_entries(real_path: Path, shadow_path: Path, touched: list[str], key: str) -> list[DiffEntry]:
    """Free-function form of AgentOrchestrator._compute_diff_entries (engine.py:1147)."""
    entries: list[DiffEntry] = []
    for rel in touched:
        shadow_file = shadow_path / rel
        real_file = real_path / rel
        if not shadow_file.exists():
            continue
        shadow_lines = shadow_file.read_text(errors="replace").splitlines(keepends=True)
        real_lines = real_file.read_text(errors="replace").splitlines(keepends=True) if real_file.exists() else []
        diff = list(difflib.unified_diff(real_lines, shadow_lines, lineterm=""))
        additions = sum(1 for ln in diff if ln.startswith("+") and not ln.startswith("+++"))
        deletions = sum(1 for ln in diff if ln.startswith("-") and not ln.startswith("---"))
        entries.append(DiffEntry(path=rel, additions=additions, deletions=deletions,
                                 temp_path=str(shadow_file), unified_diff=_cap_unified_diff("\n".join(diff))))
    return entries
```

```python
# agentd/workspace/promote.py
from __future__ import annotations
import shutil
from pathlib import Path

def promote_files(shadow_path: Path, real_path: Path, touched: list[str]) -> None:
    """Scoped shadow→real copy. Free-function form of _partial_promote (engine.py:2153)."""
    for rel in touched:
        src = shadow_path / rel
        dst = real_path / rel
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
```

Then make the existing code delegate (DRY): in `tools/loop.py::_apply_patch_inline`, replace the inline body with `touched = await apply_ops(self._patch_engine, self._shadow_path, patch_ops, {t.path for t in step.targets})` (keep the scope-error handling around it); in `engine.py`, `_compute_diff_entries` and `_partial_promote` call the new free functions. Run the existing patch/inline tests to confirm no regression.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_inline_apply_primitives.py tests/test_inline_change.py -v` (the second guards no regression in the existing inline path)
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentd/patch/inline_apply.py agentd/patch/diffing.py agentd/workspace/promote.py agentd/tools/loop.py agentd/orchestrator/engine.py tests/test_inline_apply_primitives.py
git commit -m "refactor(patch): extract apply_ops/compute_diff_entries/promote_files (DRY)"
```

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
    diff = await sess.apply([{"op": "search_replace", "file": "f.py",
                              "search": "x = 1", "replace": "x = 2", "reason": "r"}])
    assert any(e.path == "f.py" for e in diff)
    await sess.accept()
    assert (real / "f.py").read_text() == "x = 2\n"   # instant-promoted to real
    # reject leaves real untouched (patch was applied to shadow only, not yet promoted)
    await sess.apply([{"op": "search_replace", "file": "f.py",
                       "search": "x = 2", "replace": "x = 999", "reason": "r"}])
    await sess.reject()
    assert (real / "f.py").read_text() == "x = 2\n"
    await sess.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_turn_edit_session.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

```python
# agentd/chat/edit_session.py
from __future__ import annotations
import shutil
from pathlib import Path
from agentd.domain.models import DiffEntry
from agentd.patch.engine import PatchEngine
from agentd.patch.inline_apply import apply_ops
from agentd.patch.diffing import compute_diff_entries
from agentd.workspace.promote import promote_files
from agentd.workspace.shadow import ShadowWorkspaceManager

class TurnEditSession:
    """One ACID shadow per turn. apply() patches the shadow (real is the clean before, since
    every prior patch was promoted-or-reverted); accept() promotes touched files to real
    instantly; reject() restores the shadow's touched files from real so shadow==real holds."""

    def __init__(self, *, turn_id: str, real_path: Path,
                 workspace_manager: ShadowWorkspaceManager, patch_engine: PatchEngine):
        self._turn_id = turn_id
        self._real = real_path
        self._wm = workspace_manager
        self._patch = patch_engine
        self._shadow: Path | None = None
        self._touched_ever: set[str] = set()      # all files the shadow has ever held this turn
        self._pending_touched: list[str] = []

    async def _ensure_shadow(self, touched: list[str]) -> Path:
        if self._shadow is None:
            sw = await self._wm.prepare_lightweight(
                f"chatturn-{self._turn_id}", str(self._real), touched)
            self._shadow = Path(sw.shadow_path)
        else:
            # seed any newly-touched file into the lightweight shadow from real
            for rel in touched:
                if rel not in self._touched_ever and (self._real / rel).exists():
                    dst = self._shadow / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(self._real / rel, dst)
        self._touched_ever.update(touched)
        return self._shadow

    async def apply(self, patch_ops: list[dict]) -> list[DiffEntry]:
        touched = [str(op["file"]) for op in patch_ops if "file" in op]
        shadow = await self._ensure_shadow(touched)
        applied = await apply_ops(self._patch, shadow, patch_ops, allowed_files=set(touched))
        self._pending_touched = applied
        return compute_diff_entries(self._real, shadow, applied, self._turn_id)

    async def accept(self) -> None:
        assert self._shadow is not None
        promote_files(self._shadow, self._real, self._pending_touched)
        self._pending_touched = []

    async def reject(self) -> None:
        assert self._shadow is not None
        for rel in self._pending_touched:
            real_f, shadow_f = self._real / rel, self._shadow / rel
            if real_f.exists():
                shadow_f.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(real_f, shadow_f)     # modified/deleted → restore from real
            elif shadow_f.exists():
                shadow_f.unlink()                  # created → drop
        self._pending_touched = []

    async def close(self) -> None:
        if self._shadow is not None:
            shutil.rmtree(self._shadow, ignore_errors=True)
            self._shadow = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_turn_edit_session.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentd/chat/edit_session.py tests/test_turn_edit_session.py
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
    eng = ScriptedReasoningEngine(None, [], controller_step_responses=[
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
    return ControllerLoop(ScriptedReasoningEngine(None, [], controller_step_responses=steps), reg,
                          EventBroadcaster(), channel_id="c", phase_sm=ControllerPhaseSM())

@pytest.mark.asyncio
async def test_clarify_terminal(tmp_path: Path):
    out = await _loop(tmp_path, [{"type": "clarify", "thought": "t", "question": "which file?"}]).run(
        {"goal": "g", "workspace_path": str(tmp_path)}, max_iters=4)
    assert out.kind == "clarify" and out.text == "which file?"

@pytest.mark.asyncio
async def test_propose_mode_terminal_carries_payload(tmp_path: Path):
    out = await _loop(tmp_path, [{"type": "propose_mode", "thought": "t", "recommended": "create_task",
                                  "plan_sketch": "add a decorator and apply to 3 routes",
                                  "reason": "big", "options": [{"mode": "create_task"}]}]).run(
        {"goal": "g", "workspace_path": str(tmp_path)}, max_iters=4)
    assert out.kind == "propose_mode" and out.payload["recommended"] == "create_task"
    assert out.payload["plan_sketch"] == "add a decorator and apply to 3 routes"
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
                    "plan_sketch": resp.get("plan_sketch", ""),
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
    loop = ControllerLoop(ScriptedReasoningEngine(None, [], controller_step_responses=[
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
    loop = ControllerLoop(ScriptedReasoningEngine(None, [], controller_step_responses=[
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

> **Verified architecture (2026-06-15):** interactive gates are **Class-A**: a durable `PendingGate` exposed via `/live` (polled), rendered by `LiveSlot.GateDispatch` by `kind`; resolution leaves a breadcrumb. Task gates pair `execution_state.pending_*` (for `/live`) with an in-memory future (to resume). The controller has **no task**, so its gates live at the **thread** level: `ChatThread.pending_controller_gate` (durable, for `/live`) + an in-memory future for the held-open edit gate. F0 builds that plumbing; F2/F3 use it. Webview decision posts are handled in `chat-panel.ts:134` (where `stepDecision` is handled).

### Task F0: Thread-level controller gates via `/live`

**Files:**
- Modify: `agentd/chat/models.py` — `PendingGate.kind` += `"mode"`,`"edit"`; `ChatThread` += `pending_controller_gate: PendingGate | None = None`.
- Modify: `agentd/chat/storage.py` — persist/restore `pending_controller_gate`; add `set_controller_gate(thread_id, gate|None)`.
- Modify: `agentd/api/routes.py` — `get_thread_live` overlays the thread gate.
- Test: `tests/test_controller_live_gate.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_controller_live_gate.py
import pytest
from pathlib import Path
from agentd.chat.storage import ChatThreadStore
from agentd.chat.models import PendingGate
from agentd.chat.live_state import resolve_thread_live   # new wrapper (Step 3)

def test_controller_gate_overlays_live(tmp_path: Path):
    store = ChatThreadStore(tmp_path / "c.sqlite3"); th = store.create_thread(str(tmp_path), title="t")
    store.set_controller_gate(th.thread_id, PendingGate(kind="mode", payload={"plan_sketch": "x"}))
    th2 = store.get_thread(th.thread_id)
    live = resolve_thread_live(th2, active_task_id=None, get_task=lambda _id: (_ for _ in ()).throw(KeyError()))
    assert live.pending_gate is not None and live.pending_gate.kind == "mode"
    store.set_controller_gate(th.thread_id, None)
    assert store.get_thread(th.thread_id).pending_controller_gate is None
```

- [ ] **Step 2: Run** `pytest tests/test_controller_live_gate.py -v` → FAIL (`kind` enum rejects "mode"; `pending_controller_gate`/`set_controller_gate`/`resolve_thread_live` missing).

- [ ] **Step 3: Write minimal implementation**
- `models.py`: `kind: Literal["command","step","scope","validation","mode","edit"]`; add `pending_controller_gate: PendingGate | None = None` to `ChatThread`.
- `storage.py`: include the field in the row (de)serialization; `set_controller_gate(thread_id, gate)` updates it in place (mirrors `set_active_task`).
- `live_state.py`: add a thin wrapper that prefers the thread gate:
```python
def resolve_thread_live(thread, active_task_id, get_task):
    if thread is not None and thread.pending_controller_gate is not None:
        return ThreadLiveState(active_task_id=active_task_id,
                               pending_gate=thread.pending_controller_gate)
    return resolve_live_state(active_task_id, get_task)
```
- `routes.py::get_thread_live`: call `resolve_thread_live(thread, active_id if task else None, _get)` instead of `resolve_live_state(...)`.

- [ ] **Step 4: Run** → PASS.

- [ ] **Step 5: Commit**

```bash
git add agentd/chat/models.py agentd/chat/storage.py agentd/chat/live_state.py agentd/api/routes.py tests/test_controller_live_gate.py
git commit -m "feat(chat): thread-level controller gates (mode/edit) exposed via /live"
```

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
    thread = store.create_thread(str(tmp_path), title="t")   # create_thread(workspace_path, title)
    ctrl = ChatController(workspace_path=str(tmp_path),
                          reasoning_engine=ScriptedReasoningEngine(None, [], controller_step_responses=[
                              {"type": "answer", "thought": "t", "answer": "hello"}]),
                          thread_store=store, orchestrator=None, broadcaster=EventBroadcaster(),
                          retrieval_client=None)
    await ctrl.handle_message(thread.thread_id, "hi", channel_id="c1")   # ChatThread.thread_id
    msgs = store.get_thread(thread.thread_id).messages
    assert any(m.role == "agent" and "hello" in m.content for m in msgs)   # role ∈ {"user","agent"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_chat_controller_qa.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

```python
# agentd/chat/controller.py
from __future__ import annotations
import asyncio, logging
from pathlib import Path
from agentd.chat.controller_loop import ControllerLoop, ControllerOutcome
from agentd.chat.controller_phase import ControllerPhaseSM
from agentd.chat.edit_session import TurnEditSession
from agentd.chat.models import ChatMessage
from agentd.tools.sources import AggregatingToolRegistry, BuiltinToolSource

logger = logging.getLogger(__name__)

class ChatController:
    """Dynamic agentic chat handler (flag-selected vs ChatAgent). Mirrors ChatAgent's
    public surface: handle_message(thread_id, message, channel_id, step_review=None),
    plus _store / _broadcaster attrs the route reads."""

    def __init__(self, *, workspace_path, reasoning_engine, thread_store, orchestrator,
                 broadcaster, retrieval_client=None):
        self._workspace_path = workspace_path
        self._reasoning = reasoning_engine
        self._store = thread_store
        self._orchestrator = orchestrator
        self._broadcaster = broadcaster
        self._retrieval = retrieval_client
        # In-memory per-thread state (mirrors the in-memory _pending_* gate maps).
        self._histories: dict[str, list[dict]] = {}              # controller conversation history
        self._pending_mode: dict[str, asyncio.Future[str]] = {}  # thread_id → mode future (F2)
        self._pending_edit: dict[str, asyncio.Future[dict]] = {} # thread_id → edit decision (F3)

    def _build_registry(self):
        return AggregatingToolRegistry([BuiltinToolSource(
            shadow_root=Path(self._workspace_path), real_workspace_path=Path(self._workspace_path),
            semantic_index=getattr(self._retrieval, "_semantic_index", None))])

    def _retrieval_seed(self) -> dict | None:
        if self._retrieval is None:
            return None
        try:
            return self._retrieval.load_context(self._workspace_path)[0].as_prompt_payload()
        except Exception:
            logger.debug("[controller] retrieval seed failed", exc_info=True); return None

    async def handle_message(self, thread_id, message, channel_id, step_review=None):
        thread = self._store.get_thread(thread_id)
        if thread is None:
            raise ValueError(f"Thread {thread_id!r} not found")
        if not any(m.role == "user" for m in thread.messages):
            self._store.update_title(thread_id, message.strip().replace("\n", " ")[:50])
            self._broadcaster.broadcast(channel_id, {"type": "thread_title_updated",
                "payload": {"thread_id": thread_id, "title": message.strip()[:50]}})
        self._store.append_message(thread_id, ChatMessage(role="user", content=message))

        seed = self._histories.get(thread_id, [])
        user_turn = [{"role": "user", "content": message}] if seed else None
        outcome = await self._run_loop(thread_id, channel_id, message,
                                       seed_history=(seed + user_turn) if user_turn else None,
                                       step_review=step_review)
        await self._finish(thread_id, channel_id, outcome, step_review)

    async def _run_loop(self, thread_id, channel_id, goal, *, seed_history, step_review, phase=None):
        sm = ControllerPhaseSM()
        if phase == "EDIT":
            sm.enter_edit_mode()
        edit = TurnEditSession(turn_id=thread_id, real_path=Path(self._workspace_path),
                               workspace_manager=self._orchestrator._workspace_manager,
                               patch_engine=self._orchestrator._patch_engine) if self._orchestrator else None
        loop = ControllerLoop(self._reasoning, self._build_registry(), self._broadcaster,
                              channel_id=channel_id, phase_sm=sm, edit_session=edit)
        plan_context = {"goal": goal, "workspace_path": self._workspace_path}
        s = self._retrieval_seed()
        if s:
            plan_context["retrieval_seed"] = s
        outcome = await loop.run(plan_context, seed_history=seed_history,
                                 auto_accept_edits=(step_review is not True))
        self._histories[thread_id] = outcome.history or []
        return outcome

    async def _finish(self, thread_id, channel_id, outcome: ControllerOutcome, step_review):
        if outcome.kind in ("answer", "clarify"):
            self._store.append_message(thread_id, ChatMessage(role="agent", content=outcome.text))
            self._broadcaster.broadcast(channel_id, {"type": "chat_response", "payload": {"chunk": outcome.text}})
            self._broadcaster.broadcast(channel_id, {"type": "chat_done", "payload": {}})
        elif outcome.kind == "submit_changes":
            self._broadcaster.broadcast(channel_id, {"type": "chat_done", "payload": {}})
        elif outcome.kind == "propose_mode":
            await self._present_mode_choice(thread_id, channel_id, outcome)  # F2
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_chat_controller_qa.py -v`
Expected: PASS. (`_present_mode_choice` is added in F2; QA/clarify paths pass now. If the import-time reference to an undefined method trips, stub `_present_mode_choice` to `pass` here and flesh it out in F2.)

- [ ] **Step 5: Commit**

```bash
git add agentd/chat/controller.py tests/test_chat_controller_qa.py
git commit -m "feat(chat): ChatController handle_message (QA + clarify)"
```

### Task F2: `propose_mode` gate + `/mode-decision` route → dispatch

**Files:**
- Modify: `agentd/chat/controller.py`, `agentd/api/routes.py`
- Test: `tests/test_mode_decision.py`

**Design note (verified against the route):** the chat turn is one SSE stream ending at `chat_done` (`post_chat_message`, routes.py:1113), and "discuss" is a *new* `POST /message`. So `propose_mode` is **Class-A**: emit the `mode_choice` card + `chat_done` (turn ends), persist a durable card, and store the loop history. `/mode-decision` then starts a **new streamed turn** (edit→loop in EDIT phase; create_task/resume→handoff). "Discuss" is just the next `handle_message`, which already seeds from `self._histories[thread_id]` (F1). This differs from the per-edit gate (F3), which *holds the stream open* and awaits a future.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mode_decision.py
import pytest
from pathlib import Path
from agentd.chat.controller import ChatController
from agentd.chat.storage import ChatThreadStore
from agentd.orchestrator.broadcaster import EventBroadcaster
from agentd.orchestrator.scripted_engine import ScriptedReasoningEngine

class _Orch:
    def __init__(self): self.created = None
    async def create_task_from_chat(self, **kw): self.created = kw; return "task-xyz"
    async def await_plan_ready(self, tid, timeout_sec=3600.0): return None

@pytest.mark.asyncio
async def test_propose_mode_emits_card_and_stores_history(tmp_path: Path):
    store = ChatThreadStore(tmp_path / "c.sqlite3"); th = store.create_thread(str(tmp_path), title="t")
    events = []
    bc = EventBroadcaster(); 
    eng = ScriptedReasoningEngine(None, [], controller_step_responses=[
        {"type": "propose_mode", "thought": "t", "plan_sketch": "add decorator",
         "recommended": "create_task", "reason": "big", "options": [{"mode": "create_task"}]}])
    ctrl = ChatController(workspace_path=str(tmp_path), reasoning_engine=eng, thread_store=store,
                          orchestrator=_Orch(), broadcaster=bc, retrieval_client=None)
    await ctrl.handle_message(th.thread_id, "do a big thing", channel_id=f"chat:{th.thread_id}")
    # Class-A: a durable thread gate is set (rendered by /live), NOT an SSE mode event.
    gate = store.get_thread(th.thread_id).pending_controller_gate
    assert gate is not None and gate.kind == "mode" and gate.payload["plan_sketch"] == "add decorator"
    assert ctrl._histories[th.thread_id]                       # history stored for resume/discuss

@pytest.mark.asyncio
async def test_mode_decision_create_task_dispatches(tmp_path: Path):
    store = ChatThreadStore(tmp_path / "c.sqlite3"); th = store.create_thread(str(tmp_path), title="t")
    orch = _Orch()
    ctrl = ChatController(workspace_path=str(tmp_path), reasoning_engine=ScriptedReasoningEngine(None, []),
                          thread_store=store, orchestrator=orch, broadcaster=EventBroadcaster(), retrieval_client=None)
    ctrl._histories[th.thread_id] = [{"role": "assistant", "content": "{}"}]
    await ctrl.resolve_mode(th.thread_id, "create_task", channel_id=f"chat:{th.thread_id}", goal="g")
    assert orch.created is not None and orch.created["goal"] == "g"
```

- [ ] **Step 2: Run test to verify it fails** — `pytest tests/test_mode_decision.py -v` → FAIL (methods/route missing).

- [ ] **Step 3: Write minimal implementation**

Add to `ChatController`:
```python
    async def _present_mode_choice(self, thread_id, channel_id, outcome):
        from agentd.chat.models import PendingGate
        # Durable Class-A gate → /live renders it via LiveSlot (survives reload). No SSE
        # mode event: chat-side gates render purely from the /live poll (CLAUDE.md).
        self._store.set_controller_gate(thread_id, PendingGate(kind="mode", payload=outcome.payload or {}))
        self._broadcaster.broadcast(channel_id, {"type": "chat_done", "payload": {}})  # end the message stream

    async def resolve_mode(self, thread_id, mode, *, channel_id, goal):
        """Called by POST /mode-decision. Clears the live gate, then dispatches."""
        self._store.set_controller_gate(thread_id, None)
        self._broadcaster.broadcast(channel_id, {"type": "chat_breadcrumb",
            "payload": {"text": f"▸ Proceeding: {mode}", "task_id": ""}})
        if mode in ("edit", "explain"):
            phase = "EDIT" if mode == "edit" else None
            outcome = await self._run_loop(thread_id, channel_id, goal,
                                           seed_history=self._histories.get(thread_id), step_review=False, phase=phase)
            await self._finish(thread_id, channel_id, outcome, step_review=False)
        elif mode == "create_task":
            task_id = await self._orchestrator.create_task_from_chat(
                thread_id=thread_id, goal=goal, workspace_path=self._workspace_path,
                explore_context=[], store=self._store)
            self._store.append_message(thread_id, ChatMessage(role="agent", content=task_id,
                                       type="task_card", metadata={"taskId": task_id}))
            self._broadcaster.broadcast(channel_id, {"type": "task_card", "payload": {"task_id": task_id}})
            await self._orchestrator.await_plan_ready(task_id)
        elif mode == "resume":
            child = await self._orchestrator.resume_from_execute(self._histories.get("_recent_task_id"),
                                                                 chat_channel_id=channel_id)
            self._broadcaster.broadcast(channel_id, {"type": "task_card", "payload": {"task_id": child}})
        self._broadcaster.broadcast(channel_id, {"type": "chat_done", "payload": {}})
```

Add the route in `routes.py` (inside the `chat_agent is not None` block, mirroring `/step-decision` 729-761 but chat-scoped). The handler must run the dispatch as a **streamed background task** like `post_chat_message`, because `edit`/`create_task` produce live events:
```python
        @router.post("/chat/threads/{thread_id}/mode-decision")
        async def post_mode_decision(thread_id: str, request: dict) -> StreamingResponse:
            import asyncio as _a, json as _j
            mode = request.get("mode", ""); goal = request.get("goal", "")
            channel_id = f"chat:{thread_id}"
            _chat_agent._broadcaster.clear_replay(channel_id)
            queue = _chat_agent._broadcaster.subscribe(channel_id)
            async def _run():
                try:
                    await _chat_agent.resolve_mode(thread_id, mode, channel_id=channel_id, goal=goal)
                except Exception:
                    logging.getLogger(__name__).exception("resolve_mode failed")
                    _chat_agent._broadcaster.broadcast(channel_id, {"type": "chat_done", "payload": {}})
            async def gen():
                t = _a.create_task(_run())
                try:
                    while True:
                        try: ev = await _a.wait_for(queue.get(), timeout=15.0)
                        except _a.TimeoutError: yield ": ping\n\n"; continue
                        yield f"data: {_j.dumps(ev)}\n\n"
                        if ev.get("type") in ("chat_done", "done"): break
                finally:
                    _chat_agent._broadcaster.unsubscribe(channel_id, queue); t.cancel()
            return StreamingResponse(gen(), media_type="text/event-stream")
```
(`goal` is the original user message; the frontend posts it with the decision, or the controller reads it from the last user message in the thread — prefer the latter to avoid trusting the client: `goal = next(m.content for m in reversed(thread.messages) if m.role=="user")`.)

- [ ] **Step 4: Run test to verify it passes** — `pytest tests/test_mode_decision.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add agentd/chat/controller.py agentd/api/routes.py tests/test_mode_decision.py
git commit -m "feat(chat): propose_mode Class-A card + /mode-decision streamed dispatch"
```

### Task F3: Per-edit review gate + `/edit-decision` (reuses step_review_auto_accept)

**Files:**
- Modify: `agentd/chat/controller.py`, `agentd/chat/controller_loop.py`, `agentd/api/routes.py`
- Test: `tests/test_edit_decision.py`

Unlike `propose_mode` (Class-A, turn ends), the per-edit gate **holds the stream open** and awaits a future — exactly like `_pause_for_step_review`. The loop calls an injected async `edit_decision_cb()` after `apply()` when not auto-accepting.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_edit_decision.py
import pytest, asyncio
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
async def test_reject_leaves_real_untouched_then_accept_promotes(tmp_path: Path):
    real = tmp_path / "ws"; real.mkdir(); (real / "f.py").write_text("x = 1\n")
    sm = ControllerPhaseSM(); sm.enter_edit_mode()
    sess = TurnEditSession(turn_id="t1", real_path=real,
                           workspace_manager=ShadowWorkspaceManager(tmp_path / "sh"), patch_engine=PatchEngine())
    decisions = iter([{"decision": "reject", "reason": "wrong var"},
                      {"decision": "accept"}])
    async def edit_cb(): return next(decisions)
    reg = AggregatingToolRegistry([BuiltinToolSource(shadow_root=real, real_workspace_path=real)])
    loop = ControllerLoop(ScriptedReasoningEngine(None, [], controller_step_responses=[
        {"type": "edit", "thought": "t", "patch_ops": [
            {"op": "search_replace", "file": "f.py", "search": "x = 1", "replace": "x = 9", "reason": "r"}]},
        {"type": "edit", "thought": "fix", "patch_ops": [
            {"op": "search_replace", "file": "f.py", "search": "x = 1", "replace": "x = 2", "reason": "r"}]},
        {"type": "submit_changes", "thought": "done", "summary": "s"},
    ]), reg, EventBroadcaster(), channel_id="c", phase_sm=sm, edit_session=sess)
    out = await loop.run({"goal": "g", "workspace_path": str(real)}, max_iters=8,
                         auto_accept_edits=False, edit_decision_cb=edit_cb)
    assert out.kind == "submit_changes"
    assert (real / "f.py").read_text() == "x = 2\n"   # first rejected (real untouched), second accepted
```

- [ ] **Step 2: Run test to verify it fails** — `pytest tests/test_edit_decision.py -v` → FAIL (`edit_decision_cb` not a param).

- [ ] **Step 3: Write minimal implementation**

In `ControllerLoop.run`, add `edit_decision_cb=None`, and rewrite the `edit` branch (replacing the F1-era "treat as accept" stub):
```python
            if atype == "edit":
                ops = resp.get("patch_ops") or []
                diff = await self._edit.apply(ops)
                self._broadcaster.broadcast(self._channel_id,
                    {"type": "diff_ready", "payload": {"diff_entries": [
                        {"path": d.path, "additions": d.additions, "deletions": d.deletions,
                         "unified_diff": d.unified_diff} for d in diff]}})
                if auto_accept_edits or edit_decision_cb is None:
                    await self._edit.accept(); decision = {"decision": "accept"}
                else:
                    decision = await edit_decision_cb(diff)     # holds the turn open; renders via /live
                    if decision.get("decision") == "accept":
                        await self._edit.accept()
                    else:
                        await self._edit.reject()
                history.append(assistant_turn(resp))
                if decision.get("decision") == "accept":
                    history.append({"role": "tool_result", "tool": "edit",
                                    "content": f"applied+promoted: {[d.path for d in diff]}"})
                else:
                    history.append({"role": "tool_result", "tool": "edit",
                                    "content": f"REJECTED by user: {decision.get('reason','')}. Revise and re-emit."})
                continue
```

In `ChatController`, provide the cb + route resolution:
```python
    async def _edit_decision_cb(self, thread_id, channel_id, diff):
        from agentd.chat.models import PendingGate
        self._store.set_controller_gate(thread_id, PendingGate(kind="edit", payload={
            "diff_entries": [{"path": d.path, "additions": d.additions, "deletions": d.deletions,
                              "unified_diff": d.unified_diff} for d in diff]}))   # /live renders it
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_edit[thread_id] = fut
        try:
            return await fut
        finally:
            self._pending_edit.pop(thread_id, None)
            self._store.set_controller_gate(thread_id, None)

    async def resolve_edit(self, thread_id, decision: dict) -> bool:
        fut = self._pending_edit.get(thread_id)
        if fut is None or fut.done():
            return False
        fut.set_result(decision); return True
```
Wire `_run_loop` to pass `edit_decision_cb=lambda diff: self._edit_decision_cb(thread_id, channel_id, diff)` when `step_review is True`. Add a `POST /chat/threads/{id}/edit-decision {decision, reason?}` route (mirror `/step-decision` 729-761) that calls `_chat_agent.resolve_edit(thread_id, request)` and returns `{ok: True}` (the held-open message stream surfaces the continuation).

**Hardening (client-disconnect):** if the SSE client drops while `_edit_decision_cb` awaits, the future never resolves and the turn hangs. Wrap the await with `AI_EDITOR_CHAT_EDIT_DECISION_TIMEOUT_SEC` (default `0` = wait forever, matching `AI_EDITOR_COMMAND_DECISION_TIMEOUT_SEC`); on timeout return `{"decision": "reject", "reason": "decision timed out"}` so the loop unwinds cleanly. Add a test that a timeout rejects the edit.

- [ ] **Step 4: Run test to verify it passes** — `pytest tests/test_edit_decision.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add agentd/chat/controller.py agentd/chat/controller_loop.py agentd/api/routes.py tests/test_edit_decision.py
git commit -m "feat(chat): per-edit held-stream gate + /edit-decision (mirrors step-review)"
```

### Task F4: Clarify resume (mirror planning feedback) + retrieval delta on edit

**Files:**
- Modify: `agentd/chat/controller.py`
- Test: `tests/test_clarify_resume_and_retrieval_delta.py`

Clarify-resume is already delivered by F1 (`self._histories[thread_id]` + the `user_turn` seed in `handle_message`). This task adds (a) a regression test pinning that behavior, and (b) the **append-only retrieval delta** after an accepted edit.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_clarify_resume_and_retrieval_delta.py
import pytest
from pathlib import Path
from agentd.chat.controller import ChatController
from agentd.chat.storage import ChatThreadStore
from agentd.orchestrator.broadcaster import EventBroadcaster
from agentd.orchestrator.scripted_engine import ScriptedReasoningEngine

class _RecordingEngine(ScriptedReasoningEngine):
    def __init__(self, responses): super().__init__(None, [], controller_step_responses=responses); self.seen_histories = []
    async def create_controller_step(self, plan_context, history, tool_definitions, *, phase, on_thinking=None):
        self.seen_histories.append(list(history)); return await super().create_controller_step(
            plan_context, history, tool_definitions, phase=phase, on_thinking=on_thinking)

@pytest.mark.asyncio
async def test_clarify_then_resume_sees_prior_history(tmp_path: Path):
    store = ChatThreadStore(tmp_path / "c.sqlite3"); th = store.create_thread(str(tmp_path), title="t")
    eng = _RecordingEngine([
        {"type": "clarify", "thought": "t", "question": "which file?"},   # turn 1
        {"type": "answer", "thought": "t", "answer": "ok, foo.py"},         # turn 2 (resume)
    ])
    ctrl = ChatController(workspace_path=str(tmp_path), reasoning_engine=eng, thread_store=store,
                          orchestrator=None, broadcaster=EventBroadcaster(), retrieval_client=None)
    await ctrl.handle_message(th.thread_id, "change the thing", channel_id=f"chat:{th.thread_id}")
    await ctrl.handle_message(th.thread_id, "the foo one", channel_id=f"chat:{th.thread_id}")
    # turn 2's first step must have seen turn 1's history (clarify resume = feedback resume)
    assert any(h for h in eng.seen_histories[1:]), "resume must seed prior history"
    assert any(m.content == "ok, foo.py" for m in store.get_thread(th.thread_id).messages)
```

- [ ] **Step 2: Run test to verify it fails** — run `pytest tests/test_clarify_resume_and_retrieval_delta.py -v`. If clarify-resume already passes from F1, this confirms it; add the retrieval-delta assertion below and watch *that* fail first.

- [ ] **Step 3: Write minimal implementation** — add an optional `retrieval_delta_cb(touched: list[str]) -> str | None` to `ControllerLoop.run`; in the `edit` branch, **after** an accepted promote, call it and, if it returns text, append `{"role":"tool_result","tool":"retrieval_refresh","content": text}` to history (append-only — never rewrites `retrieval_seed`). `ChatController` supplies a cb that asks the retrieval client for compact neighbors/diagnostics of `touched` (pointers only) and best-effort nudges an incremental reindex of those files (`self._retrieval.reindex_files(touched)` if available; swallow errors). Assert in the test that after an accepted edit a `retrieval_refresh` entry exists in `ctrl._histories[thread_id]` and no `retrieval_seed` mutation occurred.

- [ ] **Step 4: Run test to verify it passes** — PASS.

- [ ] **Step 5: Commit**

```bash
git add agentd/chat/controller.py agentd/chat/controller_loop.py tests/test_clarify_resume_and_retrieval_delta.py
git commit -m "feat(chat): clarify-resume regression + append-only retrieval delta on edit"
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
  5. `shadow==real` across reject rounds: edit file A (reject) → edit file B (accept) → assert A unchanged on real, B promoted, and the shadow holds both A (==real) and B (==real) — pins the invariant when a rejected round touched a *different* file than the next.

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

### Task I1: Extend `LiveGateView.kind` + decision client methods

> **Verified:** chat gates render via the `/live` poll → `LiveSlot.GateDispatch` by `kind` (NOT via SSE / `MessageRow`). So there is **no** `mode_choice` `StreamEvent`. The frontend change is to extend `LiveGateView.kind` (`types.ts:56`) to match the backend `PendingGate.kind` (F0), and add decision client methods. Per-edit gate reuses the same gate path with `kind="edit"`.

**Files:**
- Modify: `apps/vscode-extension/webview-ui/src/types.ts` (`LiveGateView.kind`:56) **AND** `apps/vscode-extension/src/controller.ts` (`LiveGateView.kind`:85) — **`LiveGateView` is declared in BOTH** (extension builds it from `/live`, webview renders it); extend the union in both or the build/render won't type-check.
- Modify: `apps/editor-client/src/contracts/task-contracts.ts` (`BackendTaskClient`) + `apps/editor-client/src/client/http-backend-client.ts` (impl, ctor `{ baseUrl, fetchFn? }`).
- Test: `apps/editor-client/test/decision-clients.test.ts`

- [ ] **Step 1: Write the failing test**

```typescript
// apps/editor-client/test/decision-clients.test.ts
import { describe, it, expect, vi } from "vitest";
import { HttpBackendClient } from "../src/client/http-backend-client";

describe("mode/edit decision clients", () => {
  it("posts edit-decision to the right endpoint", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ ok: true }) });
    // ctor takes an options object: { baseUrl, fetchFn? }
    const c = new HttpBackendClient({ baseUrl: "http://x", fetchFn: fetchMock });
    await c.postEditDecision("th1", "reject", "wrong var");
    expect(fetchMock).toHaveBeenCalledWith("http://x/v1/chat/threads/th1/edit-decision",
      expect.objectContaining({ method: "POST" }));
  });
});
```

- [ ] **Step 2: Run** `npm run -w @ai-editor/editor-client test decision-clients` → FAIL (method missing).

- [ ] **Step 3: Implement**
- `types.ts:56` AND `controller.ts:85`: `kind: "command" | "scope" | "validation" | "step" | "mode" | "edit";` (both `LiveGateView` declarations).
- `task-contracts.ts` (`BackendTaskClient`): `postEditDecision(threadId: string, decision: "accept" | "reject", reason?: string): Promise<void>;` (the mode decision is a *streamed* POST consumed like `sendChatMessage`; if you prefer a typed method add `postModeDecision(threadId, mode): AsyncIterable<StreamEvent>`).
- `http-backend-client.ts`: implement `postEditDecision` (plain `POST /v1/chat/threads/{id}/edit-decision` body `{decision, reason}`, mirror the existing `fetchJson(path, {method:"POST", ...})` POST methods) and, if added, `postModeDecision` (SSE, mirror `sendChatMessage` against `/mode-decision` body `{mode}`).
- **Controller `/live`→`LiveGateView` build (verified bug at `controller.ts:1456`):** the current code is `if (live.pendingGate && live.activeTaskId) { renderLiveGate({kind, payload, taskId: live.activeTaskId}) }`. A controller turn has **no `activeTaskId`**, so the mode/edit gate would never render. Fix it to:
  ```typescript
  if (live.pendingGate) {
    this.ui.renderLiveGate({
      kind: live.pendingGate.kind,
      payload: live.pendingGate.payload,
      taskId: live.activeTaskId ?? threadId,   // controller gates have no task → use the thread id
    });
  }
  ```
  (`threadId` is already in scope — the poll calls `getThreadLiveState(threadId)` at :1431.) Add a guard test that a `pendingGate` with null `activeTaskId` still renders.

- [ ] **Step 4: Run** → PASS; `npm run -w @ai-editor/editor-client build`.

- [ ] **Step 5: Commit**

```bash
git add apps/vscode-extension/webview-ui/src/types.ts apps/editor-client/src/contracts/task-contracts.ts apps/editor-client/src/client/http-backend-client.ts apps/editor-client/test/decision-clients.test.ts
git commit -m "feat(contracts): LiveGateView mode/edit kinds + decision clients"
```

### Task I2: `ModeGate` + `EditGate` in `LiveSlot`; wire decisions in `chat-panel.ts`

> **Verified:** gates are dispatched in `LiveSlot.tsx::GateDispatch` by `kind`; webview→extension messages are handled in `chat-panel.ts::registerHandlers` (the `m["type"]` if-else at :91, where `stepDecision`/`scopeDecision`/… live); the backend already emits the resolution breadcrumb (F2/F3). Valid `Icon` names exclude `"lightbulb"` — use `"bolt"`. The controller gate's `LiveGateView.taskId` is set to the **thread id** by `chat-panel` when it builds the gate from `/live`.

**Files:**
- Create: `apps/vscode-extension/webview-ui/src/components/messages/gates/ModeGate.tsx`, `gates/EditGate.tsx`.
- Modify: `LiveSlot.tsx` (`GateDispatch` cases), `chat-panel.ts` (`registerHandlers` + `onModeDecision`/`onEditDecision`).
- Test: `apps/vscode-extension/webview-ui/src/test/ModeGate.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// apps/vscode-extension/webview-ui/src/test/ModeGate.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ModeGate } from "../components/messages/gates/ModeGate";
import { vscode } from "../vscodeApi";

vi.mock("../vscodeApi", () => ({ vscode: { postMessage: vi.fn() } }));
beforeEach(() => vi.clearAllMocks());

it("renders sketch + options and posts modeDecision (taskId carries the threadId)", () => {
  render(<ModeGate taskId="th1" payload={{ plan_sketch: "add a decorator",
    recommended: "create_task",
    options: [{ mode: "create_task", label: "Plan it" }, { mode: "edit", label: "Edit inline" }] }} />);
  expect(screen.getByText(/add a decorator/)).toBeTruthy();
  fireEvent.click(screen.getByText("Edit inline"));
  expect(vscode.postMessage).toHaveBeenCalledWith({ type: "modeDecision", threadId: "th1", mode: "edit" });
});
```

- [ ] **Step 2: Run** `npm run -w @ai-editor/vscode-extension test ModeGate` → FAIL (component missing).

- [ ] **Step 3: Write `ModeGate.tsx`** (signature matches the other gates: `{ taskId, payload }`; `taskId` carries the thread id):

```tsx
// apps/vscode-extension/webview-ui/src/components/messages/gates/ModeGate.tsx
import { useState } from "react";
import { vscode } from "../../../vscodeApi";
import { CardShell } from "../../shared/CardShell";
import { BtnPrimary, BtnGhost } from "../../shared/buttons";

interface Option { mode: string; label?: string; description?: string }
interface Props { taskId: string; payload: Record<string, unknown> }  // taskId == threadId for controller gates

export function ModeGate({ taskId, payload }: Props) {
  const sketch = String(payload.plan_sketch ?? "");
  const recommended = String(payload.recommended ?? "");
  const options = (Array.isArray(payload.options) ? payload.options : []) as Option[];
  const [picked, setPicked] = useState<string | null>(null);
  function pick(mode: string) {
    if (picked !== null) return;
    setPicked(mode);
    vscode.postMessage({ type: "modeDecision", threadId: taskId, mode });
  }
  return (
    <CardShell icon="bolt" title="How should I proceed?"
               subtitle={recommended ? `recommended: ${recommended}` : undefined}
               borderColor="var(--accent-brd)" headerTint="linear-gradient(180deg, var(--accent-bg), transparent)">
      {sketch && <div className="px-2.5 py-2 text-[12px] text-text-1 whitespace-pre-wrap border-t border-border">{sketch}</div>}
      <div className="flex flex-col gap-1.5 px-2.5 py-2 border-t border-border">
        {picked === null ? options.map((o) => {
          const Btn = o.mode === recommended ? BtnPrimary : BtnGhost;
          return (<Btn key={o.mode} onClick={() => pick(o.mode)}>
            <span className="font-medium">{o.label ?? o.mode}</span>
            {o.description && <span className="ml-1 text-[11px] text-text-2">— {o.description}</span>}
          </Btn>);
        }) : <span className="text-[11px] text-text-2">Chose: {picked} — or type a message to discuss further.</span>}
      </div>
    </CardShell>
  );
}
```

`EditGate.tsx` is `StepGate.tsx` with the action handlers posting `{ type: "editDecision", threadId: taskId, decision: "accept"|"reject", reason }` instead of `stepDecision` (a reject prompts for a reason via a small textarea, or sends empty). Copy `StepGate.tsx` and swap the two `vscode.postMessage` calls.

- [ ] **Step 4: Wire dispatch + decisions**
  - `LiveSlot.tsx::GateDispatch`: add `case "mode": return <ModeGate taskId={taskId} payload={payload} />;` and `case "edit": return <EditGate taskId={taskId} payload={payload} />;`.
  - `chat-panel.ts::registerHandlers` (next to `stepDecision` at :134):
    ```typescript
    } else if (m["type"] === "modeDecision") {
      p = this.onModeDecision(m["threadId"] as string, m["mode"] as string);
    } else if (m["type"] === "editDecision") {
      const decision = m["decision"] === "accept" ? "accept" : "reject";
      p = this.onEditDecision(m["threadId"] as string, decision, (m["reason"] as string) ?? "");
    ```
  - `chat-panel.ts`: add `onModeDecision(threadId, mode)` → consume `client.postModeDecision(threadId, mode)` (SSE) into the thread-render path like `onMessage`; `onEditDecision(threadId, decision, reason)` → `await client.postEditDecision(threadId, decision, reason)` (the held-open message stream surfaces the loop continuation). The resolution breadcrumb already arrives via the backend (F2/F3).
  - The "discuss" path needs no UI: typing a normal message calls the existing `sendMessage` flow, which resumes from `_histories` (F1/F4).

- [ ] **Step 5: Run + commit**

```bash
npm run -w @ai-editor/vscode-extension test ModeGate && npm run build
git add apps/vscode-extension/webview-ui/src apps/vscode-extension/src/chat-panel.ts
git commit -m "feat(webview): ModeGate + EditGate in LiveSlot; mode/edit decision wiring"
```

---

## Phase J — Live dev-host smoke (no unit test substitutes this)

> Drive the real dev-host per `docs/superpowers/plans/2026-06-14-tierB-narrative-smoke.md` env recipe (backend :8001, worktree extension, Playwright CDP frame-eval). Set `AI_EDITOR_CHAT_CONTROLLER=1`.

- [ ] **J1** QA turn: ask a question → agent explores → `answer` renders; no mode card.
- [ ] **J2** Edit turn: "add X to file Y" → agent emits `propose_mode` showing a **`plan_sketch`** ("here's my approach") + options → pick **Edit inline** → per-edit diff card → Accept → file changed on real ws; subsequent read in same turn sees the edit.
- [ ] **J2b** Discuss path: on a `propose_mode` card, **don't pick** — type a follow-up ("actually, also handle the error case") → the turn resumes and the agent re-proposes a refined sketch (mirrors plan-approval feedback).
- [ ] **J3** Reject path: trigger an `edit` → Reject with reason → real ws unchanged; agent revises and re-proposes.
- [ ] **J4** create_task path: "big multi-file change" → `propose_mode` recommends **Plan as task** (with a sketch) → pick it → existing plan-approval flow runs unchanged (the concrete plan still goes through its own approval gate).
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

**Spec coverage:** §3 architecture → Phases E/F; §4 action union → B1/E1–E3; §5 phase SM + ACID edit → C1/D0/D1/E3; §6 cache payload → B2 + H1; §7 ToolSource seam → A1/A2; §8 migration → G1/K1; §9 invariants → H1; §12 mirror/DRY/patterns → B3 (shared primitives), C1 (State), A2 (Composite), D0 (extracted patch/promote/diff), E1 (mirror loop). Deferred subsystems (#2–#6) intentionally absent.

**Placeholder scan:** Phases A–I now carry **full literal code** (constructor, gate plumbing, routes, components) verified against source — no "mirror the pattern" stubs remain except where a step legitimately says "copy the exact shape from `_pending_step_decisions`/`/step-decision`" for the route boilerplate (which is shown). Phases J (live smoke) and K (deletion) are checklists by nature, not code.

**Frontend re-trace correction (2026-06-15):** an initial shallow frontend pass was wrong on every structural point and was redone against source. Confirmed: interactive gates are **`/live`-polled `LiveSlot.GateDispatch` cards keyed by `kind`** (not SSE `MessageRow` cases); the controller turn has **no task**, so its gates need a **thread-level** `ChatThread.pending_controller_gate` exposed via `/live` (new **Task F0**), paired with the in-memory future for the held-open edit gate (mirrors task gates' `pending_step_review` + `_pending_step_decisions`); decision posts wire through **`chat-panel.ts::registerHandlers`** (not `controller.ts`); `Icon` has no `"lightbulb"` (use `"bolt"`). F2/F3 set/clear `pending_controller_gate`; I1 extends `LiveGateView.kind`; I2 adds `ModeGate`/`EditGate` to `GateDispatch`. Resolution leaves a breadcrumb (no new `MessageRow` type).

**Anchor verification (2026-06-15):** all assumed APIs traced to source and corrected — `apply_patch_candidate`/`PatchDocumentV2` (not `PatchEngine.apply`), `_partial_promote`→`promote_files` (no `promote_files` on the manager), `ChatThread.thread_id`/role `"agent"`, `ScriptedReasoningEngine(plan, patches, *_responses)`, `create_task_from_chat(*, …)` keyword-only, `post_chat_message` SSE shape. D0 extracts `apply_ops`/`compute_diff_entries`/`promote_files` as the single shared implementation.

**Type consistency:** `ToolSource`/`AggregatingToolRegistry`/`BuiltinToolSource` (A); `controller_response_schema(phase=)`/`_PHASE_TYPES` (B/C); `ControllerOutcome`/`ControllerLoop.run(…, auto_accept_edits, edit_decision_cb, retrieval_delta_cb)` (E/F3/F4); `TurnEditSession.apply/accept/reject/close` (D); `ChatController._histories`/`_pending_mode`/`_pending_edit`/`resolve_mode`/`resolve_edit` (F1–F4); `mode_choice` event + `ModeChoiceCard` (I) — consistent across tasks.

**Failure-path dry-run (review pass):**
1. **Shadow seeding for new files mid-turn** — `TurnEditSession._ensure_shadow` copies a newly-touched existing file into the lightweight shadow on second+ edits (else `apply_ops` would patch a missing file). Created-from-scratch files have no real source → `CreateFileOp` handles them. ✓ (covered by D1's multi-edit + E3's two-edit tests).
2. **`shadow==real` after reject across rounds** — reject restores touched files from real; next edit re-seeds. The invariant test (H1 #4) must include a reject-then-edit-different-file case. **Added risk note** → H1 test 4 expanded below.
3. **propose_mode → edit dispatch loses phase** — `resolve_mode("edit")` calls `_run_loop(phase="EDIT")`, which `enter_edit_mode()`s a *fresh* SM seeded with prior history; the agent must re-emit `edit` (it can, EDIT phase allows it). If the agent instead re-emits `propose_mode` it's blocked by the EDIT enum → malformed-correction loop. **Mitigation:** the EDIT-phase instruction explicitly says "you are now in edit mode; emit edit/submit_changes." (add to `build_controller_step_payload` instruction text).
4. **Held-stream edit gate vs client disconnect** — if the SSE client drops while `_pending_edit` awaits, the future never resolves → the turn hangs. Mirror the task gate's behavior: add an `AI_EDITOR_CHAT_EDIT_DECISION_TIMEOUT_SEC` (default 0 = wait) and on timeout default to reject. **Added to F3 as a hardening step.**
5. **`create_task_from_chat` explore_context empty** — controller passes `explore_context=[]`; the planner re-explores from scratch (acceptable, the controller's exploration wasn't a task). Optionally pass the controller's tool-result history as `initial_explore_context`. Noted, not required for v1.
6. **Retrieval seed staleness across a long session** — seed is frozen at session start; deltas + live tools cover freshness (spec §6). Acceptable; compaction is the future module's job.
