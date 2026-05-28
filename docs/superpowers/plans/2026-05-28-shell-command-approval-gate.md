# Shell Command Approval Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the static `AI_EDITOR_SHELL_ALLOWLIST` with a policy-driven, per-command user-approval gate so no shell command runs without explicit (or remembered) consent unless the user opted into `allow_all`.

**Architecture:** Mirror the existing scope-extension gate. `run_command` calls an injected approval callback before executing; in `ask` mode the callback pauses the task (`AWAITING_COMMAND_DECISION`), broadcasts an SSE event, and awaits a future resolved by a new decision route. Approvals can be remembered per-workspace (JSON store) at a user-chosen breadth (exact / prefix / binary).

**Tech Stack:** Python (FastAPI, Pydantic, asyncio), TypeScript (Zod, VS Code extension), pytest, vitest.

**Spec:** `docs/superpowers/specs/2026-05-28-shell-command-approval-gate-design.md`
**Branch:** `feat/agentic-planning-delta-replan`

---

## File Structure

**Backend (`services/agentd-py/`)**
- `agentd/domain/models.py` — modify: add `ShellPolicy` enum, `CommandApprovalRequest`, `CommandDecision`, `CommandRule`; add `AWAITING_COMMAND_DECISION` to `TaskStatus`; add fields to `TaskExecutionState`; add `shell_policy` to `TaskCreateRequest`.
- `agentd/domain/state_machine.py` — modify: add `AWAITING_COMMAND_DECISION` transition edges.
- `agentd/tools/command_rules.py` — **create**: `CommandRuleStore` (per-workspace JSON, match logic).
- `agentd/orchestrator/engine.py` — modify: `_shell_policy` + `_command_decision_timeout_sec` + `_pending_command_decisions`; `_build_command_approval_callback`; pass callback to `ToolRegistry`.
- `agentd/tools/registry.py` — modify: accept `command_approval_callback`, call it in the `run_command` branch; drop allowlist.
- `agentd/tools/shell.py` — modify: remove `allowlist` param + check.
- `agentd/api/routes.py` — modify: `POST /tasks/{id}/command-decision`; `_in_flight_command` guard.
- construction site (`agentd/main.py` / `agentd/chat/app_factory.py`) — modify: read `AI_EDITOR_SHELL_POLICY`, pass to orchestrator.

**Client (`apps/`)**
- `editor-client/src/contracts/task-contracts.ts` — modify: `command_approval_requested` in `StreamEvent`; `CommandDecisionSchema`; `command_card` message type.
- `editor-client/src/client/http-backend-client.ts` — modify: `sendCommandDecision`.
- `vscode-extension/src/controller.ts` — modify: handle event, post decision.
- `vscode-extension/src/chat-panel.ts` + `media/chat.js` — modify: render command card + scope radio.

**Config/docs**
- `.env`, `scripts/stress/start-backend.sh`, `CLAUDE.md` — remove `AI_EDITOR_SHELL_ALLOWLIST`, document `AI_EDITOR_SHELL_POLICY`.

> **Phasing:** Tasks 1–8 deliver a fully working gate testable via `curl` (no UI needed). Tasks 9–11 add the VS Code UI. Task 12 is the end-to-end integration test.

---

## Task 1: Domain models

**Files:**
- Modify: `agentd/domain/models.py`
- Test: `tests/test_command_gate_models.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_command_gate_models.py
from agentd.domain.models import (
    TaskStatus, ShellPolicy, CommandApprovalRequest, CommandDecision,
    CommandRule, TaskExecutionState, TaskCreateRequest,
)


def test_command_gate_models_exist():
    assert TaskStatus.AWAITING_COMMAND_DECISION == "AWAITING_COMMAND_DECISION"
    assert ShellPolicy.ASK == "ask"
    assert ShellPolicy.ALLOW_ALL == "allow_all"

    req = CommandApprovalRequest(
        decision_id="d1", command="python", args=["-c", "print(1)"],
        cwd="services/agentd-py", step_id="s1",
    )
    assert req.command == "python"

    dec = CommandDecision(approve=True, remember=True, scope="prefix")
    assert dec.scope == "prefix"

    rule = CommandRule(type="prefix", value="python -c", added_at="2026-05-28T00:00:00Z")
    assert rule.type == "prefix"

    state = TaskExecutionState()
    assert state.pending_command_request is None
    assert state.approved_commands == []

    assert TaskCreateRequest(goal="g", workspace_path=".").shell_policy is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/agentd-py && .venv/bin/python -m pytest tests/test_command_gate_models.py -v`
Expected: FAIL — `ImportError` / `AttributeError` (symbols not defined).

- [ ] **Step 3: Implement the models**

In `agentd/domain/models.py`, add `AWAITING_COMMAND_DECISION = "AWAITING_COMMAND_DECISION"` to the `TaskStatus` StrEnum (after `AWAITING_VALIDATION_DECISION`). Add near the `ScopePolicy` block:

```python
class ShellPolicy(StrEnum):
    """How run_command is gated."""
    ASK = "ask"             # pause + user gate via POST /command-decision (default)
    ALLOW_ALL = "allow_all" # run any command without prompting


class CommandApprovalRequest(BaseModel):
    """Persisted on the task while the engine waits for a command decision."""
    decision_id: str
    command: str
    args: list[str] = Field(default_factory=list)
    cwd: str = ""
    step_id: str


class CommandDecision(BaseModel):
    approve: bool
    remember: bool = False
    scope: Literal["exact", "prefix", "binary"] = "exact"
    # For approve+remember: the rule value the UI chose (e.g. "python -c").
    # When omitted the engine derives it from the request per `scope`.
    rule_value: str | None = None


class CommandRule(BaseModel):
    type: Literal["exact", "prefix", "binary"]
    value: str
    added_at: str
```

Add to `TaskExecutionState` (after `pending_step_review`):

```python
    pending_command_request: CommandApprovalRequest | None = None
    approved_commands: list[CommandRule] = Field(default_factory=list)
```

Add `shell_policy` to **both** `TaskCreateRequest` (backs `POST /v1/tasks`) and `TaskRecord` (the persisted model — the engine callback in Task 4 reads `task.shell_policy`):

```python
    shell_policy: ShellPolicy | None = None  # per-task override of AI_EDITOR_SHELL_POLICY
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/agentd-py && .venv/bin/python -m pytest tests/test_command_gate_models.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/agentd-py/agentd/domain/models.py services/agentd-py/tests/test_command_gate_models.py
git commit -m "feat(models): command-approval gate domain types"
```

---

## Task 2: State-machine transitions

**Files:**
- Modify: `agentd/domain/state_machine.py`
- Test: `tests/test_state_machine.py` (existing — add a test)

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_state_machine.py
from agentd.domain.models import TaskStatus
from agentd.domain.state_machine import can_transition


def test_command_decision_edges():
    assert can_transition(TaskStatus.EXECUTING, TaskStatus.AWAITING_COMMAND_DECISION)
    assert can_transition(TaskStatus.AWAITING_COMMAND_DECISION, TaskStatus.EXECUTING)
    assert can_transition(TaskStatus.AWAITING_COMMAND_DECISION, TaskStatus.FAILED)
    assert can_transition(TaskStatus.AWAITING_COMMAND_DECISION, TaskStatus.ABORTED)
    assert not can_transition(TaskStatus.AWAITING_COMMAND_DECISION, TaskStatus.SUCCEEDED)
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd services/agentd-py && .venv/bin/python -m pytest tests/test_state_machine.py::test_command_decision_edges -v`
Expected: FAIL (edge not in map).

- [ ] **Step 3: Implement**

In `agentd/domain/state_machine.py` `_TRANSITIONS`: add `TaskStatus.AWAITING_COMMAND_DECISION` to the `EXECUTING` outgoing set, and add a new entry:

```python
    TaskStatus.AWAITING_COMMAND_DECISION: {
        TaskStatus.EXECUTING,
        TaskStatus.FAILED,
        TaskStatus.ABORTED,
    },
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd services/agentd-py && .venv/bin/python -m pytest tests/test_state_machine.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/agentd-py/agentd/domain/state_machine.py services/agentd-py/tests/test_state_machine.py
git commit -m "feat(state-machine): AWAITING_COMMAND_DECISION edges"
```

---

## Task 3: `CommandRuleStore`

**Files:**
- Create: `agentd/tools/command_rules.py`
- Test: `tests/test_command_rules.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_command_rules.py
from pathlib import Path
from agentd.tools.command_rules import CommandRuleStore
from agentd.domain.models import CommandRule


def test_exact_prefix_binary_matching(tmp_path: Path):
    store = CommandRuleStore(tmp_path)
    store.add(CommandRule(type="exact", value="ruff check .", added_at="t"))
    store.add(CommandRule(type="prefix", value="python -c", added_at="t"))
    store.add(CommandRule(type="binary", value="pytest", added_at="t"))

    store.add(CommandRule(type="prefix", value="cat /etc/passwd", added_at="t"))

    # exact: full token-list equality
    assert store.matches("ruff check .")
    assert not store.matches("ruff check src")            # different token
    assert not store.matches("ruff check . --fix")        # extra token

    # prefix: leading token sublist
    assert store.matches('python -c "print(1)"')          # ["python","-c","print(1)"]
    assert not store.matches("python script.py")          # ["python","script.py"]

    # prefix is token-aware, NOT char startswith — no substring bleed
    assert store.matches("cat /etc/passwd -n")
    assert not store.matches("cat /etc/password-store/secret")

    # binary: basename of first token
    assert store.matches("pytest tests/test_x.py::t")
    assert store.matches("/usr/bin/pytest -q")
    assert not store.matches("pytestx -q")                # basename differs


def test_persist_and_reload(tmp_path: Path):
    store = CommandRuleStore(tmp_path)
    store.add(CommandRule(type="binary", value="pytest", added_at="t"))
    # New instance reads the same file
    reloaded = CommandRuleStore(tmp_path)
    assert reloaded.matches("pytest -q")


