# Verify Phase Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the `test_command` null short-circuit from the tool loop so the verify phase always runs, and tighten the planning prompt so `test_command` is only set when a test file is a target of that step.

**Architecture:** Three files change — the tool loop removes the early-return and enriches the verify context message, the tool prompt removes the null=skip rule, and the planning prompt tightens the `test_command` rule. TDD: update/add tests first, then implement.

**Tech Stack:** Python 3.11+, pytest-asyncio, agentd-py worktree at `.worktrees/feat-agentic-planning/services/agentd-py/`

---

## Files

| File | Change |
|------|--------|
| `tests/test_orchestrator_verify_flow.py` | Update 2 existing tests; add 2 new tests |
| `agentd/tools/loop.py` | Remove short-circuit (lines 347–359); enrich verify context message (lines 364–372) |
| `agentd/reasoning/tool_prompts.py` | Remove null=skip rule (line 116); update verify rules block (lines 114–117) and Variant 4 description (line 216) |
| `agentd/planning/prompts.py` | Tighten TEST DISCOVERY block (lines 82–99) and BEFORE-EMIT checklist (line 106); update REVISION MODE block (lines 137–142) |

All paths relative to `.worktrees/feat-agentic-planning/services/agentd-py/`.

---

## Task 1: Update existing tests to reflect new behavior

The two existing tests that use `test_command=None` currently pass because the short-circuit
returns `verified=True` immediately. After the refactor they will fail — the loop will enter
verify phase and block waiting for `verify_done`. Update them first so they become the failing
red bar that the implementation must satisfy.

**Files:**
- Modify: `tests/test_orchestrator_verify_flow.py`

- [ ] **Step 1: Update `test_no_test_command_returns_verified_immediately`**

Rename the test and add a `verify_done` response so the scripted engine satisfies the verify
phase. The assertion stays the same — task reaches `READY_FOR_REVIEW`.

Replace the entire test (lines 63–84) with:

```python
@pytest.mark.asyncio
async def test_null_test_command_always_enters_verify(tmp_path: Path) -> None:
    """Steps without test_command still enter verify phase — agent must emit verify_done."""
    ws = tmp_path / "ws"
    ws.mkdir()

    patch = _make_patch_raw()
    patch_ops = patch["candidates"][0]["patch_ops"]
    reasoning = ScriptedReasoningEngine(
        plan=_make_plan_raw(test_command=None),
        patches=[patch],
        tool_step_responses=[
            {"type": "emit_patch", "thought": "create file", "patch_ops": patch_ops},
            {"type": "verify_done", "thought": "no tests applicable", "verified": True, "test_output": ""},
        ],
    )
    orchestrator, store = _make_orchestrator(reasoning, tmp_path)
    task = TaskRecord(task_id="task-1", goal="create hello.py", workspace_path=str(ws))
    await store.create(task)

    initialized = await orchestrator.run_task("task-1")
    assert initialized.status == TaskStatus.AWAITING_PLAN_APPROVAL
    result = await orchestrator.continue_task("task-1", feedback=None)
    assert result.status == TaskStatus.READY_FOR_REVIEW
    assert "hello.py" in result.modified_files
```

- [ ] **Step 2: Update `test_patch_apply_failure_stays_in_explore`**

This test also uses `test_command=None`. After the good patch applies, the loop enters verify
phase — add a `verify_done` at the end of `tool_step_responses`. Find the test starting at line
143 and add one entry at the end of `tool_step_responses`:

```python
            # After good patch, enter verify and complete
            {"type": "verify_done", "thought": "no tests", "verified": True, "test_output": ""},
```

The full `tool_step_responses` list becomes:

```python
        tool_step_responses=[
            {"type": "emit_patch", "thought": "bad patch", "patch_ops": bad_ops},
            # Agent sees failure in history, corrects:
            {"type": "emit_patch", "thought": "corrected", "patch_ops": good_ops},
            {"type": "verify_done", "thought": "no tests", "verified": True, "test_output": ""},
        ],
```

