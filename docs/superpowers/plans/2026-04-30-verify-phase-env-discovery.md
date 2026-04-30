# Verify-Phase Environment Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace engine-side per-step validation with an agent-owned verify phase inside `ToolLoop` — the agent discovers test binaries, sets up environments, runs linters and tests, and self-corrects before signalling success.

**Architecture:** `emit_patch` becomes a phase checkpoint — ToolLoop applies the patch inline and continues in verify mode. New tools `find_binary` and `setup_env` give the agent binary discovery and real-workspace env installation. `PatchResult` is removed; engine always receives `VerifyResult | PlanHandoff`.

**Tech Stack:** Python 3.13, asyncio, uv/pip/npm/cargo, pydantic v2, pytest

**Spec:** `docs/superpowers/specs/2026-04-30-verify-phase-env-discovery-design.md`

**Working directory:** `.worktrees/feat-agentic-planning/services/agentd-py`

---

### Task 1: Domain models — `max_verify_calls_per_step`

**Files:**
- Modify: `agentd/domain/models.py`

- [ ] **Step 1: Add the field**

In `TaskBudget` (line ~60), add after `max_delta_replans`:

```python
max_verify_calls_per_step: int = 4
```

- [ ] **Step 2: Verify existing tests still pass**

```bash
cd .worktrees/feat-agentic-planning/services/agentd-py
python -m pytest tests/test_planning_domain_models.py tests/test_state_machine.py -x -q
```

Expected: all pass (field has a default, no existing test breaks).

- [ ] **Step 3: Commit**

```bash
git add agentd/domain/models.py
git commit -m "feat(domain): add max_verify_calls_per_step to TaskBudget"
```

---

### Task 2: `find_binary` — locate executables on real filesystem

**Files:**
- Create: `agentd/tools/env.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_tools_env.py`:

```python
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agentd.tools.env import find_binary
from agentd.tools.registry import ToolOutput


@pytest.mark.asyncio
async def test_find_binary_finds_python(tmp_path: Path) -> None:
    result = await find_binary(name="python3", real_workspace=tmp_path)
    assert not result.is_error
    assert "python3" in result.output


@pytest.mark.asyncio
async def test_find_binary_not_found(tmp_path: Path) -> None:
    result = await find_binary(name="__nonexistent_binary_xyz__", real_workspace=tmp_path)
    assert not result.is_error
    assert "not found" in result.output.lower()


@pytest.mark.asyncio
async def test_find_binary_finds_in_venv(tmp_path: Path) -> None:
    # Create a fake binary inside a .venv
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    fake_pytest = venv_bin / "pytest"
    fake_pytest.write_text("#!/bin/sh\necho ok")
    fake_pytest.chmod(0o755)

    result = await find_binary(name="pytest", real_workspace=tmp_path)
    assert not result.is_error
    assert str(fake_pytest) in result.output
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/test_tools_env.py -x -q
```

Expected: `ModuleNotFoundError: No module named 'agentd.tools.env'`

- [ ] **Step 3: Implement `find_binary`**

Create `agentd/tools/env.py`:

```python
"""Environment setup and binary discovery tools."""
from __future__ import annotations

import asyncio
import os
import shlex
from asyncio.subprocess import PIPE, STDOUT
from pathlib import Path

from agentd.tools.registry import ToolOutput

_MAX_OUTPUT_CHARS = 4000


async def find_binary(*, name: str, real_workspace: Path) -> ToolOutput:
    """Locate an executable binary on system PATH and within the real workspace.

    Runs `which {name}` then `find {real_workspace} -name {name} -maxdepth 6 -type f`.
    Returns all found paths ranked shallowest first, or a "not found" message.
    Not sandboxed to shadow — intentionally searches real filesystem.
    """
    if not name or "/" in name:
        return ToolOutput(output="Error: binary name must not contain path separators", is_error=True)

    found: list[str] = []

    # 1. System PATH lookup
    which_path = await _run_silent("which", name)
    if which_path:
        found.append(which_path.strip())

    # 2. Workspace-local search (covers .venv, venv, node_modules, etc.)
    try:
        proc = await asyncio.create_subprocess_exec(
            "find", str(real_workspace),
            "-name", name,
            "-maxdepth", "6",
            "-type", "f",
            stdout=PIPE,
            stderr=PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and line not in found:
                found.append(line)
    except (asyncio.TimeoutError, FileNotFoundError):
        pass

    if not found:
        return ToolOutput(output=f"not found: no '{name}' binary on PATH or in {real_workspace}", is_error=False)

    # Sort by path depth (shallowest = most local first)
    found.sort(key=lambda p: p.count(os.sep))
    lines = [f"found: {p}" for p in found]
    return ToolOutput(output="\n".join(lines))


async def _run_silent(command: str, *args: str) -> str | None:
    """Run a command, return stdout stripped, or None on failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            command, *args,
            stdout=PIPE,
            stderr=PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode == 0:
            return stdout.decode("utf-8", errors="replace").strip()
    except (asyncio.TimeoutError, FileNotFoundError):
        pass
    return None
```

- [ ] **Step 4: Run tests — confirm pass**

```bash
python -m pytest tests/test_tools_env.py::test_find_binary_finds_python tests/test_tools_env.py::test_find_binary_not_found tests/test_tools_env.py::test_find_binary_finds_in_venv -x -q
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add agentd/tools/env.py tests/test_tools_env.py
git commit -m "feat(tools): add find_binary — real-filesystem binary discovery"
```

---

### Task 3: `setup_env` — install to real workspace from shadow

**Files:**
- Modify: `agentd/tools/env.py`
- Modify: `tests/test_tools_env.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tools_env.py`:

```python
from agentd.tools.env import setup_env


@pytest.mark.asyncio
async def test_setup_env_rejects_unknown_binary(tmp_path: Path) -> None:
    result = await setup_env(
        command="rm -rf /",
        shadow_root=tmp_path,
        real_workspace=tmp_path,
    )
    assert result.is_error
    assert "not allowed" in result.output.lower()


@pytest.mark.asyncio
async def test_setup_env_rejects_empty_command(tmp_path: Path) -> None:
    result = await setup_env(
        command="",
        shadow_root=tmp_path,
        real_workspace=tmp_path,
    )
    assert result.is_error


@pytest.mark.asyncio
async def test_setup_env_uv_uses_shadow_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """setup_env runs with cwd=shadow_root and UV_PROJECT_ENVIRONMENT set."""
    calls: list[dict] = []

    async def fake_exec(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        calls.append({"args": args, "env": kwargs.get("env", {}), "cwd": kwargs.get("cwd")})
        proc = asyncio.subprocess.Process.__new__(asyncio.subprocess.Process)  # type: ignore[call-arg]
        # Return a minimal fake process
        raise FileNotFoundError("uv not installed — test only checks args")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    shadow = tmp_path / "shadow"
    shadow.mkdir()
    real = tmp_path / "real"
    real.mkdir()

    result = await setup_env(command="uv sync", shadow_root=shadow, real_workspace=real)
    assert calls, "create_subprocess_exec must have been called"
    call = calls[0]
    assert call["cwd"] == str(shadow)
    assert call["env"].get("UV_PROJECT_ENVIRONMENT") == str(real / ".venv")
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/test_tools_env.py -k "setup_env" -x -q
```

Expected: `ImportError` — `setup_env` not yet defined.

- [ ] **Step 3: Implement `setup_env`**

Append to `agentd/tools/env.py`:

```python
_SETUP_ENV_BINARIES = {"uv", "pip3", "pip", "npm", "yarn", "pnpm", "cargo", "go", "poetry"}
_SETUP_ENV_TIMEOUT_SEC = 300


async def setup_env(
    *,
    command: str,
    shadow_root: Path,
    real_workspace: Path,
    timeout_sec: int = _SETUP_ENV_TIMEOUT_SEC,
) -> ToolOutput:
    """Run an env setup command in shadow_root (reads patched dep files),
    installing binaries permanently to real_workspace.

    shadow_root is cwd so the package manager reads YOUR patched pyproject.toml
    / package.json / requirements.txt. Real-workspace targeting is achieved via
    env vars or explicit path args per package manager.
    """
    parts = command.strip().split()
    if not parts:
        return ToolOutput(output="Error: command is required", is_error=True)

    binary = parts[0]
    if binary not in _SETUP_ENV_BINARIES:
        return ToolOutput(
            output=(
                f"Error: '{binary}' not allowed for setup_env. "
                f"Allowed: {', '.join(sorted(_SETUP_ENV_BINARIES))}"
            ),
            is_error=True,
        )

    env = os.environ.copy()
    cmd_parts = list(parts)

    if binary == "uv":
        # uv reads pyproject.toml from cwd (shadow_root); installs venv to real workspace
        env["UV_PROJECT_ENVIRONMENT"] = str(real_workspace / ".venv")

    elif binary in ("pip3", "pip"):
        # Use real workspace's pip if its venv exists, otherwise fall back to system pip
        real_pip = real_workspace / ".venv" / "bin" / binary
        if real_pip.exists():
            cmd_parts[0] = str(real_pip)

    elif binary == "npm":
        env["npm_config_prefix"] = str(real_workspace)

    elif binary == "yarn":
        if "--modules-dir" not in command:
            cmd_parts += ["--modules-dir", str(real_workspace / "node_modules")]

    elif binary == "pnpm":
        if "--modules-dir" not in command:
            cmd_parts += ["--modules-dir", str(real_workspace / "node_modules")]

    # cargo/go/poetry: cwd=shadow_root is sufficient; they use global caches

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd_parts,
            cwd=str(shadow_root),
            stdout=PIPE,
            stderr=STDOUT,
            env=env,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        return ToolOutput(
            output=f"Error: setup_env '{command}' timed out after {timeout_sec}s",
            is_error=True,
        )
    except FileNotFoundError:
        return ToolOutput(
            output=f"Error: '{binary}' not found on PATH",
            is_error=True,
        )
    except Exception as exc:
        return ToolOutput(output=f"Error running setup_env '{command}': {exc}", is_error=True)

    output = stdout.decode("utf-8", errors="replace")
    exit_code = proc.returncode or 0
    header = f"$ {command}\n(exit code: {exit_code})\n"
    full = header + output
    if len(full) > _MAX_OUTPUT_CHARS:
        keep = _MAX_OUTPUT_CHARS - len(header) - 80
        full = header + f"...(truncated)...\n" + output[-keep:]
    return ToolOutput(output=full, is_error=exit_code != 0)
```

- [ ] **Step 4: Run tests — confirm pass**

```bash
python -m pytest tests/test_tools_env.py -x -q
```

Expected: all pass (6 tests).

- [ ] **Step 5: Commit**

```bash
git add agentd/tools/env.py tests/test_tools_env.py
git commit -m "feat(tools): add setup_env — real-workspace env install from shadow"
```

---

### Task 4: Extract `list_directory` to `tools/files.py`; update `PlanningToolRegistry`

**Files:**
- Modify: `agentd/tools/files.py`
- Modify: `agentd/planning/registry.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tools_env.py`:

```python
from agentd.tools.files import list_directory


@pytest.mark.asyncio
async def test_list_directory_shows_files(tmp_path: Path) -> None:
    (tmp_path / "foo.py").write_text("x = 1")
    (tmp_path / "bar.txt").write_text("hello")
    (tmp_path / "subdir").mkdir()

    result = await list_directory(path=".", root=tmp_path)
    assert not result.is_error
    assert "foo.py" in result.output
    assert "bar.txt" in result.output
    assert "subdir" in result.output


@pytest.mark.asyncio
async def test_list_directory_rejects_traversal(tmp_path: Path) -> None:
    result = await list_directory(path="../../etc", root=tmp_path)
    assert result.is_error
    assert "traversal" in result.output.lower()


@pytest.mark.asyncio
async def test_list_directory_missing_path(tmp_path: Path) -> None:
    result = await list_directory(path="nonexistent_dir", root=tmp_path)
    assert result.is_error
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/test_tools_env.py -k "list_directory" -x -q
```

Expected: `ImportError` — `list_directory` not in `tools/files.py` yet.

- [ ] **Step 3: Add `list_directory` to `tools/files.py`**

Append to the end of `agentd/tools/files.py`:

```python
async def list_directory(*, path: str, root: Path) -> ToolOutput:
    """List files and directories at path within root.

    Returns one entry per line: 'file  <name>' or 'dir   <name>'.
    Capped at 200 entries. Path traversal rejected.
    """
    from agentd.tools.registry import ToolOutput  # local import avoids circular

    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        return ToolOutput(output="Error: path traversal rejected", is_error=True)

    if not resolved.exists():
        return ToolOutput(output=f"Error: '{path}' does not exist", is_error=True)

    if not resolved.is_dir():
        return ToolOutput(output=f"Error: '{path}' is not a directory", is_error=True)

    entries = sorted(resolved.iterdir(), key=lambda p: (p.is_file(), p.name))
    lines: list[str] = []
    for entry in entries[:200]:
        kind = "file " if entry.is_file() else "dir  "
        lines.append(f"{kind}  {entry.name}")

    if not lines:
        return ToolOutput(output=f"(empty directory: {path})")

    return ToolOutput(output=f"Contents of {path}:\n" + "\n".join(lines))
```

- [ ] **Step 4: Run tests — confirm pass**

```bash
python -m pytest tests/test_tools_env.py -k "list_directory" -x -q
```

Expected: 3 passed.

- [ ] **Step 5: Update `PlanningToolRegistry` to use shared implementation**

In `agentd/planning/registry.py`, replace the `list_directory` dispatch in `execute()`:

```python
# Before
if name == "list_directory":
    return await self._list_directory(
        path=str(args.get("path", ".")),
        depth=int(args.get("depth", 2)),  # type: ignore[call-overload]
    )
```

```python
# After
if name == "list_directory":
    from agentd.tools.files import list_directory
    return await list_directory(
        path=str(args.get("path", ".")),
        root=self._real_path,
    )
```

Keep `_list_directory` and `_walk_dir` methods in place for now — they will be removed in Task 13 (dead code cleanup). No functional change to PlanningToolRegistry.

- [ ] **Step 6: Run planning tests — confirm no regression**

```bash
python -m pytest tests/test_planning_agent.py -x -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add agentd/tools/files.py agentd/planning/registry.py tests/test_tools_env.py
git commit -m "feat(tools): extract list_directory to tools/files.py; reuse in PlanningToolRegistry"
```

---

### Task 5: `ToolRegistry` — `real_workspace_path`, `definitions(phase=)`, new tools