def test_add_is_deduped(tmp_path: Path):
    store = CommandRuleStore(tmp_path)
    store.add(CommandRule(type="binary", value="pytest", added_at="t1"))
    store.add(CommandRule(type="binary", value="pytest", added_at="t2"))
    assert len(store.load()) == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd services/agentd-py && .venv/bin/python -m pytest tests/test_command_rules.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```python
# agentd/tools/command_rules.py
"""Per-workspace persistent store of user-approved shell-command rules.

Backs the "Accept & remember (this workspace)" choice of the command-approval
gate. Rules are matched against the normalized "command + args" string.
"""
from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

from agentd.domain.models import CommandRule


def _tokenize(s: str) -> list[str]:
    try:
        return shlex.split(s)
    except ValueError:
        return s.split()


class CommandRuleStore:
    def __init__(self, workspace_path: str | Path) -> None:
        self._path = Path(workspace_path) / ".ai-editor" / "approved-commands.json"

    def load(self) -> list[CommandRule]:
        if not self._path.exists():
            return []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        return [CommandRule(**r) for r in raw if isinstance(r, dict)]

    @staticmethod
    def rule_matches(rule: CommandRule, cmd_tokens: list[str]) -> bool:
        """Token-aware match — NOT character startswith."""
        if not cmd_tokens:
            return False
        if rule.type == "binary":
            return Path(cmd_tokens[0]).name == rule.value
        rule_tokens = _tokenize(rule.value)
        if rule.type == "exact":
            return cmd_tokens == rule_tokens
        if rule.type == "prefix":
            return bool(rule_tokens) and cmd_tokens[: len(rule_tokens)] == rule_tokens
        return False

    def matches(self, command: str, args: list[str] | None = None) -> bool:
        # `args=None` → treat `command` as a full "cmd args" string (test/CLI use).
        cmd_tokens = _tokenize(command) if args is None else [command, *args]
        return any(self.rule_matches(r, cmd_tokens) for r in self.load())

    def add(self, rule: CommandRule) -> None:
        rules = self.load()
        if any(r.type == rule.type and r.value == rule.value for r in rules):
            return
        rules.append(rule)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps([r.model_dump() for r in rules], indent=2), encoding="utf-8",
        )
        os.replace(tmp, self._path)
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd services/agentd-py && .venv/bin/python -m pytest tests/test_command_rules.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/agentd-py/agentd/tools/command_rules.py services/agentd-py/tests/test_command_rules.py
git commit -m "feat(tools): CommandRuleStore with exact/prefix/binary matching"
```

---

## Task 4: Engine — approval callback + gate

**Files:**
- Modify: `agentd/orchestrator/engine.py`
- Test: `tests/test_command_gate_engine.py` (create)

**Reference:** mirror `_build_scope_callback` (engine.py:~1448–1494) and `_pending_scope_decisions` (engine.py:207).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_command_gate_engine.py
import asyncio
import pytest
from agentd.domain.models import ShellPolicy, CommandDecision, CommandRule
# Assumes a test helper that builds an AgentOrchestrator with InMemoryTaskStore +
# a task in EXECUTING. Reuse the fixtures used by tests/test_engine_scope_decision.py.
from tests.helpers import make_orchestrator_with_task  # existing helper pattern


@pytest.mark.asyncio
async def test_allow_all_skips_gate():
    orch, task = await make_orchestrator_with_task(shell_policy=ShellPolicy.ALLOW_ALL)
    cb = orch._build_command_approval_callback(task.task_id)
    decision = await cb("pytest", ["-q"], "services/agentd-py")
    assert decision.approve is True
    assert task.task_id not in orch._pending_command_decisions


@pytest.mark.asyncio
async def test_remembered_rule_skips_gate():
    orch, task = await make_orchestrator_with_task(shell_policy=ShellPolicy.ASK)
    task.execution_state.approved_commands.append(
        CommandRule(type="binary", value="pytest", added_at="t")
    )
    await orch._store.save(task)
    cb = orch._build_command_approval_callback(task.task_id)
    decision = await cb("pytest", ["-q"], "services/agentd-py")
    assert decision.approve is True


@pytest.mark.asyncio
async def test_ask_pauses_then_resumes_on_approval():
    orch, task = await make_orchestrator_with_task(shell_policy=ShellPolicy.ASK)
    cb = orch._build_command_approval_callback(task.task_id)
    gate = asyncio.create_task(cb("python", ["-c", "print(1)"], "services/agentd-py"))
    # Let the gate suspend on the future.
    for _ in range(50):
        await asyncio.sleep(0)
        if task.task_id in orch._pending_command_decisions:
            break
    fut = orch._pending_command_decisions[task.task_id]
    assert not fut.done()
    fut.set_result(CommandDecision(approve=True, remember=True, scope="prefix",
                                   rule_value="python -c"))
    decision = await gate
    assert decision.approve is True
    # remembered persisted to the per-task set
    reloaded = await orch._store.get(task.task_id)
    assert any(r.value == "python -c" for r in reloaded.execution_state.approved_commands)