- [ ] **Step 3: Run tests to confirm they now fail**

```bash
cd .worktrees/feat-agentic-planning/services/agentd-py
source .venv/bin/activate
pytest tests/test_orchestrator_verify_flow.py -x -q 2>&1 | head -40
```

Expected: `FAILED` on `test_null_test_command_always_enters_verify` and
`test_patch_apply_failure_stays_in_explore` (loop short-circuits before the scripted
`verify_done` is consumed, causing `ScriptedReasoningEngine` to have leftover responses or the
loop to return early without consuming them).

---

## Task 2: Add two new tests

Before implementing, write the tests that verify the new behaviours we're adding.

**Files:**
- Modify: `tests/test_orchestrator_verify_flow.py`

- [ ] **Step 1: Add test for verify context message content**

Append after the last test in the file:

```python
@pytest.mark.asyncio
async def test_verify_context_message_contains_touched_files_and_strategy(tmp_path: Path) -> None:
    """Patch-apply context message includes touched_files and testing_strategy."""
    ws = tmp_path / "ws"
    ws.mkdir()

    captured_histories: list[list[dict]] = []

    class _CapturingEngine(ScriptedReasoningEngine):
        async def create_tool_step(
            self,
            step_context: dict,
            history: list[dict],
            tool_definitions: list[dict],
        ) -> dict:
            captured_histories.append(list(history))
            return await super().create_tool_step(step_context, history, tool_definitions)

    patch = _make_patch_raw()
    patch_ops = patch["candidates"][0]["patch_ops"]
    plan = _make_plan_raw(test_command=None)
    plan["steps"][0]["testing_strategy"] = "run vitest"

    reasoning = _CapturingEngine(
        plan=plan,
        patches=[patch],
        tool_step_responses=[
            {"type": "emit_patch", "thought": "create", "patch_ops": patch_ops},
            {"type": "verify_done", "thought": "ok", "verified": True, "test_output": ""},
        ],
    )
    orchestrator, store = _make_orchestrator(reasoning, tmp_path)
    task = TaskRecord(task_id="task-ctx", goal="create", workspace_path=str(ws))
    await store.create(task)

    await orchestrator.run_task("task-ctx")
    await orchestrator.continue_task("task-ctx", feedback=None)

    # The second create_tool_step call (verify phase) receives a history that includes
    # the patch-apply notification. Find it.
    patch_apply_msgs = [
        msg
        for history in captured_histories
        for msg in history
        if isinstance(msg.get("content"), str) and "Patch applied successfully" in msg["content"]
    ]
    assert patch_apply_msgs, "No patch-apply message found in any captured history"
    content = patch_apply_msgs[0]["content"]
    assert "hello.py" in content, f"touched file missing from verify context: {content}"
    assert "run vitest" in content, f"testing_strategy missing from verify context: {content}"
```

- [ ] **Step 2: Add test that verify_done with empty test_output is accepted when null test_command**

```python
@pytest.mark.asyncio
async def test_verify_done_empty_output_accepted_when_no_test_command(tmp_path: Path) -> None:
    """verify_done(verified=True, test_output='') is valid when step has no test_command."""
    ws = tmp_path / "ws"
    ws.mkdir()

    patch = _make_patch_raw()
    patch_ops = patch["candidates"][0]["patch_ops"]
    reasoning = ScriptedReasoningEngine(
        plan=_make_plan_raw(test_command=None),
        patches=[patch],
        tool_step_responses=[
            {"type": "emit_patch", "thought": "done", "patch_ops": patch_ops},
            {"type": "verify_done", "thought": "pure config, nothing to test", "verified": True, "test_output": ""},
        ],
    )
    orchestrator, store = _make_orchestrator(reasoning, tmp_path)
    task = TaskRecord(task_id="task-empty", goal="create", workspace_path=str(ws))
    await store.create(task)

    await orchestrator.run_task("task-empty")
    result = await orchestrator.continue_task("task-empty", feedback=None)
    assert result.status == TaskStatus.READY_FOR_REVIEW
```