**Files:**
- Modify: `agentd/tools/registry.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_tools_registry.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from agentd.tools.registry import ToolRegistry


def test_explore_phase_omits_env_tools(tmp_path: Path) -> None:
    registry = ToolRegistry(shadow_root=tmp_path, real_workspace_path=tmp_path)
    names = {t.name for t in registry.definitions(phase="explore")}
    assert "search_code" in names
    assert "read_file" in names
    assert "list_directory" in names
    assert "setup_env" not in names
    assert "find_binary" not in names


def test_verify_phase_includes_env_tools(tmp_path: Path) -> None:
    registry = ToolRegistry(shadow_root=tmp_path, real_workspace_path=tmp_path)
    names = {t.name for t in registry.definitions(phase="verify")}
    assert "setup_env" in names
    assert "find_binary" in names


def test_run_command_allows_full_path(tmp_path: Path) -> None:
    """Basename of a full path must pass the allowlist check."""
    import asyncio

    registry = ToolRegistry(shadow_root=tmp_path, real_workspace_path=tmp_path)
    # Create a fake pytest binary
    fake = tmp_path / "pytest"
    fake.write_text("#!/bin/sh\necho ok")
    fake.chmod(0o755)

    result = asyncio.get_event_loop().run_until_complete(
        registry.execute("run_command", {"command": str(fake), "args": ["--version"]})
    )
    # Should NOT be an allowlist rejection (may fail for other reasons)
    assert "not in the shell allowlist" not in result.output


def test_run_command_blocks_unlisted_binary(tmp_path: Path) -> None:
    import asyncio

    registry = ToolRegistry(shadow_root=tmp_path, real_workspace_path=tmp_path)
    result = asyncio.get_event_loop().run_until_complete(
        registry.execute("run_command", {"command": "rm", "args": ["-rf", "/"]})
    )
    assert result.is_error
    assert "allowlist" in result.output.lower()
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/test_tools_registry.py -x -q
```

Expected: `TypeError` — `ToolRegistry.__init__` missing `real_workspace_path`.

- [ ] **Step 3: Update `ToolRegistry.__init__`**

In `agentd/tools/registry.py`, add `real_workspace_path` parameter:

```python
def __init__(
    self,
    shadow_root: Path,
    real_workspace_path: Path,      # NEW
    semantic_index: object | None = None,
) -> None:
    self._shadow_root = shadow_root
    self._real_workspace_path = real_workspace_path   # NEW
    self._semantic_index = semantic_index
    self._ripgrep_cmd = os.environ.get("AI_EDITOR_RIPGREP_CMD", "rg")
    allowlist_raw = os.environ.get(
        "AI_EDITOR_SHELL_ALLOWLIST",
        "pytest,npm,cargo,ruff,mypy,tsc,eslint,jest,vitest",
    )
    self._shell_allowlist = {c.strip() for c in allowlist_raw.split(",") if c.strip()}
```

- [ ] **Step 4: Update `definitions()` to accept `phase` and add new tools**

Replace `definitions(self)` with:

```python
def definitions(self, phase: str = "explore") -> list[ToolDefinition]:
    tools = [
        ToolDefinition(
            name="search_code",
            description=(
                "Search for a regex/literal pattern across files in the workspace using ripgrep. "
                "Use to find callers, definitions, imports, or any text pattern."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex or literal pattern to search for"},
                    "path_filter": {"type": "string", "description": "Optional glob to restrict search (e.g. '*.py', 'src/**/*.ts')"},
                    "context_lines": {"type": "integer", "description": "Lines of context around each match (default 3)"},
                    "fixed_strings": {"type": "boolean", "description": "Treat pattern as a literal string, not a regex (default false)"},
                },
                "required": ["pattern"],
            },
        ),
        ToolDefinition(
            name="read_file",
            description=(
                "Read a file from the workspace. Optionally specify a line range. "
                "Use when you need to see file content not in the initial context."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path within the workspace"},
                    "start_line": {"type": "integer", "description": "First line to read (1-indexed, inclusive)"},
                    "end_line": {"type": "integer", "description": "Last line to read (1-indexed, inclusive)"},
                },
                "required": ["path"],
            },
        ),
        ToolDefinition(
            name="list_directory",
            description=(
                "List files and directories at a path in the workspace. "
                "Use to detect lockfiles (uv.lock, package-lock.json) at project root, "
                "or check if a binary exists (.venv/bin/pytest, node_modules/.bin/vitest)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to list (e.g. '.' or '.venv/bin')"},
                },
                "required": ["path"],
            },
        ),
        ToolDefinition(
            name="run_command",
            description=(
                "Run an allow-listed shell command inside the shadow workspace. "
                f"Allowed commands: {', '.join(sorted(self._shell_allowlist))}. "
                "Full paths to allowed binaries are also accepted (e.g. /home/user/.venv/bin/pytest). "
                "Use to run tests, linters, or type checkers."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Command name or full path (basename must be in the allowlist)"},
                    "args": {"type": "array", "items": {"type": "string"}, "description": "Command arguments"},
                },
                "required": ["command"],
            },
        ),
    ]

    if phase == "verify":
        tools += [
            ToolDefinition(
                name="find_binary",
                description=(
                    "Locate an executable binary in the real workspace or on system PATH. "
                    "Use when run_command fails with 'not found'. "
                    "Returns full paths ranked by proximity to workspace root."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Binary name to find, e.g. 'pytest', 'vitest', 'cargo'"},
                    },
                    "required": ["name"],
                },
            ),
            ToolDefinition(
                name="setup_env",
                description=(
                    "Install or sync declared dependencies into the real workspace. "
                    "Reads dependency files from YOUR patched workspace (shadow). "
                    "Any dependency you added via emit_patch will be picked up. "
                    "Installs binaries permanently to the real workspace's .venv or node_modules. "
                    "Call ONLY when find_binary confirms binary is absent. "
                    "Allowed: 'uv sync', 'pip install -r requirements.txt', "
                    "'npm ci', 'yarn install --frozen-lockfile', 'pnpm install --frozen-lockfile', "
                    "'cargo build', 'go mod download', 'poetry install'"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Full command string, e.g. 'uv sync' or 'npm ci'"},
                    },
                    "required": ["command"],
                },
            ),
        ]

    if self._semantic_index is not None:
        tools.append(
            ToolDefinition(
                name="search_semantic",
                description=(
                    "Vector similarity search: find code semantically related to a natural-language query. "
                    "Use when you don't know the exact names or patterns but know the concept."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Natural-language description of what you're looking for"},
                        "top_k": {"type": "integer", "description": "Number of results to return (default 8)"},
                    },
                    "required": ["query"],
                },
            )
        )
    return tools
```

- [ ] **Step 5: Update `execute()` — add new tools, fix `run_command` allowlist, add `list_directory`**

Replace the `execute` method:

```python
async def execute(self, name: str, args: dict[str, object]) -> ToolOutput:
    if name == "search_code":
        from agentd.tools.search import search_code
        return await search_code(
            pattern=str(args.get("pattern", "")),
            path_filter=str(args["path_filter"]) if "path_filter" in args else None,
            context_lines=int(args.get("context_lines", 3)),  # type: ignore[call-overload]
            fixed_strings=bool(args.get("fixed_strings", False)),
            shadow_root=self._shadow_root,
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
            shadow_root=self._real_workspace_path,
        )

    if name == "list_directory":
        from agentd.tools.files import list_directory
        return await list_directory(
            path=str(args.get("path", ".")),
            root=self._real_workspace_path,
        )

    if name == "run_command":
        from agentd.tools.shell import run_command
        from pathlib import Path as _Path
        raw_args = args.get("args", [])
        cmd_args = [str(a) for a in raw_args] if isinstance(raw_args, list) else []
        command = str(args.get("command", ""))
        # Allow full paths: check basename against allowlist
        binary_name = _Path(command).name
        return await run_command(
            command=command,
            args=cmd_args,
            shadow_root=self._shadow_root,
            allowlist=self._shell_allowlist,
            binary_name_override=binary_name,
        )

    if name == "find_binary":
        from agentd.tools.env import find_binary
        return await find_binary(
            name=str(args.get("name", "")),
            real_workspace=self._real_workspace_path,
        )

    if name == "setup_env":
        from agentd.tools.env import setup_env
        return await setup_env(
            command=str(args.get("command", "")),
            shadow_root=self._shadow_root,
            real_workspace=self._real_workspace_path,
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
```

- [ ] **Step 6: Update `run_command` in `tools/shell.py` to accept `binary_name_override`**

In `agentd/tools/shell.py`, update `run_command` signature and allowlist check:

```python
async def run_command(
    *,
    command: str,
    args: list[str],
    shadow_root: Path,
    allowlist: set[str],
    timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
    binary_name_override: str | None = None,   # NEW: use basename of full path
) -> ToolOutput:
    if not command:
        return ToolOutput(output="Error: command is required", is_error=True)

    check_name = binary_name_override or command   # NEW: basename check
    if check_name not in allowlist:
        return ToolOutput(
            output=(
                f"Error: '{check_name}' is not in the shell allowlist. "
                f"Allowed: {', '.join(sorted(allowlist))}"
            ),
            is_error=True,
        )
    # rest of the function unchanged ...
```

- [ ] **Step 7: Run tests — confirm pass**

```bash
python -m pytest tests/test_tools_registry.py tests/test_tools_env.py -x -q
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add agentd/tools/registry.py agentd/tools/shell.py tests/test_tools_registry.py
git commit -m "feat(tools): ToolRegistry gains real_workspace_path, phase-gated tools, basename allowlist"
```

---

### Task 6: Schema + prompt — `verify_done`, two-phase system prompt

**Files:**
- Modify: `agentd/reasoning/tool_prompts.py`

- [ ] **Step 1: Add `verify_done` fields to `AGENT_STEP_RESPONSE_SCHEMA`**

In `AGENT_STEP_RESPONSE_SCHEMA["properties"]`, add after `"affected_steps"`:

```python
# verify_done fields
"verified": {
    "type": "boolean",
    "description": "True when all linters and tests passed (required for verify_done)",
},
"test_output": {
    "type": "string",
    "description": "Full output from the last test/lint run (required for verify_done)",
},
```

Update the `"type"` enum to include `"verify_done"`:

```python
"type": {
    "type": "string",
    "enum": ["tool_call", "emit_patch", "verify_done", "revision_needed"],
    "description": "Action type: tool_call to gather context, emit_patch to write code, verify_done when checks pass, revision_needed if plan is wrong",
},
```

- [ ] **Step 2: Add EXECUTION PHASES and BINARY DISCOVERY to `TOOL_LOOP_SYSTEM_PROMPT`**

After the RULES block, add:

```python
TOOL_LOOP_SYSTEM_PROMPT = """\
You are an expert code editor executing a single step of a coding plan.
You have access to tools to gather information before writing code.

AVAILABLE TOOLS:
{tools_json}

PATCH OPERATION FORMATS (for emit_patch):
Each element of patch_ops must be one of these objects:

search_replace — find and replace text in a file (most reliable):
  {{"op": "search_replace", "file": "path/to/file.rs", "search": "exact text to find", "replace": "new text", "reason": "why"}}

create_file — create a new file:
  {{"op": "create_file", "file": "path/to/new_file.ext", "content": "full file content", "reason": "why"}}

apply_diff — apply a unified diff (for multi-section edits):
  {{"op": "apply_diff", "file": "path/to/file.ext", "diff": "@@ -1,3 +1,4 @@\\n context\\n+added line\\n context", "reason": "why"}}

delete_file — delete a file:
  {{"op": "delete_file", "file": "path/to/file.ext", "reason": "why"}}

RULES:
1. Use tools to gather context before writing code. Read files you haven't seen.
2. When you have enough information, emit a patch. Do not over-search.
3. The search field in search_replace must be an EXACT substring of the current file content.
4. Output exactly one JSON object per turn. The "type" field selects the variant; all fields
   listed for that variant are REQUIRED.

EXECUTION PHASES:

Phase 1 — EXPLORE & PATCH
  Gather context with tools, emit_patch when confident.
  After your patch is applied you will automatically enter Phase 2.

Phase 2 — VERIFY
  You will be notified in the conversation when Phase 2 begins.
  Required sequence:
    1. Run static analysis first (fast): ruff check, mypy, tsc --noEmit, cargo check
    2. Run tests: pytest, cargo test, vitest, npm test
    3. If any check fails: emit another emit_patch to correct, then re-run checks
    4. When all pass: emit verify_done with verified=true and full test_output

  Rules:
    - You MUST run at least one linter AND one test command before verify_done(verified=true)
    - If this step has no test_command hint, emit verify_done(verified=true) immediately
    - Never claim verified=true without actually running the checks

BINARY DISCOVERY (verify phase only):

When run_command fails with "not found":
  1. find_binary <name>               — returns full paths in real workspace
  2. If found: run_command <full-path> <args>  (full paths to allowed binaries accepted)
  3. If not found: detect package manager, call setup_env, then retry

Package manager detection — list_directory(".") first:
  uv.lock              -> setup_env: "uv sync"
  poetry.lock          -> setup_env: "poetry install"
  requirements*.txt    -> setup_env: "pip install -r requirements.txt"
  pyproject.toml only  -> setup_env: "uv sync"
  package-lock.json    -> setup_env: "npm ci"
  yarn.lock            -> setup_env: "yarn install --frozen-lockfile"
  pnpm-lock.yaml       -> setup_env: "pnpm install --frozen-lockfile"
  Cargo.toml           -> cargo is always available, no setup needed
  go.mod               -> setup_env: "go mod download"

IMPORTANT: setup_env reads YOUR patched files (shadow workspace), not the original.
If you added a dependency via emit_patch, call setup_env immediately after —
it reads your patched pyproject.toml/package.json.

When a dependency is missing from the project file:
  1. emit_patch  — add the dep to pyproject.toml / package.json
  2. setup_env   — reads your patched file, installs to real env
  3. find_binary — confirm the binary is now present
  4. run_command — run the test

Concrete example (Python/uv, pytest missing):
  list_directory(".")         -> pyproject.toml, uv.lock, src/, tests/
  run_command pytest tests/   -> Error: pytest not found on PATH
  find_binary pytest          -> not found in real workspace
  emit_patch                  -> add "pytest>=8" to pyproject.toml dev-dependencies
  setup_env "uv sync"         -> cwd=shadow, UV_PROJECT_ENVIRONMENT=/real/.venv
  find_binary pytest          -> found: /real/.venv/bin/pytest
  run_command /real/.venv/bin/pytest tests/test_foo.py  -> 1 passed
  verify_done verified=true test_output="1 passed"

OUTPUT — choose exactly one variant per turn:

Variant 1 — call a tool (required fields: type, thought, tool, args):
  {{"type": "tool_call", "thought": "<1-3 sentence reasoning>", "tool": "<tool_name>", "args": {{<tool args>}}}}

Variant 2 — emit patch ops (required fields: type, thought, patch_ops):
  {{"type": "emit_patch", "thought": "<final reasoning>", "patch_ops": [{{<patch op>}}, ...]}}

Variant 3 — signal plan error (required fields: type, thought, reason, evidence, affected_steps):
  {{"type": "revision_needed", "thought": "...", "reason": "...", "evidence": "...", "affected_steps": [...]}}
  Use ONLY when the target files/symbols in the plan are fundamentally wrong.

Variant 4 — signal verify complete (required fields: type, thought, verified, test_output):
  {{"type": "verify_done", "thought": "...", "verified": true, "test_output": "full pytest or linter output"}}
  Use after ALL linters and tests pass. Or immediately if no test_command is set.
"""
```

- [ ] **Step 3: Update `build_tool_step_payload` to accept `phase`**

Replace `build_tool_step_payload`:

```python
def build_tool_step_payload(
    step_context: dict[str, object],
    history: list[dict[str, object]],
    *,
    phase: str = "explore",
) -> dict[str, object]:
    """Build the user_payload dict for a single ReAct loop turn."""
    payload: dict[str, object] = {
        "step_goal": step_context.get("goal", ""),
        "targets": step_context.get("targets", []),
        "allowed_files": step_context.get("allowed_files", []),
        "last_failure": step_context.get("last_failure"),
    }

    for field in ("implementation_details", "edge_cases", "design_rationale", "testing_strategy"):
        value = step_context.get(field)
        if value:
            payload[field] = value

    risk = step_context.get("risk")
    if risk and risk != "low":
        payload["risk"] = risk

    file_contents = step_context.get("file_contents")
    if file_contents:
        payload["file_contents"] = file_contents

    diagnostics = step_context.get("diagnostics")
    if diagnostics:
        payload["diagnostics"] = diagnostics

    plan_markdown = step_context.get("plan_markdown")
    if plan_markdown:
        payload["plan_markdown"] = plan_markdown

    if history:
        payload["conversation_history"] = history
        if phase == "verify":
            payload["instruction"] = (
                "You are in VERIFY phase. Run linters and tests. "
                "Emit verify_done when all checks pass, or emit_patch to correct failures."
            )
        else:
            payload["instruction"] = (
                "Continue the conversation above. Output your NEXT action as a JSON object. "
                "If you have gathered enough context, emit_patch. Otherwise call another tool."
            )
    else:
        payload["instruction"] = (
            "Start gathering context for this step. "
            "Output your first action as a JSON object."
        )

    return payload
```

- [ ] **Step 4: Update `format_tool_system_prompt` to accept `phase`**

```python
def format_tool_system_prompt(tool_definitions: list[dict[str, object]], *, phase: str = "explore") -> str:
    tools_json = json.dumps(tool_definitions, indent=2)
    return TOOL_LOOP_SYSTEM_PROMPT.format(tools_json=tools_json)
```

- [ ] **Step 5: Run test suite to confirm no regression**

```bash
python -m pytest tests/ -x -q --ignore=tests/test_anthropic_transport.py --ignore=tests/test_gemini_transport.py --ignore=tests/test_groq_transport.py --ignore=tests/test_openai_transport.py --ignore=tests/test_huggingface_transport.py --ignore=tests/test_watsonx_transport.py
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add agentd/reasoning/tool_prompts.py
git commit -m "feat(prompts): add verify_done schema, two-phase system prompt, phase-aware payload builder"
```

---

### Task 7: `ToolLoop` — `VerifyResult`, inline patch apply, two-phase loop

**Files:**
- Modify: `agentd/tools/loop.py`

- [ ] **Step 1: Add `VerifyResult`, remove `PatchResult`, update `StepOutcome`**

Replace the `PatchResult` dataclass with `VerifyResult`:

```python
@dataclass
class VerifyResult:
    patch_document: dict[str, object]   # last applied patch (for artifact writing)
    touched_files: list[str]            # all files modified across all emit_patch calls
    verified: bool
    test_output: str                    # empty when no test_command
    tool_trace: AgentToolTrace
```

Update `StepOutcome`:

```python
StepOutcome = VerifyResult | PlanHandoff
```

- [ ] **Step 2: Update `ToolLoop.__init__` to accept `patch_engine` and `shadow_path`**

```python
from agentd.patch.engine import PatchEngine

class ToolLoop:
    def __init__(
        self,
        reasoning_engine: ReasoningEngine,
        registry: ToolRegistry,
        broadcaster: PatchEventBroadcaster,
        task_id: str,
        patch_engine: PatchEngine,    # NEW
        shadow_path: Path,            # NEW
    ) -> None:
        self._reasoning = reasoning_engine
        self._registry = registry
        self._broadcaster = broadcaster
        self._task_id = task_id
        self._patch_engine = patch_engine    # NEW
        self._shadow_path = shadow_path      # NEW
```

- [ ] **Step 3: Rewrite `ToolLoop.run()` with two-phase logic**

Replace `run()` entirely:

```python
async def run(
    self,
    step: PlanStep,
    patch_request_context: dict[str, object],
    budget: TaskBudget,
    usage: TaskUsage,
) -> StepOutcome:
    trace = AgentToolTrace(step_id=step.id)
    history: list[dict[str, object]] = []
    phase = "explore"
    explore_calls = 0
    verify_calls = 0
    last_patch_document: dict[str, object] = {}
    all_touched_files: list[str] = []

    retrieval_ctx = patch_request_context.get("retrieval_context") or {}
    if not isinstance(retrieval_ctx, dict):
        retrieval_ctx = {}

    step_context: dict[str, object] = {
        "goal": step.goal,
        "targets": [{"path": t.path, "intent": t.intent} for t in step.targets],
        "risk": step.risk,
        "implementation_details": step.implementation_details,
        "edge_cases": step.edge_cases,
        "design_rationale": step.design_rationale,
        "testing_strategy": step.testing_strategy,
        "allowed_files": patch_request_context.get("allowed_files"),
        "file_contents": retrieval_ctx.get("file_contents"),
        "diagnostics": patch_request_context.get("diagnostics"),
        "last_failure": patch_request_context.get("last_failure"),
        "plan_markdown": patch_request_context.get("plan_markdown"),
    }

    max_explore = budget.max_tool_calls_per_step
    max_verify = budget.max_verify_calls_per_step
    total_budget = max_explore + max_verify + 10  # generous outer cap

    for iteration in range(total_budget):
        tool_defs = [t.model_dump() for t in self._registry.definitions(phase=phase)]

        response = await self._reasoning.create_tool_step(
            step_context=step_context,
            history=history,
            tool_definitions=tool_defs,
        )

        action_type = str(response.get("type", ""))
        thought = str(response.get("thought", ""))

        # ── verify_done ──────────────────────────────────────────────────
        if action_type == "verify_done":
            return VerifyResult(
                patch_document=last_patch_document,
                touched_files=all_touched_files,
                verified=bool(response.get("verified", False)),
                test_output=str(response.get("test_output", "")),
                tool_trace=trace,
            )

        # ── revision_needed ──────────────────────────────────────────────
        if action_type == "revision_needed":
            reason = str(response.get("reason", ""))
            evidence = str(response.get("evidence", ""))
            raw_affected = response.get("affected_steps", [])
            affected = [str(s) for s in raw_affected] if isinstance(raw_affected, list) else []
            logger.info("Tool loop revision_needed: %s", reason[:200],
                        extra={"task_id": self._task_id, "step_id": step.id})
            self._broadcaster.broadcast(self._task_id, {
                "type": "revision_needed", "step_id": step.id,
                "reason": reason, "evidence": evidence[:300],
            })
            return PlanHandoff(
                step_id=step.id, reason=reason, evidence=evidence,
                hinted_affected_steps=affected, tool_trace=trace,
            )

        # ── emit_patch ───────────────────────────────────────────────────
        if action_type == "emit_patch":
            patch_ops = response.get("patch_ops")
            if not isinstance(patch_ops, list):
                raise ToolBudgetExceededError(
                    f"Step {step.id!r}: emit_patch has non-list 'patch_ops' at iteration {iteration}"
                )

            patch_document = self._wrap_as_patch_document(patch_ops)
            history.append({"role": "assistant", "content": json.dumps(response, default=str)})

            # Apply inline
            apply_result = await self._apply_patch_inline(patch_document, step)

            if apply_result.get("is_error"):
                error_msg = str(apply_result.get("error", "patch application failed"))
                logger.warning("Inline patch failed: %s", error_msg,
                               extra={"task_id": self._task_id, "step_id": step.id})
                history.append({
                    "role": "tool_result", "tool": "_patch_apply",
                    "content": f"Patch FAILED: {error_msg}\nFix your search strings and re-emit.",
                })
                self._broadcaster.broadcast(self._task_id, {
                    "type": "patch_failed", "step_id": step.id, "error": error_msg,
                })
                continue  # stay in explore, agent corrects and re-emits

            # Patch succeeded
            touched = apply_result.get("touched_files", [])
            if isinstance(touched, list):
                for f in touched:
                    if f not in all_touched_files:
                        all_touched_files.append(str(f))
            last_patch_document = patch_document

            # Short-circuit if no verify needed
            if not step.test_command:
                return VerifyResult(
                    patch_document=last_patch_document,
                    touched_files=all_touched_files,
                    verified=True,
                    test_output="",
                    tool_trace=trace,
                )

            # Transition to verify phase
            phase = "verify"
            history.append({
                "role": "tool_result", "tool": "_patch_apply",
                "content": (
                    "Patch applied successfully.\n"
                    "VERIFY PHASE: run linters then tests.\n"
                    f"test_command hint: {step.test_command}\n"
                    "Emit verify_done when all checks pass, or emit_patch again to correct."
                ),
            })
            self._broadcaster.broadcast(self._task_id, {
                "type": "patch_applied", "step_id": step.id,
                "phase": "verify", "touched_files": all_touched_files,
            })
            continue

        # ── tool_call ────────────────────────────────────────────────────
        if action_type != "tool_call":
            raise ToolBudgetExceededError(
                f"Step {step.id!r}: unexpected response type '{action_type}' at iteration {iteration}"
            )

        # Budget enforcement per phase
        if phase == "explore":
            if explore_calls >= max_explore:
                raise ToolBudgetExceededError(
                    f"Step {step.id!r}: explore budget ({max_explore}) exhausted without emitting a patch"
                )
            explore_calls += 1
        else:
            if verify_calls >= max_verify:
                return VerifyResult(
                    patch_document=last_patch_document,
                    touched_files=all_touched_files,
                    verified=False,
                    test_output=f"Verify budget exhausted after {verify_calls} calls without passing checks",
                    tool_trace=trace,
                )
            verify_calls += 1

        tool_name = str(response.get("tool", ""))
        raw_args = response.get("args")
        args: dict[str, object] = raw_args if isinstance(raw_args, dict) else {}

        self._broadcaster.broadcast(self._task_id, {
            "type": "tool_call", "tool": tool_name,
            "thought": thought[:300], "iteration": iteration + 1, "phase": phase,
        })

        tool_output = await self._registry.execute(tool_name, args)
        usage.tool_calls_used += 1

        self._broadcaster.broadcast(self._task_id, {
            "type": "tool_result", "tool": tool_name,
            "output": tool_output.output[:500], "is_error": tool_output.is_error,
            "iteration": iteration + 1,
        })

        call_id = f"{step.id}-{uuid4().hex[:8]}"
        trace.calls.append(ToolCall(call_id=call_id, tool_name=tool_name, arguments=args))
        trace.results.append(ToolResult(
            call_id=call_id, tool_name=tool_name,
            output=tool_output.output[:_MAX_OUTPUT_INJECT_CHARS],
            is_error=tool_output.is_error,
        ))

        history.append({"role": "assistant", "content": json.dumps(response, default=str)})
        history.append({
            "role": "tool_result", "tool": tool_name,
            "content": tool_output.output[:_MAX_OUTPUT_INJECT_CHARS],
        })

    raise ToolBudgetExceededError(f"Step {step.id!r}: total budget exceeded")
```