```

> If `tests/helpers.py::make_orchestrator_with_task` does not exist, create it by copying the orchestrator/task construction from `tests/test_engine_scope_decision.py` and adding a `shell_policy` kwarg passed to the `AgentOrchestrator` constructor.

- [ ] **Step 2: Run to verify it fails**

Run: `cd services/agentd-py && .venv/bin/python -m pytest tests/test_command_gate_engine.py -v --timeout=30`
Expected: FAIL — `_build_command_approval_callback` / `_pending_command_decisions` missing.

- [ ] **Step 3: Implement**

In `AgentOrchestrator.__init__` add a `shell_policy: ShellPolicy = ShellPolicy.ASK` parameter and a `command_decision_timeout_sec: float = 0.0` parameter, then:

```python
        self._shell_policy = shell_policy
        self._command_decision_timeout_sec = max(0.0, command_decision_timeout_sec)
        self._pending_command_decisions: dict[str, asyncio.Future[CommandDecision]] = {}
```

Add the callback factory (mirrors `_build_scope_callback`):

```python
    def _build_command_approval_callback(self, task_id: str):
        from uuid import uuid4
        from datetime import datetime, timezone
        from agentd.tools.command_rules import CommandRuleStore

        async def _cb(command: str, args: list[str], cwd: str) -> CommandDecision:
            task = await self._store.get(task_id)

            policy = task.shell_policy or self._shell_policy
            if policy == ShellPolicy.ALLOW_ALL:
                return CommandDecision(approve=True)

            # Per-task + per-workspace remembered approvals (token-aware match,
            # via the same CommandRuleStore.rule_matches used for the JSON store).
            cmd_tokens = [command, *args]
            for rule in task.execution_state.approved_commands:
                if CommandRuleStore.rule_matches(rule, cmd_tokens):
                    return CommandDecision(approve=True)
            if CommandRuleStore(task.workspace_path).matches(command, args):
                return CommandDecision(approve=True)

            # ASK — pause + future + broadcast
            decision_id = uuid4().hex
            step_id = task.execution_state.current_step_id or ""
            future: asyncio.Future[CommandDecision] = (
                asyncio.get_event_loop().create_future()
            )
            self._pending_command_decisions[task_id] = future
            task.execution_state.pending_command_request = CommandApprovalRequest(
                decision_id=decision_id, command=command, args=args,
                cwd=cwd, step_id=step_id,
            )
            try:
                task = transition(task, TaskStatus.AWAITING_COMMAND_DECISION, "command gate")
            except ValueError:
                pass
            await self._store.save(task)
            self.broadcaster.broadcast(task_id, {
                "type": "command_approval_requested",
                "payload": {
                    "decision_id": decision_id, "command": command,
                    "args": args, "cwd": cwd, "step_id": step_id,
                },
            })

            decision = CommandDecision(approve=False)
            try:
                if self._command_decision_timeout_sec > 0:
                    decision = await asyncio.wait_for(
                        future, timeout=self._command_decision_timeout_sec
                    )
                else:
                    decision = await future
            except asyncio.TimeoutError:
                decision = CommandDecision(approve=False)
            finally:
                self._pending_command_decisions.pop(task_id, None)
                task = await self._store.get(task_id)
                task.execution_state.pending_command_request = None
                if task.status == TaskStatus.AWAITING_COMMAND_DECISION:
                    task = transition(task, TaskStatus.EXECUTING, "command decision received")
                if decision.approve and decision.remember:
                    import shlex
                    if decision.rule_value:
                        value = decision.rule_value
                    elif decision.scope == "binary":
                        value = command.rsplit("/", 1)[-1]
                    elif decision.scope == "exact":
                        value = shlex.join([command, *args])
                    else:  # prefix with no explicit value → lock command + first arg
                        _toks = [command, *args]
                        value = shlex.join(_toks[:2] if len(_toks) > 1 else _toks)
                    rule = CommandRule(
                        type=decision.scope, value=value,
                        added_at=datetime.now(timezone.utc).isoformat(),
                    )
                    task.execution_state.approved_commands.append(rule)
                    CommandRuleStore(task.workspace_path).add(rule)
                await self._store.save(task)

            return decision

        return _cb
```

Add the imports at the top of `engine.py` if missing: `CommandDecision, CommandApprovalRequest, CommandRule, ShellPolicy` from `agentd.domain.models`.

- [ ] **Step 4: Run to verify it passes**

Run: `cd services/agentd-py && .venv/bin/python -m pytest tests/test_command_gate_engine.py -v --timeout=30`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/agentd-py/agentd/orchestrator/engine.py services/agentd-py/tests/test_command_gate_engine.py services/agentd-py/tests/helpers.py
git commit -m "feat(engine): command-approval callback + AWAITING_COMMAND_DECISION gate"
```

---

## Task 5: Wire the callback into `run_command`; drop the allowlist

**Files:**
- Modify: `agentd/tools/registry.py`, `agentd/tools/shell.py`
- Test: `tests/test_tools_registry.py` (existing — adjust)

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_tools_registry.py
import pytest
from agentd.domain.models import CommandDecision


