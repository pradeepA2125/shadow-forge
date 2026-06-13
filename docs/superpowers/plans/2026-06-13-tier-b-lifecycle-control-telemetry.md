# Tier B — Task Lifecycle Control & Durable Telemetry — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the chat UI's lifecycle promises true and durable — true revert at review/abort, cooperative Stop, durable failure/run summaries, and a live-mutable "review each step" preference.

**Architecture:** Keep the partial-promote write model (live per-step edits) but pin one pre-execution checkpoint so reject/abort can perform an exact rollback by reusing `_restore_shadow_checkpoint` + `workspace_manager.promote` (which copies modified files and deletes shadow-absent ones, keyed on `task.modified_files`). Add a per-running-task in-memory `TaskControl` channel (abort event + revert flag + live review pref) that `_execute_plan` and the `ToolLoop` poll. Persist `failure_summary`/`run_summary` on `TaskRecord` and expose via `/live` + `TaskResult`.

**Tech Stack:** Python 3.11 (FastAPI, Pydantic, pytest-asyncio), TypeScript (editor-client Zod contracts, vscode-extension controller, React webview-ui + vitest).

**Spec:** `docs/superpowers/specs/2026-06-13-tier-b-lifecycle-control-telemetry-design.md`

**Deviation from spec Component 3 (recorded):** `_partial_promote` is copy-only, so the final `promote` at accept reconciles step-deleted files and is NOT safe to drop. Finish therefore keeps the existing accept→PROMOTING→promote→SUCCEEDED path. The F8 deliverable is Discard=true-revert (Task 2) + durable run_summary (Task 8), not dropping the promote.

---

## File Structure

**Backend (`services/agentd-py/agentd/`)**
- `orchestrator/engine.py` — add `_create_pre_execution_checkpoint`, `_rollback_to_pre_execution`; capture the baseline in `_execute_plan`; read review pref from the control channel; poll abort in the step loop; finalize/write summaries. The `TaskControl` registry + lifecycle also live here.
- `orchestrator/task_control.py` — **new** small module: `TaskControl` dataclass + `TaskAborted` exception (kept out of the 3.7k-line engine for focus).
- `tools/loop.py` — `ToolLoop` accepts an optional `abort: asyncio.Event`; checks it between ReAct iterations and raises `TaskAborted`.
- `workspace/shadow.py` — `prune_checkpoints` is unchanged; the pre-execution checkpoint lives under a separate `_baselines/` root it never scans.
- `domain/models.py` — `TaskExecutionState.pre_execution_checkpoint`; new `FailureSummary` / `RunSummary`; `TaskRecord.failure_summary` / `.run_summary`.
- `api/routes.py` — `/reject` performs rollback; new `/abort`, `/review-pref`; `/cancel` unchanged for terminal/queued.
- `chat/live_state.py` — surface `failure_summary` + `run_summary`.

**Contracts / Frontend**
- `apps/editor-client/src/contracts/task-contracts.ts` — Zod for the new fields + routes.
- `apps/vscode-extension/src/controller.ts`, `chat-panel.ts` — abort (keep/revert), review-pref, durable card data.
- `apps/vscode-extension/webview-ui/src/...` — Stop keep/revert, ReviewCard Finish/Discard, ErrorCard durable detail, dynamic checkbox.

**Slice order:** 1 (checkpoint + rollback helper) → 2 (Discard-revert at review) → 3 (control channel + abort) → 4 (durable telemetry) → 5 (dynamic review pref). Each slice is independently testable and committable.

---

## Slice 1 — Pre-execution checkpoint + rollback helper

### Task 1: Add `pre_execution_checkpoint` to the execution state

**Files:**
- Modify: `services/agentd-py/agentd/domain/models.py:205` (`TaskExecutionState`)
- Test: `services/agentd-py/tests/test_models_pre_exec_checkpoint.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_models_pre_exec_checkpoint.py
from agentd.domain.models import TaskExecutionState


def test_execution_state_has_pre_execution_checkpoint_default_none():
    state = TaskExecutionState()
    assert state.pre_execution_checkpoint is None
    state.pre_execution_checkpoint = "/abs/path/_baselines/task-1/shadow"
    assert state.pre_execution_checkpoint.endswith("/shadow")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/agentd-py && python -m pytest tests/test_models_pre_exec_checkpoint.py -q`
Expected: FAIL — `TaskExecutionState` has no attribute `pre_execution_checkpoint`.

- [ ] **Step 3: Add the field**

In `domain/models.py`, inside `class TaskExecutionState`, after `step_checkpoints`:

```python
    # Pinned pristine shadow snapshot captured before step 1, used to roll the real
    # workspace back to its pre-execution state on Discard/abort-revert. Lives under a
    # separate _baselines root so prune_checkpoints never reaps it; cleared at terminal.
    pre_execution_checkpoint: str | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/agentd-py && python -m pytest tests/test_models_pre_exec_checkpoint.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/agentd-py/agentd/domain/models.py services/agentd-py/tests/test_models_pre_exec_checkpoint.py
git commit -m "feat(models): pre_execution_checkpoint on TaskExecutionState"
```

### Task 2: `_create_pre_execution_checkpoint` + `_rollback_to_pre_execution`

**Files:**
- Modify: `services/agentd-py/agentd/orchestrator/engine.py` (near `_create_shadow_checkpoint`, ~3522)
- Test: `services/agentd-py/tests/test_pre_execution_rollback.py`