- [ ] **Step 4: Add `_apply_patch_inline` helper**

```python
async def _apply_patch_inline(
    self,
    patch_document: dict[str, object],
    step: PlanStep,
) -> dict[str, object]:
    """Apply patch_document to shadow_path. Returns {touched_files, is_error, error}."""
    from agentd.domain.models import PatchDocumentV2
    from pydantic import ValidationError

    try:
        doc = PatchDocumentV2.model_validate(patch_document)
    except (ValidationError, Exception) as exc:
        return {"is_error": True, "error": f"Invalid patch document: {exc}", "touched_files": []}

    if not doc.candidates:
        return {"is_error": True, "error": "No candidates in patch document", "touched_files": []}

    candidate = doc.candidates[0]
    allowed_files = {t.path for t in step.targets}

    try:
        result = await self._patch_engine.apply_patch_candidate(
            self._shadow_path,
            candidate,
            allowed_files=allowed_files,
        )
    except Exception as exc:
        return {"is_error": True, "error": str(exc), "touched_files": []}

    if not result.success:
        issues = "; ".join(i.message for i in result.issues[:3])
        return {"is_error": True, "error": issues, "touched_files": []}

    touched = [op.get("file", "") for op in (candidate.patch_ops or []) if isinstance(op, dict)]
    return {"is_error": False, "touched_files": [f for f in touched if f]}
```

- [ ] **Step 5: Update `build_tool_registry`**

```python
def build_tool_registry(
    shadow_root: Path,
    retrieval_client: object | None = None,
    real_workspace_path: Path | None = None,    # NEW
) -> ToolRegistry:
    semantic_index = getattr(retrieval_client, "_semantic_index", None)
    return ToolRegistry(
        shadow_root=shadow_root,
        real_workspace_path=real_workspace_path or shadow_root,  # fallback
        semantic_index=semantic_index,
    )
```

- [ ] **Step 6: Run tests — confirm no import errors**

```bash
python -m pytest tests/test_planning_agent.py tests/test_delta_replan.py -x -q
```