@pytest.mark.asyncio
async def test_run_command_consults_approval_callback(tmp_path):
    calls = []
    async def cb(command, args, cwd):
        calls.append((command, args))
        return CommandDecision(approve=False)
    reg = make_registry(tmp_path, command_approval_callback=cb)  # see helper note
    out = await reg.run_tool("run_command", {"command": "python", "args": ["-c", "1"]})
    assert calls == [("python", ["-c", "1"])]
    assert out.is_error
    assert "rejected" in out.output.lower()
```

> Use the existing `tests/test_tools_registry.py` construction helper; thread a `command_approval_callback` kwarg through it.

- [ ] **Step 2: Run to verify it fails**

Run: `cd services/agentd-py && .venv/bin/python -m pytest tests/test_tools_registry.py::test_run_command_consults_approval_callback -v`
Expected: FAIL — callback param not accepted / not consulted.

- [ ] **Step 3: Implement**

In `ToolRegistry.__init__` add a parameter `command_approval_callback=None` and store `self._command_approval_callback = command_approval_callback`. Remove the `AI_EDITOR_SHELL_ALLOWLIST` env read and `self._shell_allowlist` (lines ~43–47) and the allowlist mention in the tool schema string (~line 109,116).

Replace the `run_command` branch (registry.py:270–283) with:

```python
        if name == "run_command":
            from agentd.tools.shell import run_command
            raw_args = args.get("args", [])
            cmd_args = [str(a) for a in raw_args] if isinstance(raw_args, list) else []
            command = str(args.get("command", ""))
            cwd = str(args.get("cwd", "")) or ""
            if self._command_approval_callback is not None:
                decision = await self._command_approval_callback(command, cmd_args, cwd)
                if not decision.approve:
                    from agentd.tools.contracts import ToolOutput
                    return ToolOutput(
                        output=(
                            f"Command rejected by user: {command} "
                            f"{' '.join(cmd_args)}".strip()
                            + ". Try a different approach (e.g. a static check)."
                        ),
                        is_error=True,
                    )
            return await run_command(
                command=command,
                args=cmd_args,
                shadow_root=self._shadow_root,
                real_workspace_path=self._real_workspace_path,
                binary_name_override=Path(command).name,
            )