The rollback restores the shadow to the pristine baseline then calls `workspace_manager.promote(task)`, which (per `workspace/shadow.py:145-162`) copies each `modified_files` entry present in the shadow and **deletes** from the real workspace any entry absent from the shadow — an exact rollback (restore originals + delete created files).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pre_execution_rollback.py
import asyncio
from pathlib import Path

import pytest

from agentd.domain.models import TaskBudget, TaskRecord, TaskStatus
from agentd.orchestrator.engine import AgentOrchestrator
from agentd.patch.engine import PatchEngine
from agentd.storage.sqlite import SQLiteTaskStore
from agentd.workspace.shadow import ShadowWorkspaceManager


class _NoReason:
    async def create_plan(self, *a, **k): raise NotImplementedError
    async def create_patch(self, *a, **k): raise NotImplementedError
    async def create_tool_step(self, *a, **k): raise NotImplementedError
    async def create_planning_step(self, *a, **k): raise NotImplementedError


class _OkValidator:
    async def run(self, _p): from agentd.domain.models import ValidationResult; return ValidationResult(success=True, diagnostics=[], duration_ms=1)


def _orch(tmp_path: Path) -> AgentOrchestrator:
    return AgentOrchestrator(
        store=SQLiteTaskStore(tmp_path / "db.sqlite3"),
        reasoning_engine=_NoReason(),
        validator=_OkValidator(),
        patch_engine=PatchEngine(),
        workspace_manager=ShadowWorkspaceManager(tmp_path / "shadows"),
    )


@pytest.mark.asyncio
async def test_rollback_restores_modified_and_deletes_created(tmp_path: Path):
    # real workspace: keep.py (will be modified), original content
    real = tmp_path / "ws"
    (real / "src").mkdir(parents=True)
    (real / "src" / "keep.py").write_text("original\n")
    orch = _orch(tmp_path)
    shadow = await orch._workspace_manager.prepare("task-1", str(real))
    shadow_path = Path(shadow.shadow_path)

    task = TaskRecord(task_id="task-1", goal="g", workspace_path=str(real),
                      shadow_workspace_path=str(shadow_path), budget=TaskBudget())
    # capture baseline BEFORE any edit
    orch._create_pre_execution_checkpoint(task, shadow_path)
    assert task.execution_state.pre_execution_checkpoint is not None

    # simulate execution: modify keep.py and create new.py in BOTH shadow and real
    # (partial-promote already copied them to real during the run)
    for root in (shadow_path, real):
        (root / "src" / "keep.py").write_text("changed by task\n")
        (root / "src" / "new.py").write_text("created by task\n")
    task.modified_files = ["src/keep.py", "src/new.py"]

    await orch._rollback_to_pre_execution(task)

    assert (real / "src" / "keep.py").read_text() == "original\n"   # restored
    assert not (real / "src" / "new.py").exists()                    # created file deleted
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/agentd-py && python -m pytest tests/test_pre_execution_rollback.py -q`
Expected: FAIL — `AgentOrchestrator` has no `_create_pre_execution_checkpoint`.

- [ ] **Step 3: Implement both helpers**

In `orchestrator/engine.py`, add near `_create_shadow_checkpoint`:

```python
    def _create_pre_execution_checkpoint(self, task: TaskRecord, shadow_path: Path) -> None:
        """Snapshot the pristine shadow (pre-step-1) under a _baselines root that
        prune_checkpoints never scans, so it survives until the task terminates. Idempotent:
        a second call (e.g. a resumed run) overwrites with the current pre-execution state."""
        baseline_root = shadow_path.parent / "_baselines" / task.task_id
        snapshot_path = baseline_root / "shadow"
        if baseline_root.exists():
            shutil.rmtree(baseline_root)
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(shadow_path, snapshot_path)
        task.execution_state.pre_execution_checkpoint = str(snapshot_path)

    async def _rollback_to_pre_execution(self, task: TaskRecord) -> None:
        """Roll the REAL workspace back to its pre-execution state. Restores the shadow to the
        pinned baseline, then promote() restores modified files to originals and deletes the
        task-created files (those absent from the pristine shadow). No-op if no baseline."""
        checkpoint = task.execution_state.pre_execution_checkpoint
        if not checkpoint or task.shadow_workspace_path is None:
            return
        shadow_path = Path(task.shadow_workspace_path)
        self._restore_shadow_checkpoint(shadow_path, checkpoint)
        await self._workspace_manager.promote(task)
```

(`shutil` and `Path` are already imported in `engine.py`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/agentd-py && python -m pytest tests/test_pre_execution_rollback.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/agentd-py/agentd/orchestrator/engine.py services/agentd-py/tests/test_pre_execution_rollback.py
git commit -m "feat(engine): pre-execution checkpoint + _rollback_to_pre_execution (reuses restore+promote)"
```

### Task 3: Capture the baseline at the start of `_execute_plan`

**Files:**
- Modify: `services/agentd-py/agentd/orchestrator/engine.py:1387` (right after the EXECUTING transition, before the step loop)
- Test: `services/agentd-py/tests/test_pre_execution_rollback.py` (add a case)

- [ ] **Step 1: Write the failing test** (append)