- [ ] **Step 3: Run all new tests to confirm they fail**

```bash
pytest tests/test_orchestrator_verify_flow.py -x -q 2>&1 | head -40
```

Expected: new tests fail (loop still short-circuits, never sends patch-apply message with
touched files, and `verify_done` with empty output from a null-test_command step short-circuits
before `verify_done` is ever received).

---

## Task 3: Remove short-circuit and enrich verify context message (`tools/loop.py`)

**Files:**
- Modify: `agentd/tools/loop.py:347–377`

- [ ] **Step 1: Remove the short-circuit block and replace verify context message**

Replace lines 347–372 (from `# Short-circuit if no verify needed` through the closing `}`
of `history.append`) with:

```python
                # Always enter verify phase — execution agent decides what to run
                phase = "verify"
                last_verify_run_errored = False
                touched_files_str = ", ".join(all_touched_files) or "none"
                testing_strategy = step.testing_strategy or "not specified"
                test_cmd_hint = step.test_command or "none — discover from testing_strategy and touched files"
                history.append({
                    "role": "tool_result", "tool": "_patch_apply",
                    "content": (
                        "Patch applied successfully. Entering VERIFY PHASE.\n"
                        f"Touched files: {touched_files_str}\n"
                        f"testing_strategy: {testing_strategy}\n"
                        f"test_command hint: {test_cmd_hint}\n"
                        "Run linters then tests. Emit verify_done when all pass, "
                        "or emit_patch again to correct failures."
                    ),
                })
```

The `self._broadcaster.broadcast(...)` call and `continue` that follow remain unchanged.

- [ ] **Step 2: Run tests**

```bash
pytest tests/test_orchestrator_verify_flow.py -x -q 2>&1 | head -40
```

Expected: all tests in `test_orchestrator_verify_flow.py` pass now.

- [ ] **Step 3: Run full test suite to check for regressions**

```bash
pytest tests/ -x -q 2>&1 | tail -20
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
cd .worktrees/feat-agentic-planning/services/agentd-py
git add tests/test_orchestrator_verify_flow.py agentd/tools/loop.py
git commit -m "refactor(tool-loop): remove test_command null short-circuit; always enter verify phase"
```

---

## Task 4: Update tool prompt verify rules (`reasoning/tool_prompts.py`)

**Files:**
- Modify: `agentd/reasoning/tool_prompts.py:114–117,216`

- [ ] **Step 1: Replace verify phase rules block**

Replace lines 114–117:

```
  Rules:
    - You MUST run at least one linter AND one test command before verify_done(verified=true)
    - If this step has no test_command hint, emit verify_done(verified=true) immediately
    - Never claim verified=true without actually running the checks
```

With:

```
  Rules:
    - Use testing_strategy and touched files to determine what to run
    - Run static analysis first (fast): ruff check, mypy, tsc --noEmit, cargo check
    - Then run tests: pytest, vitest, cargo test, npm test
    - If test_command hint is provided, prefer it — but verify the binary exists first (find_binary)
    - If test_command hint is null, infer what to run from testing_strategy and touched file extensions
    - If nothing is testable (pure docs/config change with no compilation step), emit verify_done(verified=true) immediately
    - Never claim verified=true without actually running at least one check on a code-touching step
    - Use find_binary / setup_env if a binary is missing before concluding nothing can be run
```

- [ ] **Step 2: Update Variant 4 description (line 216)**

Replace:

```
  Use after ALL linters and tests pass. Or immediately if no test_command is set.
```

With:

```
  Use after ALL linters and tests pass. Or immediately if this step only touches non-code files (docs, config).
```

- [ ] **Step 3: Run tests**

```bash
pytest tests/ -x -q 2>&1 | tail -10
```