```

> `ToolOutput` import: use the same import path the rest of `registry.py`/`shell.py` uses (check the top of `shell.py` — it imports from `agentd.tools.contracts`). Adjust the import line above to match.

In `agentd/tools/shell.py` `run_command`: remove the `allowlist: set[str]` parameter and the `check_name not in allowlist` block (lines ~38, 46–53). Keep `binary_name_override` and binary resolution.

Update any other `run_command(` call sites and the `make_registry` test helper to drop `allowlist=`.

- [ ] **Step 4: Run to verify it passes**

Run: `cd services/agentd-py && .venv/bin/python -m pytest tests/test_tools_registry.py tests/test_tools_env.py -v`
Expected: PASS.

- [ ] **Step 5: Wire the callback in the engine's tool-loop setup**

Where the engine constructs the `ToolRegistry` for step execution (search `ToolRegistry(` in `engine.py`), pass `command_approval_callback=self._build_command_approval_callback(task_id)`. Run the orchestrator verify-flow tests:

Run: `cd services/agentd-py && .venv/bin/python -m pytest tests/test_orchestrator_verify_flow.py -v --timeout=60`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add services/agentd-py/agentd/tools/registry.py services/agentd-py/agentd/tools/shell.py services/agentd-py/agentd/orchestrator/engine.py services/agentd-py/tests/test_tools_registry.py
git commit -m "feat(tools): gate run_command via approval callback; remove static allowlist"
```

---

## Task 6: `POST /tasks/{id}/command-decision`

**Files:**
- Modify: `agentd/api/routes.py`, `agentd/domain/models.py` (response model)
- Test: `tests/test_command_decision_api.py` (create)

**Reference:** mirror `post_scope_decision` (routes.py:427–492).

- [ ] **Step 1: Add response model**

In `models.py` near `ScopeDecisionResponse`:

```python
class CommandDecisionResponse(BaseModel):
    task_id: str
    status: "TaskStatus"
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_command_decision_api.py
import pytest
from httpx import AsyncClient, ASGITransport
from agentd.chat.app_factory import build_app  # test-only app


@pytest.mark.asyncio
async def test_command_decision_resolves_future():
    app, orch = build_app()  # adapt to however build_app exposes the orchestrator
    # Arrange: a task in AWAITING_COMMAND_DECISION with a pending future.
    # (Construct via orch like the scope-decision api test does.)
    ...
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.post(f"/v1/tasks/{task_id}/command-decision",
                          json={"approve": True, "remember": False, "scope": "exact"})
    assert r.status_code == 200
    assert orch._pending_command_decisions[task_id].result().approve is True


@pytest.mark.asyncio
async def test_command_decision_409_when_not_awaiting():
    app, orch = build_app()
    # task in EXECUTING (no pending command)
    ...
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post(f"/v1/tasks/{task_id}/command-decision",
                          json={"approve": True})
    assert r.status_code == 409
```

> Model the construction on `tests/test_scope_decision_api.py`. Fill the `...` by creating a task, setting status to `AWAITING_COMMAND_DECISION`, and seeding `orch._pending_command_decisions[task_id]` with a fresh future.

- [ ] **Step 3: Run to verify it fails**

Run: `cd services/agentd-py && .venv/bin/python -m pytest tests/test_command_decision_api.py -v --timeout=30`
Expected: FAIL — route 404.

- [ ] **Step 4: Implement the route**

In `routes.py`, near `_in_flight_scope`, add `_in_flight_command: set[str] = set()`. Add (mirroring `post_scope_decision`):

```python
    @router.post("/tasks/{task_id}/command-decision", response_model=CommandDecisionResponse)
    async def post_command_decision(
        task_id: str, request: CommandDecision,
    ) -> CommandDecisionResponse:
        if task_id in _in_flight_command:
            raise HTTPException(status_code=409, detail=f"Command decision already in progress for task {task_id}")
        _in_flight_command.add(task_id)
        try:
            try:
                task = await store.get(task_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            if task.status != TaskStatus.AWAITING_COMMAND_DECISION:
                raise HTTPException(status_code=409, detail=f"Task {task_id} is not awaiting a command decision (status={task.status})")
            future = orchestrator._pending_command_decisions.get(task_id)
            if future is None or future.done():
                raise HTTPException(status_code=409, detail="No pending command decision for this task")
            future.set_result(request)
            return CommandDecisionResponse(task_id=task_id, status=TaskStatus.EXECUTING)
        finally:
            _in_flight_command.discard(task_id)
```

Import `CommandDecision, CommandDecisionResponse` in `routes.py`.

- [ ] **Step 5: Run to verify it passes**

Run: `cd services/agentd-py && .venv/bin/python -m pytest tests/test_command_decision_api.py -v --timeout=30`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add services/agentd-py/agentd/api/routes.py services/agentd-py/agentd/domain/models.py services/agentd-py/tests/test_command_decision_api.py
git commit -m "feat(api): POST /tasks/{id}/command-decision route"
```

---

## Task 7: Read policy at construction + per-task override

**Files:**
- Modify: `agentd/main.py`, `agentd/chat/app_factory.py`, `agentd/api/routes.py` (`create_task`)

- [ ] **Step 1: Read env at the orchestrator construction site**

In `agentd/main.py` (and `app_factory.py` if it builds an orchestrator), where `AgentOrchestrator(...)` is built, add:

```python
    shell_policy=ShellPolicy(os.environ.get("AI_EDITOR_SHELL_POLICY", "ask")),
    command_decision_timeout_sec=float(os.environ.get("AI_EDITOR_COMMAND_DECISION_TIMEOUT_SEC", "0")),
```

(import `ShellPolicy`, `os`).

- [ ] **Step 2: Per-task override in `create_task`**

The `TaskRecord.shell_policy` field (Task 1) and the callback's `policy = task.shell_policy or self._shell_policy` resolution (Task 4) already exist. Here, just wire it through: in `routes.py::create_task` (line 183), pass `shell_policy=request.shell_policy` into the `TaskRecord(...)` constructor so a per-task override reaches the engine.

- [ ] **Step 3: Test**

Run: `cd services/agentd-py && .venv/bin/python -m pytest tests/test_command_gate_engine.py -v --timeout=30`
Expected: PASS (allow_all test still green; add a per-task-override variant if useful).

- [ ] **Step 4: Commit**

```bash
git add services/agentd-py/agentd/main.py services/agentd-py/agentd/chat/app_factory.py services/agentd-py/agentd/api/routes.py services/agentd-py/agentd/domain/models.py
git commit -m "feat(engine): AI_EDITOR_SHELL_POLICY env + per-task shell_policy override"
```

---

## Task 8: Remove `AI_EDITOR_SHELL_ALLOWLIST` from config/docs

**Files:**
- Modify: `scripts/stress/start-backend.sh`, `.env`, `CLAUDE.md`

- [ ] **Step 1: Remove the env line + script export**

- In `.env`: delete the `AI_EDITOR_SHELL_ALLOWLIST=...` line (line 88).
- In `scripts/stress/start-backend.sh`: remove the `export AI_EDITOR_SHELL_ALLOWLIST=...` line (search it). Add `export AI_EDITOR_SHELL_POLICY="${AI_EDITOR_SHELL_POLICY:-ask}"` in the same env block.
- In `CLAUDE.md` "Tool loop" config section: replace the `AI_EDITOR_SHELL_ALLOWLIST` bullet with `AI_EDITOR_SHELL_POLICY` (`ask` default / `allow_all`) + `AI_EDITOR_COMMAND_DECISION_TIMEOUT_SEC` and a one-line description of the gate.

- [ ] **Step 2: Verify nothing references the removed var**

Run: `cd services/agentd-py && grep -rn "AI_EDITOR_SHELL_ALLOWLIST\|_shell_allowlist\|allowlist" agentd/ | grep -v test`
Expected: no remaining functional references (only comments/history, if any — remove those too).

- [ ] **Step 3: Commit**

```bash
git add .env scripts/stress/start-backend.sh CLAUDE.md
git commit -m "chore: replace AI_EDITOR_SHELL_ALLOWLIST with AI_EDITOR_SHELL_POLICY"
```

> **Checkpoint:** the gate is now fully functional and testable via `curl` (submit a task in `ask` mode, watch for the `command_approval_requested` SSE event, `POST /command-decision`).

---

## Task 9: editor-client contracts

**Files:**
- Modify: `apps/editor-client/src/contracts/task-contracts.ts`, `apps/editor-client/src/client/http-backend-client.ts`
- Test: `apps/editor-client/src/contracts/__tests__/command-decision.test.ts` (create, if that test dir convention exists; else colocate)

- [ ] **Step 1: Write the failing test**

```typescript
import { describe, it, expect } from "vitest";
import { CommandDecisionSchema } from "../task-contracts";

describe("CommandDecisionSchema", () => {
  it("parses a remember+prefix decision", () => {
    const d = CommandDecisionSchema.parse({ approve: true, remember: true, scope: "prefix", ruleValue: "python -c" });
    expect(d.scope).toBe("prefix");
  });
  it("defaults remember/scope", () => {
    const d = CommandDecisionSchema.parse({ approve: false });
    expect(d.remember).toBe(false);
    expect(d.scope).toBe("exact");
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npm run -w @ai-editor/editor-client test -- command-decision`
Expected: FAIL — `CommandDecisionSchema` not exported.

- [ ] **Step 3: Implement**

In `task-contracts.ts`:

```typescript
export const CommandDecisionSchema = z.object({
  approve: z.boolean(),
  remember: z.boolean().default(false),
  scope: z.enum(["exact", "prefix", "binary"]).default("exact"),
  ruleValue: z.string().optional(),
});
export type CommandDecision = z.infer<typeof CommandDecisionSchema>;
```

`ruleValue` carries the chosen remember-scope literal — prefix: `shlexJoin` of the locked leading tokens; exact: the full `shlexJoin`; binary: omitted (the engine derives the basename). This mirrors the backend's `shlex.join`/`shlex.split` round-trip.

Add to the `StreamEvent` union:

```typescript
  | { type: "command_approval_requested"; payload: { decision_id: string; command: string; args: string[]; cwd: string; step_id: string } }
```

Add `"command_card"` to the `ChatMessageSchema` `type` enum.

Add to `BackendTaskClient` interface and `HttpBackendClient` (http-backend-client.ts) — note snake_case mapping (`ruleValue` → `rule_value`):

```typescript
  async sendCommandDecision(taskId: string, decision: CommandDecision): Promise<void> {
    await this.fetchJson(`/v1/tasks/${encodeURIComponent(taskId)}/command-decision`, {
      method: "POST",
      body: JSON.stringify({
        approve: decision.approve,
        remember: decision.remember,
        scope: decision.scope,
        rule_value: decision.ruleValue ?? null,
      }),
    });
  }
```

- [ ] **Step 4: Run to verify it passes; build**

Run: `npm run -w @ai-editor/editor-client test -- command-decision`
Expected: PASS.
Run: `npm run -w @ai-editor/editor-client build`
Expected: build succeeds (so the extension typechecks against fresh `dist`).

- [ ] **Step 5: Commit**

```bash
git add apps/editor-client/src
git commit -m "feat(editor-client): command-decision contract + sendCommandDecision"
```

---

## Task 10: VS Code controller — handle event + post decision

**Files:**
- Modify: `apps/vscode-extension/src/controller.ts`
- Test: `apps/vscode-extension/test/controller.test.ts` (existing — add a case + stub `sendCommandDecision`)

- [ ] **Step 1: Write the failing test**

```typescript
it("posts a command decision from a command_card action", async () => {
  const sent: any[] = [];
  const client = makeStubClient({ sendCommandDecision: async (id, d) => { sent.push([id, d]); } });
  const controller = makeController(client);
  await controller.handleCommandDecisionFromChat("task-1", { approve: true, remember: true, scope: "prefix", ruleValue: "python -c" });
  expect(sent).toEqual([["task-1", { approve: true, remember: true, scope: "prefix", ruleValue: "python -c" }]]);
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npm run -w @ai-editor/vscode-extension test -- controller`
Expected: FAIL — method/handler missing.

- [ ] **Step 3: Implement**

In `controller.ts`: in the stream event handler (where `task_status_changed` / `scope_extension_requested` are handled), add a `command_approval_requested` branch that renders a `command_card` chat message with payload `{decision_id, command, args, cwd, step_id, taskId}`. Add:

```typescript
  async handleCommandDecisionFromChat(taskId: string, decision: CommandDecision): Promise<void> {
    await this.client.sendCommandDecision(taskId, decision);
  }
```

Add `sendCommandDecision` to the controller's `BackendTaskClient` stub type in the test.

- [ ] **Step 4: Run to verify it passes**

Run: `npm run -w @ai-editor/vscode-extension test -- controller`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/vscode-extension/src/controller.ts apps/vscode-extension/test/controller.test.ts
git commit -m "feat(vscode): handle command_approval_requested + post decision"
```

---

## Task 11: Command card UI (webview)

**Files:**
- Modify: `apps/vscode-extension/src/chat-panel.ts`, `apps/vscode-extension/media/chat.js`

- [ ] **Step 1: Render the card**

In `media/chat.js`, add a renderer for `type === "command_card"` messages. Show the command **tokenized** (each token as a chip). Provide a **scope picker**:

- `Exact` — lock the full command; `ruleValue = shlexJoin(allTokens)`.
- `Prefix` — a token-cut control: clicking after token *k* locks the first *k* tokens. Render an `auto-approves: <leading tokens> …` preview so the breadth is explicit; `ruleValue = shlexJoin(leadingTokens)`. Show a warning that nudges toward `Exact` when the token immediately after the cut is a payload flag (`-c`, `-e`, `--eval`) — a prefix there permits arbitrary code.
- `Binary` — `any <basename>`; `ruleValue` omitted (the engine derives the basename).

Three buttons: `Reject` (`{approve:false}`), `Accept once` (`{approve:true, remember:false}`), `Accept & remember` (`{approve:true, remember:true, scope:<picked>, ruleValue:<derived>}`). On click, `postMessage({ type: "commandDecision", taskId, decision })`.

`shlexJoin` in JS: join tokens with a space, wrapping any token containing whitespace or shell metacharacters in single quotes (mirrors Python `shlex.join`, so the value round-trips through the backend's `shlex.split`).

In `chat-panel.ts`, handle the inbound `commandDecision` webview message → call `controller.handleCommandDecisionFromChat(taskId, decision)`; on resolve, mark the card resolved (disable buttons), mirroring how the scope/validation cards resolve.

- [ ] **Step 2: Build + manual verification**

Run: `npm run build`
Then reload the extension dev host. Submit a task in `ask` mode that runs a command (e.g. a step whose verify runs `pytest`/`python -c`). Confirm: the card appears with the command, picking a scope + `Accept & remember` resumes execution, a second matching command does **not** re-prompt, and `Reject` produces a tool-error the agent adapts to.

> UI cannot be unit-asserted end-to-end here; this step is manual. State explicitly in the PR whether the golden path + reject path were exercised in the dev host.

- [ ] **Step 3: Commit**

```bash
git add apps/vscode-extension/src/chat-panel.ts apps/vscode-extension/media/chat.js
git commit -m "feat(vscode): command approval card with scope picker"
```

---

## Task 12: End-to-end integration test

**Files:**
- Test: `tests/test_command_gate_integration.py` (create)

- [ ] **Step 1: Write the test**

```python
# tests/test_command_gate_integration.py
import asyncio
import pytest
from agentd.domain.models import ShellPolicy, CommandDecision


@pytest.mark.asyncio
async def test_toolloop_step_gates_run_command(tmp_path):
    """A scripted step that calls run_command pauses at the gate; approving
    runs the command, rejecting returns a tool error the agent can read."""
    # Build orchestrator (ASK) + a scripted engine whose step emits a
    # run_command tool_call, then verify_done. Mirror tests/test_orchestrator_verify_flow.py.
    ...
    # Drive: start the task; poll until status == AWAITING_COMMAND_DECISION;
    # POST-equivalent: orch._pending_command_decisions[task_id].set_result(
    #     CommandDecision(approve=True))
    # assert the command actually executed and the task progressed.
```

> Fill `...` from `tests/test_orchestrator_verify_flow.py` (scripted engine + run_task driver).

- [ ] **Step 2: Run**

Run: `cd services/agentd-py && .venv/bin/python -m pytest tests/test_command_gate_integration.py -v --timeout=60`
Expected: PASS.

- [ ] **Step 3: Full backend suite (regression)**

Run: `cd services/agentd-py && .venv/bin/python -m pytest tests/test_command_gate_models.py tests/test_command_rules.py tests/test_command_gate_engine.py tests/test_command_decision_api.py tests/test_command_gate_integration.py tests/test_tools_registry.py tests/test_state_machine.py tests/test_orchestrator_verify_flow.py -v --timeout=120`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add services/agentd-py/tests/test_command_gate_integration.py
git commit -m "test(command-gate): end-to-end ToolLoop gate integration"
```

---

## Self-review notes (for the implementer)

- **Spec coverage:** policy model (T1,T7,T8), gate (T4,T5), persistence/match (T3), reject→tool-error (T5), timeout (T4,T7), route+SSE (T4 emits event, T6 route), client+UI (T9–T11), allowlist removal (T5,T8), enforcement scope = task ToolLoop (T5 wiring), testing (each task + T12). 
- **Type consistency:** `CommandDecision{approve,remember,scope,rule_value}` is identical across models (T1), engine (T4), route (T6), and the TS schema (T9, camelCase `ruleValue` ↔ snake `rule_value`). `CommandRule{type,value,added_at}` identical in T1/T3/T4. Callback signature `(command, args, cwd) -> CommandDecision` identical in T4/T5.
- **Deferred:** gate-consolidation refactor is NOT in this plan (roadmap → Deferred Refactors).