```python
@pytest.mark.asyncio
async def test_execute_plan_captures_baseline_before_first_step(tmp_path: Path, monkeypatch):
    """After EXECUTING, the pre-execution checkpoint is pinned before any step runs."""
    real = tmp_path / "ws"; (real).mkdir()
    (real / "a.py").write_text("x = 1\n")
    orch = _orch(tmp_path)
    shadow = await orch._workspace_manager.prepare("task-2", str(real))
    from agentd.domain.models import PlanDocument, PlanStep
    task = TaskRecord(task_id="task-2", goal="g", workspace_path=str(real),
                      shadow_workspace_path=str(shadow.shadow_path), budget=TaskBudget(),
                      status=TaskStatus.PLANNED,
                      plan=PlanDocument(summary="s", steps=[PlanStep(id="s1", goal="noop", targets=[])]))
    captured = {}
    # Stop after the first step begins: assert the baseline was already captured.
    async def _fake_run_step(*a, **k):
        captured["baseline"] = task.execution_state.pre_execution_checkpoint
        raise RuntimeError("stop-after-capture")
    monkeypatch.setattr(orch, "_run_step_with_retries", _fake_run_step)
    from agentd.retrieval.models import RetrievalContext
    with pytest.raises(RuntimeError, match="stop-after-capture"):
        await orch._execute_plan(task, shadow, RetrievalContext.empty(), [], 0)
    assert captured["baseline"] is not None
```