Expected: pass (scripted engine's `create_tool_step` returns `emit_patch`; no `test_command` on steps → ToolLoop returns `VerifyResult(verified=True)` immediately — engine must handle this in the next task).

- [ ] **Step 7: Commit**

```bash
git add agentd/tools/loop.py
git commit -m "feat(loop): two-phase ToolLoop — emit_patch checkpoint, inline apply, VerifyResult"
```

---

### Task 8: `ScriptedReasoningEngine` — `tool_step_responses` for verify testing

**Files:**
- Modify: `agentd/orchestrator/scripted_engine.py`

- [ ] **Step 1: Add `tool_step_responses` support**

Update `ScriptedReasoningEngine`:

```python
class ScriptedReasoningEngine:
    def __init__(
        self,
        plan: object,
        patches: list[object],
        tool_step_responses: list[dict[str, object]] | None = None,
    ) -> None:
        self._plan = plan
        self._patches = patches
        self._patch_index = 0
        self._tool_step_responses = tool_step_responses  # NEW
        self._tool_step_index = 0                        # NEW
```

Update `create_tool_step`:

```python
async def create_tool_step(
    self,
    step_context: dict[str, object],
    history: list[dict[str, object]],
    tool_definitions: list[dict[str, object]],
) -> dict[str, object]:
    _ = (step_context, history, tool_definitions)

    # If explicit tool_step_responses are provided, replay them in order
    if self._tool_step_responses is not None:
        index = min(self._tool_step_index, len(self._tool_step_responses) - 1)
        self._tool_step_index += 1
        return self._tool_step_responses[index]

    # Default: wrap next patch as emit_patch (backward compat)
    if not self._patches:
        raise RuntimeError("ScriptedReasoningEngine has no patch payloads configured")

    index = min(self._patch_index, len(self._patches) - 1)
    self._patch_index += 1
    patch_doc = self._patches[index]

    patch_ops: list[object] = []
    if isinstance(patch_doc, dict):
        raw_candidates = patch_doc.get("candidates")
        if isinstance(raw_candidates, list) and raw_candidates:
            first = raw_candidates[0]
            if isinstance(first, dict):
                ops = first.get("patch_ops")
                if isinstance(ops, list):
                    patch_ops = ops
    return {"type": "emit_patch", "thought": "scripted engine bypasses tool loop", "patch_ops": patch_ops}
```

- [ ] **Step 2: Run existing tests — confirm backward compat**

```bash
python -m pytest tests/test_orchestrator_repair_rollback.py tests/test_plan_feedback_api.py -x -q
```

Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add agentd/orchestrator/scripted_engine.py
git commit -m "feat(scripted-engine): add tool_step_responses for multi-turn verify testing"
```

---

### Task 9: Engine — wire `VerifyResult`, remove dead validation methods

**Files:**
- Modify: `agentd/orchestrator/engine.py`

- [ ] **Step 1: Update ToolLoop construction in `_run_step_with_retries`**

Find the ToolLoop construction block (around line 855) and replace:

```python
# Before
from agentd.tools.loop import PatchResult, PlanHandoff, ToolLoop, build_tool_registry
registry = build_tool_registry(shadow_path, self._retrieval_client)
tool_loop = ToolLoop(
    self._reasoning_engine,
    registry,
    self.broadcaster,
    task.task_id,
)
step_outcome = await tool_loop.run(
    step,
    {**patch_request_context, "plan_markdown": task.plan_markdown},
    task.budget,
    task.usage,
)

if isinstance(step_outcome, PlanHandoff):
    self._restore_shadow_checkpoint(shadow_path, checkpoint.checkpoint_path)
    task.modified_files = previous_modified_files
    return step_outcome

patch_raw = step_outcome.patch_document
tool_trace = step_outcome.tool_trace
```

```python
# After
from agentd.tools.loop import PlanHandoff, VerifyResult, ToolLoop, build_tool_registry
registry = build_tool_registry(
    shadow_path,
    self._retrieval_client,
    real_workspace_path=Path(task.workspace_path),
)
tool_loop = ToolLoop(
    self._reasoning_engine,
    registry,
    self.broadcaster,
    task.task_id,
    self._patch_engine,
    shadow_path,
)
step_outcome = await tool_loop.run(
    step,
    {**patch_request_context, "plan_markdown": task.plan_markdown},
    task.budget,
    task.usage,
)

if isinstance(step_outcome, PlanHandoff):
    self._restore_shadow_checkpoint(shadow_path, checkpoint.checkpoint_path)
    task.modified_files = previous_modified_files
    return step_outcome

assert isinstance(step_outcome, VerifyResult)

if not step_outcome.verified:
    # Verify phase failed — restore shadow and retry with test output
    self._restore_shadow_checkpoint(shadow_path, checkpoint.checkpoint_path)
    task.modified_files = previous_modified_files
    last_failure = {
        "failure_code": "verify_failed",
        "excerpt": step_outcome.test_output[:2000] if step_outcome.test_output else "Verify phase failed",
    }
    self._write_debug_artifact(
        task.task_id, "tool-trace",
        step_outcome.tool_trace.model_dump(mode="json"),
        step_id=step.id, attempt=attempt,
        artifacts_root_path=task.artifacts_root_path,
    )
    trace_entries.append(StepExecutionTrace(
        step_id=step.id, attempt=attempt, status="validation_failed",
        message=f"verify phase failed: {step_outcome.test_output[:200]}",
        checkpoint_id=checkpoint.checkpoint_id,
    ))
    last_result_diagnostics = [
        *persistent_diagnostics,
        Diagnostic(source="verify_phase", message=step_outcome.test_output[:500], level="error"),
    ]
    checkpoints.append(checkpoint)
    continue

# VerifyResult(verified=True) — patch already applied, proceed
touched = step_outcome.touched_files
tool_trace = step_outcome.tool_trace
patch_raw = step_outcome.patch_document
self._write_debug_artifact(
    task.task_id, "tool-trace",
    tool_trace.model_dump(mode="json"),
    step_id=step.id, attempt=attempt,
    artifacts_root_path=task.artifacts_root_path,
)
self._write_debug_artifact(
    task.task_id, "patch",
    patch_raw,
    step_id=step.id, attempt=attempt,
    artifacts_root_path=task.artifacts_root_path,
)
print(f"[PATCH] Tool loop complete ({len(tool_trace.calls)} tool calls, verified={step_outcome.verified})")
# Skip _evaluate_candidates — patch is already applied and verified
# Jump directly to StepRunResult construction
task.modified_files = sorted(set(task.modified_files) | set(touched))
trace_entries.append(StepExecutionTrace(
    step_id=step.id, attempt=attempt, status="step_completed",
    checkpoint_id=checkpoint.checkpoint_id,
    message=f"verify passed: {step_outcome.test_output[:200] if step_outcome.test_output else 'no test output'}",
))
return StepRunResult(
    step_id=step.id,
    outcome="step_completed",
    validation_result="validation_passed",
    attempts_used=attempt,
    selected_candidate_id=None,
    touched_files=touched,
    diagnostics=[*persistent_diagnostics],
    trace_entries=trace_entries,
    checkpoint_manifests=checkpoints,
    last_failure=None,
)
```

Note: place a `# fall through to single-shot path` comment after the `if self._tool_loop_enabled:` block so the `else:` branch (single-shot `_create_patch_document`) remains untouched. The single-shot path still uses `_evaluate_candidates`.

- [ ] **Step 2: Remove the 5 dead per-step validation methods**

Delete these methods entirely from `AgentOrchestrator`:
- `_run_fast_validation` (lines ~1274-1304)
- `_extract_path_from_test_command` (lines ~1306-1327)
- `_build_test_env` (lines ~1329-1354)
- `_run_step_test_command` (lines ~1356-~1450)
- Module-level `_merge_validation_results` (lines ~71-76)

Also remove the `effective_test_command` preflight gate in `_run_step_with_retries` (lines ~768-779) — the agent now owns this logic.

- [ ] **Step 3: Run full test suite**

```bash
cd .worktrees/feat-agentic-planning/services/agentd-py
python -m pytest tests/ -x -q \
  --ignore=tests/test_anthropic_transport.py \
  --ignore=tests/test_gemini_transport.py \
  --ignore=tests/test_groq_transport.py \
  --ignore=tests/test_openai_transport.py \
  --ignore=tests/test_huggingface_transport.py \
  --ignore=tests/test_watsonx_transport.py
```

Expected: all pass.

- [ ] **Step 4: Confirm dead code is gone**

```bash
grep -rn "_run_fast_validation\|_run_step_test_command\|_build_test_env\|_extract_path_from_test_command\|_merge_validation_results\|PatchResult" agentd/
```

Expected: zero results.

- [ ] **Step 5: Commit**

```bash
git add agentd/orchestrator/engine.py
git commit -m "feat(engine): wire VerifyResult, remove per-step validation methods, pass real_workspace_path"
```

---

### Task 10: Integration tests — verify loop end-to-end

**Files:**
- Create: `tests/test_orchestrator_verify_flow.py`

- [ ] **Step 1: Write the tests**

```python
"""Integration tests for the two-phase ToolLoop verify flow."""
from __future__ import annotations

from pathlib import Path

import pytest

from agentd.domain.models import TaskBudget, TaskRecord, TaskStatus, ValidationResult
from agentd.orchestrator.engine import AgentOrchestrator
from agentd.orchestrator.scripted_engine import ScriptedReasoningEngine
from agentd.patch.engine import PatchEngine
from agentd.storage.in_memory import InMemoryTaskStore
from agentd.workspace.shadow import ShadowWorkspaceManager


def _make_plan_raw(test_command: str | None = None) -> dict:
    step: dict = {
        "id": "s1",
        "goal": "Create hello.py",
        "targets": [{"path": "hello.py", "intent": "new"}],
        "risk": "low",
    }
    if test_command:
        step["test_command"] = test_command
    return {
        "analysis": "test",
        "steps": [step],
        "expected_files": ["hello.py"],
        "stop_conditions": ["done"],
    }


def _make_patch_raw(content: str = 'print("hello")') -> dict:
    return {
        "candidates": [{
            "candidate_id": "c1",
            "patch_ops": [{"op": "create_file", "file": "hello.py", "content": content, "reason": "create"}],
        }]
    }


def _make_orchestrator(
    reasoning: ScriptedReasoningEngine,
    tmp_path: Path,
) -> tuple[AgentOrchestrator, InMemoryTaskStore]:
    store = InMemoryTaskStore()
    orchestrator = AgentOrchestrator(
        store=store,
        reasoning_engine=reasoning,
        validator=_NullValidator(),
        patch_engine=PatchEngine(),
        workspace_manager=ShadowWorkspaceManager(tmp_path / "shadows"),
        max_attempts_per_step=2,
    )
    return orchestrator, store


class _NullValidator:
    async def run(self, workspace_path: str) -> ValidationResult:
        return ValidationResult(success=True, diagnostics=[], duration_ms=0)


@pytest.mark.asyncio
async def test_no_test_command_returns_verified_immediately(tmp_path: Path) -> None:
    """Steps without test_command skip verify phase — VerifyResult(verified=True) immediately."""
    reasoning = ScriptedReasoningEngine(
        plan=_make_plan_raw(test_command=None),
        patches=[_make_patch_raw()],
    )
    orchestrator, store = _make_orchestrator(reasoning, tmp_path)

    task = TaskRecord(
        task_id="task-1",
        goal="create hello.py",
        workspace_path=str(tmp_path / "ws"),
    )
    (tmp_path / "ws").mkdir()
    await store.create(task)

    result = await orchestrator.run_task("task-1")
    assert result.status == TaskStatus.READY_FOR_REVIEW
    assert "hello.py" in result.modified_files


@pytest.mark.asyncio
async def test_verify_done_true_completes_step(tmp_path: Path) -> None:
    """emit_patch + verify_done(verified=True) in tool_step_responses."""
    ws = tmp_path / "ws"
    ws.mkdir()

    patch = _make_patch_raw()
    reasoning = ScriptedReasoningEngine(
        plan=_make_plan_raw(test_command="pytest tests/test_hello.py"),
        patches=[patch],
        tool_step_responses=[
            {"type": "emit_patch", "thought": "create file", "patch_ops": patch["candidates"][0]["patch_ops"]},
            {"type": "verify_done", "thought": "tests pass", "verified": True, "test_output": "1 passed"},
        ],
    )
    orchestrator, store = _make_orchestrator(reasoning, tmp_path)
    task = TaskRecord(task_id="task-2", goal="create", workspace_path=str(ws))
    await store.create(task)

    result = await orchestrator.run_task("task-2")
    assert result.status == TaskStatus.READY_FOR_REVIEW


@pytest.mark.asyncio
async def test_verify_done_false_triggers_retry(tmp_path: Path) -> None:
    """verify_done(verified=False) causes engine to restore checkpoint and retry."""
    ws = tmp_path / "ws"
    ws.mkdir()

    patch_ops = [{"op": "create_file", "file": "hello.py", "content": "x=1", "reason": "r"}]
    reasoning = ScriptedReasoningEngine(
        plan=_make_plan_raw(test_command="pytest tests/"),
        patches=[],
        tool_step_responses=[
            # Attempt 1: patch + verify fails
            {"type": "emit_patch", "thought": "attempt 1", "patch_ops": patch_ops},
            {"type": "verify_done", "thought": "failed", "verified": False, "test_output": "1 failed"},
            # Attempt 2: patch + verify passes
            {"type": "emit_patch", "thought": "attempt 2", "patch_ops": patch_ops},
            {"type": "verify_done", "thought": "ok", "verified": True, "test_output": "1 passed"},
        ],
    )
    orchestrator, store = _make_orchestrator(reasoning, tmp_path)
    task = TaskRecord(task_id="task-3", goal="create", workspace_path=str(ws))
    await store.create(task)

    result = await orchestrator.run_task("task-3")
    assert result.status == TaskStatus.READY_FOR_REVIEW


@pytest.mark.asyncio
async def test_patch_apply_failure_stays_in_explore(tmp_path: Path) -> None:
    """When emit_patch patch ops fail to apply, agent stays in explore phase and corrects."""
    ws = tmp_path / "ws"
    ws.mkdir()

    # First emit_patch has a bad search string (search_replace on nonexistent file)
    bad_ops = [{"op": "search_replace", "file": "nonexistent.py", "search": "x", "replace": "y", "reason": "bad"}]
    good_ops = [{"op": "create_file", "file": "hello.py", "content": "x=1", "reason": "correct"}]

    reasoning = ScriptedReasoningEngine(
        plan=_make_plan_raw(test_command=None),
        patches=[],
        tool_step_responses=[
            {"type": "emit_patch", "thought": "bad patch", "patch_ops": bad_ops},
            # Agent sees failure in history, corrects:
            {"type": "emit_patch", "thought": "corrected", "patch_ops": good_ops},
        ],
    )
    orchestrator, store = _make_orchestrator(reasoning, tmp_path)
    task = TaskRecord(task_id="task-4", goal="create", workspace_path=str(ws))
    await store.create(task)

    result = await orchestrator.run_task("task-4")
    assert result.status == TaskStatus.READY_FOR_REVIEW
```

- [ ] **Step 2: Run to confirm failure**

```bash
python -m pytest tests/test_orchestrator_verify_flow.py -x -q
```

Expected: some fail (engine not yet wired, ToolLoop import issues surface here).

- [ ] **Step 3: Fix any wiring issues and run until green**

```bash
python -m pytest tests/test_orchestrator_verify_flow.py -x -q
```

Expected: 4 passed.

- [ ] **Step 4: Run full suite**

```bash
python -m pytest tests/ -x -q \
  --ignore=tests/test_anthropic_transport.py \
  --ignore=tests/test_gemini_transport.py \
  --ignore=tests/test_groq_transport.py \
  --ignore=tests/test_openai_transport.py \
  --ignore=tests/test_huggingface_transport.py \
  --ignore=tests/test_watsonx_transport.py
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add tests/test_orchestrator_verify_flow.py
git commit -m "test(orchestrator): integration tests for two-phase verify loop"
```

---

### Task 11: Type-check and lint

**Files:** None changed — validation pass only.

- [ ] **Step 1: Run mypy**

```bash
cd .worktrees/feat-agentic-planning/services/agentd-py
mypy agentd --ignore-missing-imports
```

Expected: no new errors introduced by these changes.

- [ ] **Step 2: Run ruff**

```bash
ruff check agentd/tools/env.py agentd/tools/loop.py agentd/tools/registry.py agentd/orchestrator/engine.py agentd/reasoning/tool_prompts.py
```

Expected: no errors.

- [ ] **Step 3: Fix any issues and commit**

```bash
git add -u
git commit -m "fix: type and lint cleanup for verify-phase implementation"
```

---

## Summary of files changed

| File | Change |
|------|--------|
| `agentd/domain/models.py` | `TaskBudget.max_verify_calls_per_step: int = 4` |
| `agentd/tools/env.py` | New — `find_binary`, `setup_env` |
| `agentd/tools/files.py` | Add `list_directory` function |
| `agentd/tools/registry.py` | `real_workspace_path`, `definitions(phase=)`, 3 new tools, basename allowlist |
| `agentd/tools/shell.py` | `binary_name_override` param for basename allowlist |
| `agentd/tools/loop.py` | `VerifyResult`, two-phase loop, inline apply, remove `PatchResult` |
| `agentd/reasoning/tool_prompts.py` | `verify_done` schema, PHASES+BINARY DISCOVERY prompt, `build_tool_step_payload(phase=)` |
| `agentd/orchestrator/scripted_engine.py` | `tool_step_responses` for multi-turn verify testing |
| `agentd/orchestrator/engine.py` | Wire `VerifyResult`, remove 5 dead methods, pass `real_workspace_path` |
| `agentd/planning/registry.py` | Use shared `list_directory` from `tools/files.py` |
| `tests/test_tools_env.py` | New — `find_binary`, `setup_env`, `list_directory` tests |
| `tests/test_tools_registry.py` | New — phase-gated tools, basename allowlist |
| `tests/test_orchestrator_verify_flow.py` | New — end-to-end verify loop tests |