Expected: all tests pass (prompt changes don't affect unit tests).

- [ ] **Step 4: Commit**

```bash
git add agentd/reasoning/tool_prompts.py
git commit -m "refactor(tool-prompts): remove null=skip verify rule; agent discovers what to run"
```

---

## Task 5: Tighten planning prompt (`planning/prompts.py`)

**Files:**
- Modify: `agentd/planning/prompts.py:82–106,137–142`

- [ ] **Step 1: Replace TEST DISCOVERY block (lines 82–99)**

Replace:

```
TEST DISCOVERY (for test_command field):
• For each source file you intend to modify, search for its companion test file using these
  naming conventions before emitting a plan:
  - Python:     tests/test_<stem>.py  or  tests/<stem>_test.py
  - Rust:       <module>/<stem>_tests.rs  (inline #[cfg(test)] blocks) or tests/<stem>.rs
  - TypeScript: <stem>.test.ts  or  <stem>.spec.ts (same directory or __tests__/)
• If you find the test file, set test_command to a focused command that exercises the relevant
  tests (e.g. "pytest tests/test_auth.py -x" or "cargo test auth::tests").
  Do NOT use ::function_name qualifiers — the exact test function name is decided during
  implementation, not planning. Run at the file level so all tests in the file are collected.
• If you are creating a new test file as part of the task, that test file MUST be listed as a
  target (with intent "new") in the SAME step as the source change. Set test_command to run
  those tests. Never create a test file in a separate step.
• Only set test_command when:
  a) the test file path appeared in a tool call result this session (EXISTING), OR
  b) the test file is listed as a "new" target in the same step.
• Never invent a test path you have not seen or aren't creating. Leave test_command null if
  uncertain.
```

With:

```
TEST DISCOVERY (for test_command field):
• Set test_command ONLY when a test file is an explicit target of this step — either:
  a) listed with intent "existing" (you are modifying an existing test file), OR
  b) listed with intent "new" (you are creating a new test file in this step).
• If a step only touches source files and the companion test file is updated in a separate
  step, leave test_command null. The execution agent handles verification using testing_strategy.
• When test_command is set, choose the fastest command that exercises the changed test file:
  - Python:     "pytest tests/test_<stem>.py -x -q"
  - Rust:       "cargo test" (inline tests run automatically)
  - TypeScript: "npx vitest run test/<stem>.test.ts" or "npm test -- --run"
  Do NOT use ::function_name qualifiers. Run at the file level.
• Never invent a test path you have not seen or aren't creating. Leave test_command null if
  uncertain.
• If you are creating a new test file, it MUST be listed as a target (intent "new") in the
  SAME step as the source change. Never create a test file in a separate step.
```

- [ ] **Step 2: Update BEFORE-EMIT checklist item (line 106)**

Replace:

```
□ test_command (if set) points to a file that is either EXISTING (seen in a tool result) or NEW (a target in this step).
```

With:

```
□ test_command (if set) points to a file listed in targets for this step (intent "existing" or "new") — not merely a file read during exploration.
```

- [ ] **Step 3: Update REVISION MODE test_command rules (lines 137–142)**

Replace:

```
TEST COMMAND IN REVISIONS:
Every revised_step must include test_command when the step creates or targets
a test file. Apply the same TEST DISCOVERY rules as for a new plan:
- If a test file is a target (intent "new"): set test_command to run that file.
- If the original failed step had a test_command and the revised step still
  targets the same test file: preserve that test_command unchanged.
- Never omit test_command when a test file is present among the targets.
```

With:

```
TEST COMMAND IN REVISIONS:
Apply the same TEST DISCOVERY rules as for a new plan:
- Set test_command only when a test file is listed in targets for the revised step.
- If the original step had a test_command and the revised step still targets the
  same test file: preserve that test_command unchanged.
- If the revised step no longer targets a test file, set test_command to null.
- Never omit test_command when a test file is present among the revised step's targets.
```

- [ ] **Step 4: Run full test suite**

```bash
pytest tests/ -x -q 2>&1 | tail -10
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add agentd/planning/prompts.py
git commit -m "refactor(planning-prompt): test_command only when test file is a step target"
```