(If `RetrievalContext.empty()` differs, use the constructor the suite already uses — grep `RetrievalContext(` in `tests/`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/agentd-py && python -m pytest tests/test_pre_execution_rollback.py::test_execute_plan_captures_baseline_before_first_step -q`
Expected: FAIL — `captured["baseline"]` is `None`.

- [ ] **Step 3: Capture the baseline**

In `_execute_plan`, immediately after `task = transition(task, TaskStatus.EXECUTING, "execution started")` and its `save` (engine.py:1387-1388), before `baseline_errors = ...`:

```python
            # Pin the pristine pre-step-1 shadow so Discard/abort-revert can roll the real
            # workspace back (Tier B). Captured once per run; a resumed child captures its own
            # resume-start state (rollback then undoes only this run's steps).
            self._create_pre_execution_checkpoint(task, shadow_path)
            await self._store.save(task)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/agentd-py && python -m pytest tests/test_pre_execution_rollback.py -q`
Expected: PASS (both cases).

- [ ] **Step 5: Commit**

```bash
git add services/agentd-py/agentd/orchestrator/engine.py services/agentd-py/tests/test_pre_execution_rollback.py
git commit -m "feat(engine): capture pre-execution checkpoint at _execute_plan start"
```

---

## Slice 2 — Discard all changes (true revert at READY_FOR_REVIEW)

### Task 4: `/reject` rolls back instead of keeping changes

**Files:**
- Modify: `services/agentd-py/agentd/api/routes.py:494-518` (`reject_patch`)
- Test: `services/agentd-py/tests/test_reject_reverts.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_reject_reverts.py
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agentd.api.routes import build_router
from agentd.domain.models import TaskBudget, TaskRecord, TaskStatus
from agentd.orchestrator.engine import AgentOrchestrator
from agentd.patch.engine import PatchEngine
from agentd.storage.sqlite import SQLiteTaskStore
from agentd.workspace.shadow import ShadowWorkspaceManager


class _NoReason:
    async def create_plan(self, *a, **k): raise NotImplementedError
    async def create_patch(self, *a, **k): raise NotImplementedError
    async def create_tool_step(self, *a, **k): raise NotImplementedError
    async def create_planning_step(self, *a, **k): raise NotImplementedError


class _OkValidator:
    async def run(self, _p):
        from agentd.domain.models import ValidationResult
        return ValidationResult(success=True, diagnostics=[], duration_ms=1)


@pytest.mark.asyncio
async def test_reject_reverts_real_workspace(tmp_path: Path):
    real = tmp_path / "ws"; (real / "src").mkdir(parents=True)
    (real / "src" / "keep.py").write_text("original\n")
    store = SQLiteTaskStore(tmp_path / "db.sqlite3")
    wm = ShadowWorkspaceManager(tmp_path / "shadows")
    orch = AgentOrchestrator(store=store, reasoning_engine=_NoReason(),
                             validator=_OkValidator(), patch_engine=PatchEngine(),
                             workspace_manager=wm)
    shadow = await wm.prepare("task-1", str(real))
    shadow_path = Path(shadow.shadow_path)
    task = TaskRecord(task_id="task-1", goal="g", workspace_path=str(real),
                      shadow_workspace_path=str(shadow_path), budget=TaskBudget(),
                      status=TaskStatus.READY_FOR_REVIEW, modified_files=["src/keep.py", "src/new.py"])
    orch._create_pre_execution_checkpoint(task, shadow_path)
    await store.create(task)
    # simulate a completed run: real workspace already has the task's changes
    (real / "src" / "keep.py").write_text("changed\n")
    (real / "src" / "new.py").write_text("created\n")

    app = FastAPI()
    app.include_router(build_router(store, orch, wm, None, None))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/v1/tasks/task-1/reject", json={"reason": "not needed"})

    assert resp.status_code == 200
    assert (real / "src" / "keep.py").read_text() == "original\n"   # reverted
    assert not (real / "src" / "new.py").exists()                    # created file removed
    assert (await store.get("task-1")).status == TaskStatus.ABORTED
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/agentd-py && python -m pytest tests/test_reject_reverts.py -q`
Expected: FAIL — `keep.py` still `"changed\n"`, `new.py` still exists (current reject keeps changes).

- [ ] **Step 3: Implement the rollback in `reject_patch`**

Replace the body of `reject_patch` (routes.py:494-518) so it rolls back **before** clearing the shadow:

```python
        if task.status != TaskStatus.READY_FOR_REVIEW:
            msg = f"Task {task_id} is not in READY_FOR_REVIEW state"
            raise HTTPException(status_code=409, detail=msg)

        # Discard all changes = true revert to the pre-execution state, then drop the shadow.
        await orchestrator._rollback_to_pre_execution(task)
        await workspace_manager.cleanup(task)
        task.shadow_workspace_path = None
        task = transition(task, TaskStatus.ABORTED, f"changes discarded: {request.reason}")
        await store.save(task)
        await workspace_manager.prune_checkpoints()

        orchestrator.write_chat_breadcrumb(
            task,
            "✗ All changes discarded — workspace rolled back to its pre-task state.",
        )
        return _to_task_result(task)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/agentd-py && python -m pytest tests/test_reject_reverts.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/agentd-py/agentd/api/routes.py services/agentd-py/tests/test_reject_reverts.py
git commit -m "feat(api): /reject performs true revert to pre-execution state (Discard all changes)"
```

---

## Slice 3 — Control channel + cooperative abort

### Task 5: `TaskControl` + `TaskAborted`

**Files:**
- Create: `services/agentd-py/agentd/orchestrator/task_control.py`
- Test: `services/agentd-py/tests/test_task_control.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_task_control.py
import asyncio

from agentd.orchestrator.task_control import TaskAborted, TaskControl


def test_task_control_defaults_and_mutation():
    c = TaskControl(step_review_auto_accept=True)
    assert not c.abort.is_set()
    assert c.abort_revert is False
    assert c.step_review_auto_accept is True
    c.abort_revert = True
    c.abort.set()
    c.step_review_auto_accept = False
    assert c.abort.is_set() and c.abort_revert and not c.step_review_auto_accept


def test_task_aborted_is_exception():
    assert issubclass(TaskAborted, Exception)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/agentd-py && python -m pytest tests/test_task_control.py -q`
Expected: FAIL — module `agentd.orchestrator.task_control` not found.

- [ ] **Step 3: Create the module**

```python
# agentd/orchestrator/task_control.py
"""In-memory per-running-task control channel for cooperative abort and the live-mutable
step-review preference. Single-process asyncio: check+set with no await between is race-safe."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


class TaskAborted(Exception):
    """Raised inside the execution loop when an abort signal is observed."""


@dataclass
class TaskControl:
    step_review_auto_accept: bool
    abort: asyncio.Event = field(default_factory=asyncio.Event)
    abort_revert: bool = False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/agentd-py && python -m pytest tests/test_task_control.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/agentd-py/agentd/orchestrator/task_control.py services/agentd-py/tests/test_task_control.py
git commit -m "feat(engine): TaskControl channel + TaskAborted"
```

### Task 6: Control registry on the orchestrator + abort polling in the step loop

**Files:**
- Modify: `services/agentd-py/agentd/orchestrator/engine.py` (`__init__`; `_execute_plan`)
- Test: `services/agentd-py/tests/test_cooperative_abort.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cooperative_abort.py
from pathlib import Path

import pytest

from agentd.domain.models import (PlanDocument, PlanStep, TaskBudget, TaskRecord, TaskStatus)
from agentd.orchestrator.engine import AgentOrchestrator
from agentd.patch.engine import PatchEngine
from agentd.storage.sqlite import SQLiteTaskStore
from agentd.workspace.shadow import ShadowWorkspaceManager


class _NoReason:
    async def create_plan(self, *a, **k): raise NotImplementedError
    async def create_patch(self, *a, **k): raise NotImplementedError
    async def create_tool_step(self, *a, **k): raise NotImplementedError
    async def create_planning_step(self, *a, **k): raise NotImplementedError


class _OkValidator:
    async def run(self, _p):
        from agentd.domain.models import ValidationResult
        return ValidationResult(success=True, diagnostics=[], duration_ms=1)


@pytest.mark.asyncio
async def test_abort_between_steps_marks_aborted(tmp_path: Path, monkeypatch):
    real = tmp_path / "ws"; real.mkdir(); (real / "a.py").write_text("x=1\n")
    store = SQLiteTaskStore(tmp_path / "db.sqlite3")
    wm = ShadowWorkspaceManager(tmp_path / "shadows")
    orch = AgentOrchestrator(store=store, reasoning_engine=_NoReason(),
                             validator=_OkValidator(), patch_engine=PatchEngine(),
                             workspace_manager=wm)
    shadow = await wm.prepare("task-1", str(real))
    task = TaskRecord(task_id="task-1", goal="g", workspace_path=str(real),
                      shadow_workspace_path=str(shadow.shadow_path), budget=TaskBudget(),
                      status=TaskStatus.PLANNED,
                      plan=PlanDocument(summary="s", steps=[PlanStep(id="s1", goal="noop", targets=[])]))
    # Register a control whose abort is already set → loop should bail before running s1.
    ctrl = orch._register_task_control(task.task_id, step_review_auto_accept=True)
    ctrl.abort.set()
    from agentd.retrieval.models import RetrievalContext
    out = await orch._execute_plan(task, shadow, RetrievalContext.empty(), [], 0)
    assert out.status == TaskStatus.ABORTED
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/agentd-py && python -m pytest tests/test_cooperative_abort.py -q`
Expected: FAIL — `AgentOrchestrator` has no `_register_task_control`.

- [ ] **Step 3: Implement the registry + the abort check**

In `AgentOrchestrator.__init__`, add:

```python
        self._task_controls: dict[str, "TaskControl"] = {}
```

Add the import at the top of `engine.py`: `from agentd.orchestrator.task_control import TaskAborted, TaskControl`.

Add methods on the orchestrator:

```python
    def _register_task_control(self, task_id: str, *, step_review_auto_accept: bool) -> TaskControl:
        control = TaskControl(step_review_auto_accept=step_review_auto_accept)
        self._task_controls[task_id] = control
        return control

    def get_task_control(self, task_id: str) -> TaskControl | None:
        return self._task_controls.get(task_id)

    def _release_task_control(self, task_id: str) -> None:
        self._task_controls.pop(task_id, None)
```

In `_execute_plan`, at the **top of the `while (step := ...)` loop** (engine.py:1414), before `plan_steps = ...`:

```python
                control = self._task_controls.get(task.task_id)
                if control is not None and control.abort.is_set():
                    raise TaskAborted()
```

Wrap the step loop's enclosing logic so `TaskAborted` unwinds to an ABORTED finalize. At the `except`/`finally` level of `_execute_plan`'s `try` (the outer one beginning engine.py:1379), add an `except TaskAborted` BEFORE the generic handler:

```python
        except TaskAborted:
            control = self._task_controls.get(task.task_id)
            if control is not None and control.abort_revert:
                await self._rollback_to_pre_execution(task)
            await self._workspace_manager.cleanup(task)
            task.shadow_workspace_path = None
            task = transition(task, TaskStatus.ABORTED, "aborted by user")
            await self._store.save(task)
            self.write_chat_breadcrumb(
                task,
                "✗ Run reverted — workspace rolled back." if (control and control.abort_revert)
                else "✗ Run stopped — changes so far kept.",
            )
            return task
```

(If `_execute_plan` registers its own control, also call `self._register_task_control` at entry and `self._release_task_control` in a `finally`. If the caller — `run_task`/`resume_task` — owns registration, keep it there; pick ONE owner. For this plan: register in `run_task`/`resume_task` right before `_execute_plan`, release in their `finally`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/agentd-py && python -m pytest tests/test_cooperative_abort.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/agentd-py/agentd/orchestrator/engine.py services/agentd-py/tests/test_cooperative_abort.py
git commit -m "feat(engine): TaskControl registry + between-steps cooperative abort (keep/revert)"
```

### Task 7: Abort between ToolLoop iterations + `/abort` route

**Files:**
- Modify: `services/agentd-py/agentd/tools/loop.py` (`ToolLoop.__init__`/`run`), `services/agentd-py/agentd/orchestrator/engine.py` (pass `control.abort` into the ToolLoop), `services/agentd-py/agentd/api/routes.py` (new `/abort`)
- Test: `services/agentd-py/tests/test_cooperative_abort.py` (route case), `services/agentd-py/tests/test_tool_loop_abort.py`

- [ ] **Step 1: Write the failing test (ToolLoop)**

```python
# tests/test_tool_loop_abort.py
import asyncio
import pytest
from agentd.orchestrator.task_control import TaskAborted

@pytest.mark.asyncio
async def test_tool_loop_raises_when_abort_set_between_iterations():
    # Build a minimal ToolLoop per the suite's existing constructor (grep `ToolLoop(` in tests/);
    # inject an abort event that is already set, and assert run() raises TaskAborted before the
    # first model call. Use the scripted reasoning + registry pattern from test_tools_registry.py.
    from tests._toolloop_fixtures import make_tool_loop  # add this helper mirroring existing tests
    ev = asyncio.Event(); ev.set()
    loop = make_tool_loop(abort=ev)
    with pytest.raises(TaskAborted):
        await loop.run()
```

> Implementer note: model this on the existing ToolLoop construction in `tests/test_tools_registry.py` / `tests/test_tool_loop*.py`. If no shared fixture exists, inline the construction in the test rather than adding `_toolloop_fixtures`.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/agentd-py && python -m pytest tests/test_tool_loop_abort.py -q`
Expected: FAIL — `ToolLoop` does not accept `abort` / does not check it.

- [ ] **Step 3: Implement the abort check in ToolLoop**

In `tools/loop.py`, add an optional `abort: asyncio.Event | None = None` parameter to `ToolLoop.__init__` (store `self._abort = abort`). At the top of the per-iteration loop in `run()` (the ReAct `while`/`for` iteration), add:

```python
            if self._abort is not None and self._abort.is_set():
                from agentd.orchestrator.task_control import TaskAborted
                raise TaskAborted()
```

In `engine.py`, where the ToolLoop is constructed for step execution (inside `_run_step_with_retries`/step execution), pass `abort=control.abort if (control := self._task_controls.get(task.task_id)) else None`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/agentd-py && python -m pytest tests/test_tool_loop_abort.py -q`
Expected: PASS.

- [ ] **Step 5: Add the `/abort` route + test**

In `routes.py`, add (model `AbortRequest(BaseModel): revert: bool = False` in `domain/models.py` or inline a `dict` body):

```python
    @router.post("/tasks/{task_id}/abort", response_model=TaskView)
    async def abort_task(task_id: str, request: AbortRequest) -> TaskView:
        try:
            task = await store.get(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        control = orchestrator.get_task_control(task_id)
        if control is None:
            raise HTTPException(status_code=409, detail="Task is not running")
        control.abort_revert = bool(request.revert)
        control.abort.set()
        return _to_task_view(task)
```

Route test (append to `test_cooperative_abort.py`): POST `/abort {revert: true}` sets the live control's `abort` + `abort_revert`. (Use a control registered via `orch._register_task_control` before the call.)

- [ ] **Step 6: Run + commit**

Run: `cd services/agentd-py && python -m pytest tests/test_tool_loop_abort.py tests/test_cooperative_abort.py -q`
Expected: PASS.

```bash
git add services/agentd-py/agentd/tools/loop.py services/agentd-py/agentd/orchestrator/engine.py services/agentd-py/agentd/api/routes.py services/agentd-py/agentd/domain/models.py services/agentd-py/tests/test_tool_loop_abort.py services/agentd-py/tests/test_cooperative_abort.py
git commit -m "feat: mid-iteration ToolLoop abort + POST /tasks/{id}/abort {revert}"
```

---

## Slice 4 — Durable telemetry

### Task 8: `FailureSummary` / `RunSummary` models + TaskRecord fields

**Files:**
- Modify: `services/agentd-py/agentd/domain/models.py` (new models + `TaskRecord` fields)
- Test: `services/agentd-py/tests/test_telemetry_models.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_telemetry_models.py
from agentd.domain.models import FailureSummary, RunSummary, TaskRecord, TaskBudget


def test_summaries_default_none_and_assignable():
    t = TaskRecord(task_id="t", goal="g", workspace_path="/w", budget=TaskBudget())
    assert t.failure_summary is None and t.run_summary is None
    t.failure_summary = FailureSummary(step_id="s1", step_index=3, error_class="VerifyPhaseExhausted", message="m")
    t.run_summary = RunSummary(steps_completed=2, steps_total=4, deviations=["scope: x.py"])
    assert t.failure_summary.error_class == "VerifyPhaseExhausted"
    assert t.run_summary.steps_completed == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/agentd-py && python -m pytest tests/test_telemetry_models.py -q`
Expected: FAIL — `FailureSummary` import error.

- [ ] **Step 3: Add the models + fields**

In `domain/models.py` (before `TaskRecord`):

```python
class FailureSummary(BaseModel):
    step_id: str | None = None
    step_index: int | None = None
    error_class: str
    message: str


class RunSummary(BaseModel):
    steps_completed: int
    steps_total: int
    deviations: list[str] = Field(default_factory=list)
```

In `class TaskRecord`, add:

```python
    failure_summary: FailureSummary | None = None
    run_summary: RunSummary | None = None
```

- [ ] **Step 4: Run + commit**

Run: `cd services/agentd-py && python -m pytest tests/test_telemetry_models.py -q` → PASS.

```bash
git add services/agentd-py/agentd/domain/models.py services/agentd-py/tests/test_telemetry_models.py
git commit -m "feat(models): FailureSummary + RunSummary on TaskRecord"
```

### Task 9: Write summaries at terminal transitions

**Files:**
- Modify: `services/agentd-py/agentd/orchestrator/engine.py` (a `_finalize_run_summary(task)` helper called on SUCCEEDED/FAILED/ABORTED; `_write_failure_summary(task, step, exc)` at the FAILED site)
- Test: `services/agentd-py/tests/test_telemetry_write.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_telemetry_write.py
from agentd.domain.models import (PlanDocument, PlanStep, RunSummary, TaskBudget, TaskRecord)
from agentd.orchestrator.engine import AgentOrchestrator
# build orch like prior tests (SQLiteTaskStore, _NoReason, _OkValidator) — reuse a local _orch().

def test_finalize_run_summary_counts_completed_and_total(tmp_path):
    orch = _orch(tmp_path)  # same helper as test_pre_execution_rollback.py
    task = TaskRecord(task_id="t", goal="g", workspace_path="/w", budget=TaskBudget(),
                      completed_step_ids=["s1", "s2"],
                      plan=PlanDocument(summary="s", steps=[PlanStep(id=f"s{i}", goal="g", targets=[]) for i in (1, 2, 3, 4)]))
    task.execution_state.delta_replans_used = 1
    orch._finalize_run_summary(task)
    assert task.run_summary == RunSummary(steps_completed=2, steps_total=4, deviations=["1 delta replan(s)"])
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd services/agentd-py && python -m pytest tests/test_telemetry_write.py -q`
Expected: FAIL — no `_finalize_run_summary`.

- [ ] **Step 3: Implement the helpers**

In `engine.py`:

```python
    def _finalize_run_summary(self, task: TaskRecord) -> None:
        total = len(task.plan.steps) if task.plan else 0
        deviations: list[str] = []
        es = task.execution_state
        if es.delta_replans_used:
            deviations.append(f"{es.delta_replans_used} delta replan(s)")
        if es.auto_approved_scope_files:
            deviations.append(f"{len(es.auto_approved_scope_files)} scope extension(s)")
        if es.approved_commands:
            deviations.append(f"{len(es.approved_commands)} command(s) approved")
        task.run_summary = RunSummary(
            steps_completed=len(task.completed_step_ids),
            steps_total=total,
            deviations=deviations,
        )

    def _write_failure_summary(self, task: TaskRecord, *, step_id: str | None,
                               step_index: int | None, exc: BaseException) -> None:
        task.failure_summary = FailureSummary(
            step_id=step_id, step_index=step_index,
            error_class=type(exc).__name__, message=str(exc)[:2000],
        )
```

Call `self._finalize_run_summary(task)` immediately before each terminal `transition(...)` to SUCCEEDED, FAILED, and ABORTED in the engine (the PROMOTING→SUCCEEDED finalize, the FAILED handlers, and the `except TaskAborted` block from Task 6). At the FAILED site that has the failing step in scope, also call `self._write_failure_summary(task, step_id=step.id, step_index=step_index, exc=err)` so **both** summaries are present on FAILED (import `FailureSummary`, `RunSummary` at top of `engine.py`).

- [ ] **Step 4: Run + commit**

Run: `cd services/agentd-py && python -m pytest tests/test_telemetry_write.py -q` → PASS.

```bash
git add services/agentd-py/agentd/orchestrator/engine.py services/agentd-py/tests/test_telemetry_write.py
git commit -m "feat(engine): finalize run_summary on every terminal; failure_summary on FAILED"
```

### Task 10: Expose summaries via `/live` and `TaskResult`/`TaskView`

**Files:**
- Modify: `services/agentd-py/agentd/chat/live_state.py` (`resolve_live_state`), `services/agentd-py/agentd/api/routes.py` (`_to_task_result`/`_to_task_view`), `services/agentd-py/agentd/domain/models.py` (TaskResult/TaskView shapes if separate)
- Test: `services/agentd-py/tests/test_live_state_summaries.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_live_state_summaries.py
from agentd.chat.live_state import resolve_live_state
from agentd.domain.models import (FailureSummary, RunSummary, TaskBudget, TaskRecord, TaskStatus)


def test_live_state_surfaces_failure_and_run_summary():
    task = TaskRecord(task_id="t", goal="g", workspace_path="/w", budget=TaskBudget(),
                      status=TaskStatus.FAILED,
                      failure_summary=FailureSummary(error_class="VerifyPhaseExhausted", message="m"),
                      run_summary=RunSummary(steps_completed=2, steps_total=4, deviations=[]))
    live = resolve_live_state(task)   # match the real signature; grep resolve_live_state usage
    assert live["failure_summary"]["error_class"] == "VerifyPhaseExhausted"
    assert live["run_summary"]["steps_completed"] == 2
```

> Implementer note: match `resolve_live_state`'s actual return type (dict vs model) and signature — grep its definition + call sites in `chat/live_state.py` and `api/routes.py` first; adapt the assertion shape accordingly.

- [ ] **Step 2: Run to verify it fails**

Run: `cd services/agentd-py && python -m pytest tests/test_live_state_summaries.py -q`
Expected: FAIL — no `failure_summary` key in live state.

- [ ] **Step 3: Add to live state + task result**

In `resolve_live_state`, include `failure_summary` (when status in FAILED/ABORTED and present) and `run_summary` (when present), serialized with `.model_dump(mode="json")`. In `_to_task_result`/`_to_task_view`, pass the same two fields through.

- [ ] **Step 4: Run + commit**

Run: `cd services/agentd-py && python -m pytest tests/test_live_state_summaries.py -q` → PASS.

```bash
git add services/agentd-py/agentd/chat/live_state.py services/agentd-py/agentd/api/routes.py services/agentd-py/tests/test_live_state_summaries.py
git commit -m "feat(api): expose failure_summary + run_summary via /live and TaskResult"
```

---

## Slice 5 — Dynamic review preference

### Task 11: Engine reads review pref from the control channel + `/review-pref` route

**Files:**
- Modify: `services/agentd-py/agentd/orchestrator/engine.py:1543,1561` (read from control), `services/agentd-py/agentd/api/routes.py` (new `/review-pref`)
- Test: `services/agentd-py/tests/test_dynamic_review_pref.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dynamic_review_pref.py
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
# build orch + app like test_reject_reverts.py

@pytest.mark.asyncio
async def test_review_pref_route_updates_live_control(tmp_path):
    store, wm, orch, app = _build(tmp_path)  # helper mirroring test_reject_reverts setup
    orch._register_task_control("task-1", step_review_auto_accept=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/v1/tasks/task-1/review-pref", json={"auto_accept": True})
    assert resp.status_code == 200
    assert orch.get_task_control("task-1").step_review_auto_accept is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd services/agentd-py && python -m pytest tests/test_dynamic_review_pref.py -q`
Expected: FAIL — no `/review-pref` route.

- [ ] **Step 3: Implement the route + the engine read**

`routes.py` (body model `ReviewPrefRequest(BaseModel): auto_accept: bool`):

```python
    @router.post("/tasks/{task_id}/review-pref", response_model=TaskView)
    async def set_review_pref(task_id: str, request: ReviewPrefRequest) -> TaskView:
        try:
            task = await store.get(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        control = orchestrator.get_task_control(task_id)
        if control is None:
            raise HTTPException(status_code=409, detail="Task is not running")
        control.step_review_auto_accept = bool(request.auto_accept)
        # If auto_accept turned ON while a step gate is pending, resolve it as accept
        # (consistent intent). The decision future is fired the same way /step-decision does.
        if request.auto_accept and task.execution_state.pending_step_review is not None:
            orchestrator.resolve_pending_step_review(task_id, accept=True)
        return _to_task_view(task)
```

In `engine.py`, at lines 1543 and 1561 replace `task.step_review_auto_accept` with the live control value, falling back to the record default:

```python
                _ctrl = self._task_controls.get(task.task_id)
                _auto = _ctrl.step_review_auto_accept if _ctrl is not None else task.step_review_auto_accept
                if not _auto:
                    decision = await self._pause_for_step_review(...)
```

(Mirror at 1561's `if task.step_review_auto_accept:` → `if _auto:`.) Implement `resolve_pending_step_review(task_id, accept)` on the orchestrator to fire the same decision future the `/step-decision` route uses (reuse the existing future map / `_in_flight` mechanism — grep `pending_step_review` resolution in `routes.py`/`engine.py` and call the identical path).

- [ ] **Step 4: Run + commit**

Run: `cd services/agentd-py && python -m pytest tests/test_dynamic_review_pref.py -q` → PASS.

```bash
git add services/agentd-py/agentd/orchestrator/engine.py services/agentd-py/agentd/api/routes.py services/agentd-py/tests/test_dynamic_review_pref.py
git commit -m "feat: dynamic review pref via /review-pref + live control read; auto-accept resolves pending gate"
```

---

## Slice 6 — Contracts + frontend

### Task 12: editor-client contracts

**Files:**
- Modify: `apps/editor-client/src/contracts/task-contracts.ts`
- Test: `apps/editor-client/test/` (add cases mirroring existing schema tests)

- [ ] **Step 1: Write failing schema tests** for: `failureSummary`/`runSummary` on the task/live shapes; client methods `abortTask(taskId, {revert})`, `setReviewPref(taskId, {autoAccept})`. Mirror an existing test in `apps/editor-client/test/schemas.test.ts`.
- [ ] **Step 2: Run** `npm run -w @ai-editor/editor-client test` → FAIL.
- [ ] **Step 3: Add Zod fields** (`failure_summary`, `run_summary` — snake_case on wire, camel in mapped client types per `http-backend-client.ts` convention) and the two client methods (`POST /v1/tasks/{id}/abort`, `/review-pref`).
- [ ] **Step 4: Build + test**: `npm run -w @ai-editor/editor-client build && npm run -w @ai-editor/editor-client test` → PASS. (Build BEFORE extension typecheck — build-order rule.)
- [ ] **Step 5: Commit** `feat(contracts): failure/run summary + abort/review-pref client methods`.

### Task 13: Extension controller — abort (keep/revert), review-pref, durable cards

**Files:**
- Modify: `apps/vscode-extension/src/controller.ts`, `src/chat-panel.ts`, `test/controller.test.ts`

- [ ] **Step 1: Write failing controller tests** (stub `ControllerUI`): Stop during execution posts `abortTask(taskId, {revert})` for both choices; review checkbox toggle posts `setReviewPref`; `pollThreadLiveState` forwards `failure_summary`→`renderLiveError(detail)` and `run_summary`→`renderLiveReview`.
- [ ] **Step 2: Run** `npm run -w @ai-editor/vscode-extension test` → FAIL.
- [ ] **Step 3: Implement**: `abortActiveTask(revert)`, `setReviewPref(autoAccept)`; in `pollThreadLiveState`, read `live.failure_summary`/`live.run_summary` and pass to `renderLiveError`/`renderLiveReview` (replacing ephemeral `runDeviations`/`lastStepStarted` as source-of-truth, keep as live supplement). New ChatPanel inbound branches `abortTask`, `setReviewPref`.
- [ ] **Step 4: typecheck + test** `npm run -w @ai-editor/vscode-extension typecheck && npm run -w @ai-editor/vscode-extension test` → PASS.
- [ ] **Step 5: Commit** `feat(extension): abort keep/revert, review-pref, durable error/review cards`.

### Task 14: webview-ui — Stop keep/revert, ReviewCard Finish/Discard, ErrorCard durable, dynamic checkbox

**Files:**
- Modify: `apps/vscode-extension/webview-ui/src/components/messages/ReviewCard.tsx`, `ErrorCard.tsx`, the work-bar Stop control, `InputArea.tsx` (checkbox), `types.ts`; tests under `webview-ui/src/test/`

- [ ] **Step 1: Write failing vitest cases**: ReviewCard shows **Finish** + **Discard all changes** and posts `acceptTask` / `rejectTask`; Stop-during-execution shows **Stop & keep** + **Stop & revert** posting `{type:"abortTask",revert}`; ErrorCard renders `failure_summary` detail from props (durable); composer checkbox enabled during execution posts `setReviewPref`.
- [ ] **Step 2: Run** `npm --prefix apps/vscode-extension/webview-ui test` → FAIL.
- [ ] **Step 3: Implement** the four component changes + the `WebviewMessage` union additions (`abortTask {revert}`, `setReviewPref {autoAccept}`) in `types.ts` and the App reducer wiring for durable `liveError.detail`/`liveReview` from `failure_summary`/`run_summary`.
- [ ] **Step 4: Run** `npm --prefix apps/vscode-extension/webview-ui test` → PASS.
- [ ] **Step 5: Commit** `feat(webview): Stop keep/revert, Finish/Discard, durable Error/Review cards, live review toggle`.

---

## Final task: full suites + smoke

- [ ] **Step 1:** `npm run build && npm run test && npm run typecheck` (all TS packages green; build editor-client before extension).
- [ ] **Step 2:** `cd services/agentd-py && python -m pytest -q` — only the documented pre-existing failures remain (gemini/groq transports + `@requires_live_snapshot` graph-walker). Read the FAILED lines; never trust a piped exit code (CLAUDE.md).
- [ ] **Step 3: Dev-host smoke** (backend via `start-backend.sh`, workspace OUTSIDE `.tmp` so graph indexing works): large_change task → Stop & revert mid-execution (workspace rolls back); a second task → reach READY_FOR_REVIEW → Discard all changes (true revert); a third → Finish (changes kept, SUCCEEDED); kill backend mid-execution → reload → ErrorCard shows durable `failure_summary` + `run_summary`; toggle "Review each step" mid-run both directions.
- [ ] **Step 4: Commit** any smoke fixes; update CLAUDE.md "Task Lifecycle" + chat sections (abort/revert semantics, control channel, durable summaries, dynamic review pref).
