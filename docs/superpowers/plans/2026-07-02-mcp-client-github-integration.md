# P3 — MCP Client + GitHub Integration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect external MCP tool servers (stdio + HTTP/SSE) to the chat controller, their tools callable as `mcp__<server>__<tool>` behind a live `"mcp_tool"` approval gate, configured via `<workspace>/.ai-editor/mcp.json`.

**Architecture:** New `agentd/mcp/` module (mirrors `agentd/skills/`): mtime-cached config loader → `McpConnectionManager` (one background asyncio task per server holding the SDK's async-context-manager session open; `reconcile()` seam for the future P4 settings UI) → `McpToolSource` on the existing `ToolSource`/`AggregatingToolRegistry` composite → a new `"mcp_tool"` `PendingGate` kind mirroring the controller's command gate. Spec: `docs/superpowers/specs/2026-07-02-mcp-client-github-integration-design.md`.

**Tech Stack:** Python (FastAPI, pydantic, official `mcp` SDK v1.x), TypeScript (Zod editor-client, VS Code extension, React webview).

## Global Constraints

- **Controller-only.** The planning/task ReAct path is untouched (spec decision 1).
- **Flag `AI_EDITOR_MCP_ENABLED`, default OFF** (truthy = `1/true/yes/on`). Off: no loader, no connections, no tool source, no prompt block.
- **Per-server allowlist:** an entry connects only with `"enabled": true` in `mcp.json` (spec decision 4).
- **Naming:** `mcp__<server>__<tool>`. Server names must match `^[A-Za-z0-9][A-Za-z0-9_-]*$` and must NOT contain `__` (would break namespace parsing).
- **Env vars:** `AI_EDITOR_MCP_ENABLED` (off) · `AI_EDITOR_MCP_DECISION_TIMEOUT_SEC` (default `0` = wait forever; timeout → reject) · `AI_EDITOR_MCP_TOOLS_MAX_CHARS` (default `16000`, order-truncation) · `AI_EDITOR_MCP_CONNECT_TIMEOUT_SEC` (default `30`) · `AI_EDITOR_MCP_CALL_TIMEOUT_SEC` (default `120`).
- **Degrade-not-raise:** a bad config file / failed server / dead session contributes zero tools or an `is_error` ToolOutput — never a crash.
- **SDK pin: `mcp>=1.20,<2`** — use the **v1 client API** (`streamablehttp_client` returning a 3-tuple). The v2 API (`streamable_http_client`, 2-tuple, httpx-injected) is a future migration, not this plan.
- **Async lifecycle (CRITICAL):** `main.py` calls `select_chat_handler` at module level with **no running event loop**. The manager is *constructed* there but *connects* in a FastAPI `startup` event handler. The SDK's transport/session context managers use anyio cancel scopes that must enter/exit **in the same asyncio task** — hence one dedicated `_serve` task per server; never hold these contexts across tasks with an exit stack.
- **Prompt copy:** no superiority framing — state what MCP tools are and when they shine; never rank them against other tools.
- **Python env:** `cd services/agentd-py && source .venv/bin/activate` before any pytest. Never pipe pytest (masks exit code).
- **TS build order:** after any `apps/editor-client` change, `npm run -w @ai-editor/editor-client build` **before** `npm run -w @ai-editor/vscode-extension typecheck`.
- **Commits:** `type(scope): description`, one logical change each. Do NOT push.

## File Structure

| File | Responsibility |
|---|---|
| `services/agentd-py/agentd/mcp/__init__.py` | empty package marker |
| `services/agentd-py/agentd/mcp/models.py` | `McpServerConfig`, `McpServerStatus` |
| `services/agentd-py/agentd/mcp/config.py` | `McpConfigLoader` (mtime-cached `.ai-editor/mcp.json`), `interpolate_env`, env knobs |
| `services/agentd-py/agentd/mcp/client.py` | `McpConnectionManager` — per-server serve tasks, `reconcile()`, `call_tool()`, statuses |
| `services/agentd-py/agentd/mcp/rules.py` | `McpRuleStore` — remembered `(server, tool)` approvals |
| `services/agentd-py/agentd/mcp/tool_source.py` | `McpToolSource` — namespacing, budget guard, approval, result flattening |
| `services/agentd-py/agentd/domain/models.py` | + `McpToolDecision` |
| `services/agentd-py/agentd/chat/models.py` | + `"mcp_tool"` in `PendingGate.kind` |
| `services/agentd-py/agentd/chat/controller.py` | + `_mcp_approval_cb` / `resolve_mcp` / registry + ctor wiring |
| `services/agentd-py/agentd/chat/controller_prompts.py` | + `_MCP_BLOCK` teaching block |
| `services/agentd-py/agentd/chat/controller_factory.py` | + `is_mcp_enabled`, manager construction |
| `services/agentd-py/agentd/main.py` | + startup/shutdown event handlers |
| `services/agentd-py/agentd/api/routes.py` | + `POST /chat/threads/{id}/mcp-decision`, `mcp_enabled` in `/v1/config` |
| `apps/editor-client/src/contracts/task-contracts.ts` | + gate kind, stream event, `McpToolDecision`, client interface method |
| `apps/editor-client/src/client/http-backend-client.ts` | + `postChatMcpDecision` |
| `apps/vscode-extension/src/controller.ts` | + `handleMcpDecisionFromChat`, SSE pokes, gate-kind union |
| `apps/vscode-extension/src/chat-panel.ts` | + `mcpDecision` webview message → handler |
| `apps/vscode-extension/src/extension.ts` | + wire handler into `ChatPanel` ctor |
| `apps/vscode-extension/webview-ui/src/types.ts` | + `"mcp_tool"` in `LiveGateView.kind` |
| `apps/vscode-extension/webview-ui/src/components/messages/gates/McpGate.tsx` | approval card |
| `apps/vscode-extension/webview-ui/src/components/LiveSlot.tsx` | + dispatch case |

---

### Task 1: `mcp` dependency + models + config loader

**Files:**
- Modify: `services/agentd-py/pyproject.toml` (dependencies list, after `"pyyaml>=6.0"`)
- Create: `services/agentd-py/agentd/mcp/__init__.py` (empty)
- Create: `services/agentd-py/agentd/mcp/models.py`
- Create: `services/agentd-py/agentd/mcp/config.py`
- Test: `services/agentd-py/tests/test_mcp_config.py`

**Interfaces:**
- Produces: `McpServerConfig(name, transport: Literal["stdio","http","sse"], command, args, env, url, headers, enabled)` with `.fingerprint() -> str`; `McpServerStatus(name, state: Literal["connecting","connected","failed","disconnected"], detail, tool_count)`; `McpConfigLoader(workspace_path).load() -> list[McpServerConfig]` (enabled-only); `interpolate_env(mapping) -> dict` raising `McpMissingEnvVar`; `mcp_tools_max_chars() -> int`, `mcp_decision_timeout_sec() -> float`, `mcp_connect_timeout_sec() -> float`, `mcp_call_timeout_sec() -> float`.

- [ ] **Step 1: Install the SDK into the venv and pin it**

```bash
cd "services/agentd-py" && source .venv/bin/activate
pip install 'mcp>=1.20,<2'
python -c "from mcp import ClientSession, StdioServerParameters; from mcp.client.stdio import stdio_client; from mcp.client.streamable_http import streamablehttp_client; from mcp.client.sse import sse_client; from mcp.server.fastmcp import FastMCP; print('ok')"
```
Expected: `ok`. (If the `streamablehttp_client` import fails, the installed version is v2 — re-pin per the error and STOP: flag to the human. The rest of this plan assumes the v1 API.)

In `pyproject.toml`, change the dependencies list entry `"pyyaml>=6.0"` to:
```toml
  "pyyaml>=6.0",
  "mcp>=1.20,<2"
```

- [ ] **Step 2: Write failing tests for models + loader**

Create `tests/test_mcp_config.py`:

```python
"""McpConfigLoader: mtime-cached .ai-editor/mcp.json reader (mirrors the
ProjectInstructionsLoader cache discipline) + ${VAR} interpolation helpers."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentd.mcp.config import (
    McpConfigLoader,
    McpMissingEnvVar,
    interpolate_env,
    mcp_decision_timeout_sec,
    mcp_tools_max_chars,
)


def _write(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / ".ai-editor" / "mcp.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_missing_file_returns_empty(tmp_path: Path):
    assert McpConfigLoader(str(tmp_path)).load() == []


def test_malformed_json_returns_empty(tmp_path: Path):
    p = tmp_path / ".ai-editor" / "mcp.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    assert McpConfigLoader(str(tmp_path)).load() == []


def test_parses_stdio_and_http_entries(tmp_path: Path):
    _write(tmp_path, {"mcpServers": {
        "echo": {"command": "python", "args": ["srv.py"], "env": {"K": "v"}, "enabled": True},
        "gh": {"type": "http", "url": "https://x/mcp", "headers": {"A": "b"}, "enabled": True},
    }})
    cfgs = {c.name: c for c in McpConfigLoader(str(tmp_path)).load()}
    assert cfgs["echo"].transport == "stdio" and cfgs["echo"].command == "python"
    assert cfgs["gh"].transport == "http" and cfgs["gh"].url == "https://x/mcp"


def test_streamable_http_type_alias(tmp_path: Path):
    _write(tmp_path, {"mcpServers": {
        "s": {"type": "streamable-http", "url": "https://x/mcp", "enabled": True}}})
    assert McpConfigLoader(str(tmp_path)).load()[0].transport == "http"


def test_enabled_gate_excludes_absent_and_false(tmp_path: Path):
    # Decision 4: presence in the file is NOT trust — only enabled:true connects.
    _write(tmp_path, {"mcpServers": {
        "on": {"command": "x", "enabled": True},
        "off": {"command": "x", "enabled": False},
        "absent": {"command": "x"},
    }})
    assert [c.name for c in McpConfigLoader(str(tmp_path)).load()] == ["on"]


def test_invalid_names_and_shapes_skipped(tmp_path: Path):
    _write(tmp_path, {"mcpServers": {
        "bad__name": {"command": "x", "enabled": True},   # __ breaks namespacing
        "no-transport": {"enabled": True},                 # neither command nor url
        "ok": {"command": "x", "enabled": True},
    }})
    assert [c.name for c in McpConfigLoader(str(tmp_path)).load()] == ["ok"]


def test_mtime_cache_and_self_update(tmp_path: Path):
    p = _write(tmp_path, {"mcpServers": {"a": {"command": "x", "enabled": True}}})
    loader = McpConfigLoader(str(tmp_path))
    assert [c.name for c in loader.load()] == ["a"]
    assert loader.load() is loader.load()  # cached list object on unchanged mtime
    import os
    p.write_text(json.dumps(
        {"mcpServers": {"b": {"command": "x", "enabled": True}}}), encoding="utf-8")
    os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 5))
    assert [c.name for c in loader.load()] == ["b"]


def test_interpolate_env_resolves_and_raises(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "s3cret")
    assert interpolate_env({"Authorization": "Bearer ${MY_TOKEN}"}) == {
        "Authorization": "Bearer s3cret"}
    monkeypatch.delenv("NOPE_VAR", raising=False)
    with pytest.raises(McpMissingEnvVar, match="NOPE_VAR"):
        interpolate_env({"k": "${NOPE_VAR}"})


def test_env_knob_defaults(monkeypatch):
    monkeypatch.delenv("AI_EDITOR_MCP_TOOLS_MAX_CHARS", raising=False)
    monkeypatch.delenv("AI_EDITOR_MCP_DECISION_TIMEOUT_SEC", raising=False)
    assert mcp_tools_max_chars() == 16000
    assert mcp_decision_timeout_sec() == 0.0
    monkeypatch.setenv("AI_EDITOR_MCP_TOOLS_MAX_CHARS", "500")
    monkeypatch.setenv("AI_EDITOR_MCP_DECISION_TIMEOUT_SEC", "2.5")
    assert mcp_tools_max_chars() == 500
    assert mcp_decision_timeout_sec() == 2.5
```

- [ ] **Step 3: Run to verify failure**

Run: `pytest tests/test_mcp_config.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agentd.mcp'`

- [ ] **Step 4: Implement models + config**

Create `agentd/mcp/__init__.py` (empty file).

Create `agentd/mcp/models.py`:

```python
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class McpServerConfig(BaseModel):
    """One enabled server entry from .ai-editor/mcp.json. `env`/`headers` values may
    contain ${VAR} references — resolved at connect time (never stored resolved)."""
    name: str
    transport: Literal["stdio", "http", "sse"]
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = False

    def fingerprint(self) -> str:
        """Change-detection key for reconcile(): any field change = new fingerprint."""
        return self.model_dump_json()


class McpServerStatus(BaseModel):
    """Queryable per-server connection state (spec: P4 UI serializes this)."""
    name: str
    state: Literal["connecting", "connected", "failed", "disconnected"]
    detail: str = ""
    tool_count: int = 0
```

Create `agentd/mcp/config.py`:

```python
"""Config for the MCP client: .ai-editor/mcp.json loader + env knobs.

Loader mirrors ProjectInstructionsLoader's mtime-cache discipline: cheap NOOP
until the file changes, so a config edit self-updates without a restart;
best-effort — malformed input degrades to [] with a warning, never raises.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from pydantic import ValidationError

from agentd.mcp.models import McpServerConfig

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _pos_int(env: str, default: int) -> int:
    raw = os.getenv(env, "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else default


def _nonneg_float(env: str, default: float) -> float:
    raw = os.getenv(env, "").strip()
    try:
        val = float(raw)
    except ValueError:
        return default
    return val if val >= 0 else default


def mcp_tools_max_chars() -> int:
    return _pos_int("AI_EDITOR_MCP_TOOLS_MAX_CHARS", 16000)


def mcp_decision_timeout_sec() -> float:
    """0 = wait forever (mirrors AI_EDITOR_COMMAND_DECISION_TIMEOUT_SEC)."""
    return _nonneg_float("AI_EDITOR_MCP_DECISION_TIMEOUT_SEC", 0.0)


def mcp_connect_timeout_sec() -> float:
    return _nonneg_float("AI_EDITOR_MCP_CONNECT_TIMEOUT_SEC", 30.0)


def mcp_call_timeout_sec() -> float:
    return _nonneg_float("AI_EDITOR_MCP_CALL_TIMEOUT_SEC", 120.0)


class McpMissingEnvVar(ValueError):
    """A ${VAR} reference in env/headers names an unset environment variable."""


def interpolate_env(mapping: dict[str, str]) -> dict[str, str]:
    """Resolve ${VAR} references against the real process environment. Raises
    McpMissingEnvVar naming the variable — the server then fails to connect with
    a clear message rather than connecting with a blank credential."""
    def _sub(match: re.Match[str]) -> str:
        var = match.group(1)
        val = os.environ.get(var)
        if val is None:
            raise McpMissingEnvVar(var)
        return val

    return {k: _VAR_RE.sub(_sub, v) for k, v in mapping.items()}


class McpConfigLoader:
    def __init__(self, workspace_path: str | Path) -> None:
        self._path = Path(workspace_path) / ".ai-editor" / "mcp.json"
        self._sig: tuple[int, int] | None = None
        self._cached: list[McpServerConfig] = []

    def load(self) -> list[McpServerConfig]:
        try:
            stat = self._path.stat()
        except OSError:
            self._sig, self._cached = None, []
            return self._cached
        sig = (stat.st_mtime_ns, stat.st_size)
        if sig == self._sig:
            return self._cached
        self._cached = self._parse()
        self._sig = sig
        return self._cached

    def _parse(self) -> list[McpServerConfig]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("[mcp] unreadable %s: %s — no servers", self._path, exc)
            return []
        servers = raw.get("mcpServers") if isinstance(raw, dict) else None
        if not isinstance(servers, dict):
            return []
        out: list[McpServerConfig] = []
        for name, entry in servers.items():
            if not isinstance(entry, dict):
                logger.warning("[mcp] server %r: entry is not an object — skipped", name)
                continue
            if not _NAME_RE.match(str(name)) or "__" in str(name):
                logger.warning("[mcp] server %r: invalid name (must match "
                               "[A-Za-z0-9][A-Za-z0-9_-]* and not contain '__') — skipped", name)
                continue
            if entry.get("enabled") is not True:  # decision 4: explicit allowlist
                continue
            transport = str(entry.get("type", "")).strip().lower()
            if transport == "streamable-http":  # MCP spec's name for http
                transport = "http"
            if transport not in ("stdio", "http", "sse"):
                transport = "http" if entry.get("url") else (
                    "stdio" if entry.get("command") else "")
            if (transport == "stdio" and not entry.get("command")) or (
                    transport in ("http", "sse") and not entry.get("url")) or not transport:
                logger.warning("[mcp] server %r: missing command/url for transport — skipped", name)
                continue
            try:
                out.append(McpServerConfig(
                    name=str(name),
                    transport=transport,  # type: ignore[arg-type]
                    command=entry.get("command"),
                    args=[str(a) for a in entry.get("args", []) or []],
                    env={str(k): str(v) for k, v in (entry.get("env") or {}).items()},
                    url=entry.get("url"),
                    headers={str(k): str(v) for k, v in (entry.get("headers") or {}).items()},
                    enabled=True,
                ))
            except ValidationError as exc:
                logger.warning("[mcp] server %r: invalid entry: %s — skipped", name, exc)
        return out
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_mcp_config.py -q`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml agentd/mcp tests/test_mcp_config.py
git commit -m "feat(mcp): config models + mtime-cached .ai-editor/mcp.json loader"
```

---

### Task 2: `McpConnectionManager` (per-server serve tasks + reconcile)

**Files:**
- Create: `services/agentd-py/agentd/mcp/client.py`
- Create: `services/agentd-py/tests/fixtures/mcp_echo_server.py`
- Test: `services/agentd-py/tests/test_mcp_client.py`

**Interfaces:**
- Consumes: `McpConfigLoader`, `McpServerConfig`, `McpServerStatus`, `interpolate_env`, timeouts from Task 1.
- Produces: `McpConnectionManager(loader, session_factory=_open_session)` with `async start()`, `async reconcile(configs)`, `async shutdown()`, `tool_definitions() -> list[ToolDefinition]` (namespaced `mcp__<server>__<tool>`), `statuses() -> list[McpServerStatus]`, `async call_tool(server, tool, args)`, and `McpServerUnavailable` exception. The `session_factory(cfg)` seam is an async context manager yielding an initialized session with `list_tools()`/`call_tool(name, arguments=...)`.

- [ ] **Step 1: Write failing unit tests (fake session factory)**

Create `tests/test_mcp_client.py`:

```python
"""McpConnectionManager: per-server background serve tasks (the SDK's anyio cancel
scopes must enter/exit in the same task), reconcile() diffing, degrade-not-raise."""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from agentd.mcp.client import McpConnectionManager, McpServerUnavailable
from agentd.mcp.models import McpServerConfig


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"desc of {name}"
        self.inputSchema = {"type": "object", "properties": {"q": {"type": "string"}}}


class _FakeListResult:
    def __init__(self, tools):
        self.tools = tools


class _FakeBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeCallResult:
    def __init__(self, text: str, is_error: bool = False) -> None:
        self.content = [_FakeBlock(text)]
        self.isError = is_error


class _FakeSession:
    def __init__(self, tools: list[str]) -> None:
        self._tools = [_FakeTool(t) for t in tools]
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self):
        return _FakeListResult(self._tools)

    async def call_tool(self, name: str, arguments=None):
        self.calls.append((name, dict(arguments or {})))
        return _FakeCallResult(f"ran {name}")


def _factory(sessions: dict[str, _FakeSession]):
    @asynccontextmanager
    async def open_session(cfg: McpServerConfig):
        if cfg.name == "broken":
            raise RuntimeError("handshake failed")
        yield sessions[cfg.name]
    return open_session


def _cfg(name: str) -> McpServerConfig:
    return McpServerConfig(name=name, transport="stdio", command="x", enabled=True)


class _StaticLoader:
    def __init__(self, configs):
        self._configs = configs

    def load(self):
        return self._configs


@pytest.mark.asyncio
async def test_connects_and_exposes_namespaced_tools():
    sessions = {"echo": _FakeSession(["say", "shout"])}
    mgr = McpConnectionManager(_StaticLoader([_cfg("echo")]), session_factory=_factory(sessions))
    await mgr.start()
    names = [d.name for d in mgr.tool_definitions()]
    assert names == ["mcp__echo__say", "mcp__echo__shout"]
    st = {s.name: s for s in mgr.statuses()}
    assert st["echo"].state == "connected" and st["echo"].tool_count == 2
    await mgr.shutdown()


@pytest.mark.asyncio
async def test_failed_server_contributes_zero_tools_without_raising():
    sessions = {"ok": _FakeSession(["t"])}
    mgr = McpConnectionManager(
        _StaticLoader([_cfg("broken"), _cfg("ok")]), session_factory=_factory(sessions))
    await mgr.start()  # must NOT raise
    assert [d.name for d in mgr.tool_definitions()] == ["mcp__ok__t"]
    st = {s.name: s for s in mgr.statuses()}
    assert st["broken"].state == "failed" and "handshake failed" in st["broken"].detail
    await mgr.shutdown()


@pytest.mark.asyncio
async def test_call_tool_routes_to_right_session():
    sessions = {"a": _FakeSession(["t"]), "b": _FakeSession(["t"])}
    mgr = McpConnectionManager(
        _StaticLoader([_cfg("a"), _cfg("b")]), session_factory=_factory(sessions))
    await mgr.start()
    result = await mgr.call_tool("b", "t", {"q": "hi"})
    assert result.content[0].text == "ran t"
    assert sessions["b"].calls == [("t", {"q": "hi"})] and sessions["a"].calls == []
    await mgr.shutdown()


@pytest.mark.asyncio
async def test_call_tool_on_unknown_or_failed_server_raises_unavailable():
    mgr = McpConnectionManager(
        _StaticLoader([_cfg("broken")]), session_factory=_factory({}))
    await mgr.start()
    with pytest.raises(McpServerUnavailable):
        await mgr.call_tool("broken", "t", {})
    with pytest.raises(McpServerUnavailable):
        await mgr.call_tool("never-configured", "t", {})
    await mgr.shutdown()


@pytest.mark.asyncio
async def test_reconcile_stops_removed_and_starts_new():
    sessions = {"a": _FakeSession(["t"]), "b": _FakeSession(["t"])}
    mgr = McpConnectionManager(_StaticLoader([_cfg("a")]), session_factory=_factory(sessions))
    await mgr.start()
    assert [d.name for d in mgr.tool_definitions()] == ["mcp__a__t"]
    await mgr.reconcile([_cfg("b")])  # a removed, b added — the P4 UI seam
    assert [d.name for d in mgr.tool_definitions()] == ["mcp__b__t"]
    st = {s.name: s for s in mgr.statuses()}
    assert set(st) == {"b"}
    await mgr.shutdown()


@pytest.mark.asyncio
async def test_shutdown_disconnects_everything():
    sessions = {"a": _FakeSession(["t"])}
    mgr = McpConnectionManager(_StaticLoader([_cfg("a")]), session_factory=_factory(sessions))
    await mgr.start()
    await mgr.shutdown()
    assert mgr.tool_definitions() == []
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_mcp_client.py -q`
Expected: FAIL — `ImportError` (no `agentd.mcp.client`)

- [ ] **Step 3: Implement the manager**

Create `agentd/mcp/client.py`:

```python
"""Long-lived MCP server connections: one background asyncio task per server.

The official SDK's transports and ClientSession are async context managers whose
anyio cancel scopes MUST be entered and exited in the same asyncio task — so each
server gets a dedicated `_serve` task that owns its contexts for the connection's
whole lifetime and parks on a stop event. Calling `session.call_tool` from other
tasks is fine (memory-stream I/O); only the context enter/exit is task-pinned.

`reconcile(configs)` diffs desired configs against running servers by fingerprint
and starts/stops serve tasks. v1 calls it exactly once (app startup via `start()`);
it exists as a seam so the P4 settings UI can apply config edits without a backend
restart (see docs/superpowers/2026-07-02-mcp-settings-ui-research.md).
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from agentd.mcp.config import (
    McpConfigLoader,
    interpolate_env,
    mcp_call_timeout_sec,
    mcp_connect_timeout_sec,
)
from agentd.mcp.models import McpServerConfig, McpServerStatus
from agentd.tools.registry import ToolDefinition

logger = logging.getLogger(__name__)


class McpServerUnavailable(RuntimeError):
    """The named server is not connected (unknown, failed, or shut down)."""


@asynccontextmanager
async def _open_session(cfg: McpServerConfig):
    """Default session factory: real SDK transport + initialized ClientSession.
    ${VAR} in env/headers resolves here (connect time) — a missing variable makes
    the connect fail with a message naming it, never a blank credential."""
    from mcp import ClientSession

    if cfg.transport == "stdio":
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=cfg.command or "", args=list(cfg.args),
            env=interpolate_env(cfg.env) or None)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    elif cfg.transport == "http":
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(
                cfg.url or "", headers=interpolate_env(cfg.headers) or None) as (
                read, write, _get_session_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    else:  # sse
        from mcp.client.sse import sse_client

        async with sse_client(
                cfg.url or "", headers=interpolate_env(cfg.headers) or None) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


class _ServerHandle:
    def __init__(self, cfg: McpServerConfig) -> None:
        self.cfg = cfg
        self.fingerprint = cfg.fingerprint()
        self.session: object | None = None
        self.tools: list[object] = []
        self.status = McpServerStatus(name=cfg.name, state="connecting")
        self.stop = asyncio.Event()
        self.task: asyncio.Task[None] | None = None


class McpConnectionManager:
    def __init__(self, loader: McpConfigLoader, session_factory=_open_session) -> None:
        self._loader = loader
        self._session_factory = session_factory
        self._handles: dict[str, _ServerHandle] = {}

    async def start(self) -> None:
        """Eager one-shot connect at app startup (FastAPI startup handler —
        the module-level factory has no running event loop)."""
        await self.reconcile(self._loader.load())

    async def reconcile(self, configs: list[McpServerConfig]) -> None:
        desired = {c.name: c for c in configs}
        for name in list(self._handles):
            handle = self._handles[name]
            if name not in desired or desired[name].fingerprint() != handle.fingerprint:
                await self._stop_handle(name)
        ready_events: list[asyncio.Event] = []
        for name, cfg in desired.items():
            if name in self._handles:
                continue
            handle = _ServerHandle(cfg)
            ready = asyncio.Event()
            handle.task = asyncio.create_task(self._serve(handle, ready))
            self._handles[name] = handle
            ready_events.append(ready)
        connect_timeout = mcp_connect_timeout_sec()
        for ready in ready_events:
            try:
                await asyncio.wait_for(ready.wait(), timeout=connect_timeout or None)
            except TimeoutError:
                # Startup must not hang on one dead server; it may still connect later.
                logger.warning("[mcp] a server did not connect within %ss — continuing",
                               connect_timeout)

    async def _serve(self, handle: _ServerHandle, ready: asyncio.Event) -> None:
        try:
            async with self._session_factory(handle.cfg) as session:
                listed = await session.list_tools()
                handle.session = session
                handle.tools = list(listed.tools)
                handle.status = McpServerStatus(
                    name=handle.cfg.name, state="connected", tool_count=len(handle.tools))
                logger.info("[mcp] connected server=%s tools=%d",
                            handle.cfg.name, len(handle.tools))
                ready.set()
                await handle.stop.wait()
                handle.status = McpServerStatus(
                    name=handle.cfg.name, state="disconnected", tool_count=len(handle.tools))
        except Exception as exc:  # degrade-not-raise: bad server = zero tools
            handle.status = McpServerStatus(
                name=handle.cfg.name, state="failed", detail=str(exc))
            logger.warning("[mcp] server %s failed: %s", handle.cfg.name, exc)
        finally:
            handle.session = None
            ready.set()

    async def _stop_handle(self, name: str) -> None:
        handle = self._handles.pop(name, None)
        if handle is None:
            return
        handle.stop.set()
        if handle.task is not None:
            try:
                await handle.task
            except Exception:
                logger.debug("[mcp] serve task for %s errored on stop", name, exc_info=True)

    async def shutdown(self) -> None:
        for name in list(self._handles):
            await self._stop_handle(name)

    def statuses(self) -> list[McpServerStatus]:
        return [h.status for h in self._handles.values()]

    def tool_definitions(self) -> list[ToolDefinition]:
        out: list[ToolDefinition] = []
        for handle in self._handles.values():
            if handle.session is None:
                continue
            for tool in handle.tools:
                name = getattr(tool, "name", "")
                out.append(ToolDefinition(
                    name=f"mcp__{handle.cfg.name}__{name}",
                    description=getattr(tool, "description", None)
                    or f"{name} (MCP server '{handle.cfg.name}')",
                    parameters=getattr(tool, "inputSchema", None)
                    or {"type": "object", "properties": {}},
                ))
        return out

    async def call_tool(self, server: str, tool: str, args: dict[str, object]):
        handle = self._handles.get(server)
        if handle is None or handle.session is None:
            raise McpServerUnavailable(
                f"MCP server '{server}' is not connected"
                + (f" ({handle.status.state}: {handle.status.detail})" if handle else ""))
        return await asyncio.wait_for(
            handle.session.call_tool(tool, arguments=args),
            timeout=mcp_call_timeout_sec() or None)
```

- [ ] **Step 4: Run unit tests**

Run: `pytest tests/test_mcp_client.py -q`
Expected: all PASS

- [ ] **Step 5: Add the real-protocol integration test (stdio round-trip)**

Create `tests/fixtures/mcp_echo_server.py`:

```python
"""Trivial FastMCP stdio server for the real-protocol integration test."""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo")


@mcp.tool()
def echo(text: str) -> str:
    """Echo the input text back."""
    return f"echo: {text}"


if __name__ == "__main__":
    mcp.run()  # stdio transport
```

Append to `tests/test_mcp_client.py`:

```python
@pytest.mark.asyncio
async def test_real_stdio_round_trip():
    """Spec §7 integration test: real subprocess, real protocol — not the stub."""
    import sys
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / "mcp_echo_server.py"
    cfg = McpServerConfig(
        name="echo", transport="stdio",
        command=sys.executable, args=[str(fixture)], enabled=True)
    mgr = McpConnectionManager(_StaticLoader([cfg]))  # default (real) session factory
    await mgr.start()
    try:
        assert [d.name for d in mgr.tool_definitions()] == ["mcp__echo__echo"]
        result = await mgr.call_tool("echo", "echo", {"text": "hi"})
        texts = [getattr(b, "text", "") for b in result.content]
        assert any("echo: hi" in t for t in texts)
    finally:
        await mgr.shutdown()
```

- [ ] **Step 6: Run all client tests**

Run: `pytest tests/test_mcp_client.py -q`
Expected: all PASS (integration test spawns a real python subprocess)

- [ ] **Step 7: Commit**

```bash
git add agentd/mcp/client.py tests/test_mcp_client.py tests/fixtures/mcp_echo_server.py
git commit -m "feat(mcp): connection manager with per-server serve tasks + reconcile seam"
```

---

### Task 3: `McpRuleStore` (remembered approvals)

**Files:**
- Create: `services/agentd-py/agentd/mcp/rules.py`
- Test: `services/agentd-py/tests/test_mcp_rules.py`

**Interfaces:**
- Produces: `McpRuleStore(workspace_path)` with `matches(server, tool) -> bool` and `add(server, tool) -> None`, persisted at `<workspace>/.ai-editor/approved-mcp-tools.json`.

- [ ] **Step 1: Write failing tests**

Create `tests/test_mcp_rules.py`:

```python
"""McpRuleStore: the mcp_tool gate's "Approve & remember (this workspace)" store,
keyed on (server, tool) — the MCP analog of CommandRuleStore."""
from pathlib import Path

from agentd.mcp.rules import McpRuleStore


def test_empty_store_matches_nothing(tmp_path: Path):
    assert McpRuleStore(str(tmp_path)).matches("gh", "create_issue") is False


def test_add_then_match_exact_pair_only(tmp_path: Path):
    store = McpRuleStore(str(tmp_path))
    store.add("gh", "create_issue")
    fresh = McpRuleStore(str(tmp_path))  # persisted, not just in-memory
    assert fresh.matches("gh", "create_issue") is True
    assert fresh.matches("gh", "delete_repo") is False
    assert fresh.matches("other", "create_issue") is False


def test_add_is_idempotent(tmp_path: Path):
    store = McpRuleStore(str(tmp_path))
    store.add("gh", "t")
    store.add("gh", "t")
    assert len(store.load()) == 1


def test_corrupt_file_degrades_to_empty(tmp_path: Path):
    p = tmp_path / ".ai-editor" / "approved-mcp-tools.json"
    p.parent.mkdir(parents=True)
    p.write_text("{nope", encoding="utf-8")
    assert McpRuleStore(str(tmp_path)).matches("a", "b") is False
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_mcp_rules.py -q`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement**

Create `agentd/mcp/rules.py`:

```python
"""Per-workspace store of user-approved (server, tool) MCP pairs — backs the
mcp_tool gate's "Approve & remember (this workspace)" choice. The MCP analog of
CommandRuleStore, keyed on the exact pair (spec §3.4: not a command string)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class McpRuleStore:
    def __init__(self, workspace_path: str | Path) -> None:
        self._path = Path(workspace_path) / ".ai-editor" / "approved-mcp-tools.json"

    def load(self) -> list[dict[str, str]]:
        if not self._path.exists():
            return []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        return [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []

    def matches(self, server: str, tool: str) -> bool:
        return any(r.get("server") == server and r.get("tool") == tool for r in self.load())

    def add(self, server: str, tool: str) -> None:
        rules = self.load()
        if any(r.get("server") == server and r.get("tool") == tool for r in rules):
            return
        rules.append({"server": server, "tool": tool,
                      "added_at": datetime.now(timezone.utc).isoformat()})
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(rules, indent=2), encoding="utf-8")
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_mcp_rules.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add agentd/mcp/rules.py tests/test_mcp_rules.py
git commit -m "feat(mcp): workspace rule store for remembered (server, tool) approvals"
```

---

### Task 4: `McpToolSource` (namespacing, budget, approval, flattening)

**Files:**
- Create: `services/agentd-py/agentd/mcp/tool_source.py`
- Test: `services/agentd-py/tests/test_mcp_tool_source.py`

**Interfaces:**
- Consumes: `McpConnectionManager.tool_definitions()/call_tool()` (Task 2), `mcp_tools_max_chars()` (Task 1), `ToolDefinition`/`ToolOutput` (`agentd/tools/registry.py`), `ToolSource` protocol (`agentd/tools/sources.py`: `name`, `definitions()`, `owns(tool)`, `async execute(tool, args)`).
- Produces: `McpToolSource(manager, approval_callback)` where `approval_callback: async (server: str, tool: str, args: dict) -> bool`; `parse_tool_name(name) -> tuple[str, str] | None`.

- [ ] **Step 1: Write failing tests**

Create `tests/test_mcp_tool_source.py`:

```python
"""McpToolSource: ToolSource over connected MCP servers — mcp__<server>__<tool>
namespacing, order-truncation budget guard, approval gating, text flattening."""
from __future__ import annotations

import pytest

from agentd.mcp.tool_source import McpToolSource, parse_tool_name
from agentd.tools.registry import ToolDefinition


class _Block:
    def __init__(self, text=None):
        if text is not None:
            self.text = text


class _Result:
    def __init__(self, blocks, is_error=False):
        self.content = blocks
        self.isError = is_error


class _StubManager:
    def __init__(self, defs, results=None, raise_on_call=None):
        self._defs = defs
        self._results = results or {}
        self._raise = raise_on_call
        self.calls = []

    def tool_definitions(self):
        return self._defs

    async def call_tool(self, server, tool, args):
        self.calls.append((server, tool, args))
        if self._raise is not None:
            raise self._raise
        return self._results[(server, tool)]


def _def(name, desc="d"):
    return ToolDefinition(name=name, description=desc,
                          parameters={"type": "object", "properties": {}})


async def _approve(server, tool, args):
    return True


async def _reject(server, tool, args):
    return False


def test_parse_tool_name():
    assert parse_tool_name("mcp__gh__create_issue") == ("gh", "create_issue")
    assert parse_tool_name("mcp__gh__list__all") == ("gh", "list__all")  # tool keeps rest
    assert parse_tool_name("mcp__nope") is None
    assert parse_tool_name("read_file") is None


def test_owns_only_mcp_prefix():
    src = McpToolSource(_StubManager([]), _approve)
    assert src.owns("mcp__a__b") is True
    assert src.owns("read_file") is False


def test_definitions_pass_through_and_budget_truncates(monkeypatch):
    defs = [_def(f"mcp__s__t{i}", desc="x" * 200) for i in range(10)]
    src = McpToolSource(_StubManager(defs), _approve)
    assert len(src.definitions()) == 10
    monkeypatch.setenv("AI_EDITOR_MCP_TOOLS_MAX_CHARS", "700")
    kept = src.definitions()
    assert 0 < len(kept) < 10
    assert [d.name for d in kept] == [d.name for d in defs[: len(kept)]]  # order-truncation


@pytest.mark.asyncio
async def test_execute_approved_flattens_text_blocks():
    mgr = _StubManager([_def("mcp__gh__ci")],
                       results={("gh", "ci"): _Result([_Block("made #12"), _Block()])})
    out = await McpToolSource(mgr, _approve).execute("mcp__gh__ci", {"title": "t"})
    assert out.is_error is False
    assert "made #12" in out.output and "non-text" in out.output
    assert mgr.calls == [("gh", "ci", {"title": "t"})]


@pytest.mark.asyncio
async def test_execute_rejected_returns_error_without_calling():
    mgr = _StubManager([_def("mcp__gh__ci")])
    out = await McpToolSource(mgr, _reject).execute("mcp__gh__ci", {})
    assert out.is_error is True and "rejected" in out.output
    assert mgr.calls == []


@pytest.mark.asyncio
async def test_execute_server_error_degrades_to_tool_error():
    mgr = _StubManager([_def("mcp__gh__ci")], raise_on_call=RuntimeError("server died"))
    out = await McpToolSource(mgr, _approve).execute("mcp__gh__ci", {})
    assert out.is_error is True and "server died" in out.output


@pytest.mark.asyncio
async def test_result_isError_maps_to_error_output():
    mgr = _StubManager([_def("mcp__gh__ci")],
                       results={("gh", "ci"): _Result([_Block("boom")], is_error=True)})
    out = await McpToolSource(mgr, _approve).execute("mcp__gh__ci", {})
    assert out.is_error is True and "boom" in out.output
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_mcp_tool_source.py -q`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement**

Create `agentd/mcp/tool_source.py`:

```python
"""McpToolSource — ToolSource over connected MCP servers.

Unlike SkillToolSource's fixed one-tool schema, definitions() is dynamic: whatever
the connected sessions report, namespaced mcp__<server>__<tool>. Real per-tool JSON
schemas flow into tools_json via AggregatingToolRegistry with no extra plumbing.
Every execute() passes through the approval callback (the controller's mcp_tool
gate) before the wire call. Budget guard: order-truncation over the serialized
definitions (query-independent, cache-stable — mirrors select_catalog_for_budget).
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from agentd.mcp.config import mcp_tools_max_chars
from agentd.tools.registry import ToolDefinition, ToolOutput

logger = logging.getLogger(__name__)

_PREFIX = "mcp__"

ApprovalCallback = Callable[[str, str, dict], Awaitable[bool]]


def parse_tool_name(name: str) -> tuple[str, str] | None:
    """mcp__<server>__<tool> → (server, tool); None when malformed. Server names
    can't contain '__' (loader-enforced), so the first '__' after the prefix splits."""
    if not name.startswith(_PREFIX):
        return None
    server, sep, tool = name[len(_PREFIX):].partition("__")
    return (server, tool) if sep and server and tool else None


class McpToolSource:
    name = "mcp"

    def __init__(self, manager: object, approval_callback: ApprovalCallback) -> None:
        self._manager = manager
        self._approve = approval_callback

    def definitions(self) -> list[ToolDefinition]:
        defs: list[ToolDefinition] = self._manager.tool_definitions()  # type: ignore[attr-defined]
        budget = mcp_tools_max_chars()
        out: list[ToolDefinition] = []
        used = 0
        for d in defs:
            size = len(d.model_dump_json())
            if used + size > budget:
                logger.warning("[mcp] tools_json budget (%d chars) hit — dropping %d of %d tools",
                               budget, len(defs) - len(out), len(defs))
                break
            out.append(d)
            used += size
        return out

    def owns(self, tool: str) -> bool:
        return tool.startswith(_PREFIX)

    async def execute(self, tool: str, args: dict[str, object]) -> ToolOutput:
        parsed = parse_tool_name(tool)
        if parsed is None:
            return ToolOutput(output=f"Error: malformed MCP tool name '{tool}'", is_error=True)
        server, tool_name = parsed
        approved = await self._approve(server, tool_name, dict(args))
        if not approved:
            return ToolOutput(
                output=(f"MCP tool call rejected by user: {server}.{tool_name}. "
                        "Do not retry the same call — adapt your approach or ask."),
                is_error=True)
        try:
            result = await self._manager.call_tool(server, tool_name, dict(args))  # type: ignore[attr-defined]
        except Exception as exc:
            return ToolOutput(
                output=f"Error: MCP tool {server}.{tool_name} failed: {exc}", is_error=True)
        return _flatten_result(server, tool_name, result)


def _flatten_result(server: str, tool_name: str, result: object) -> ToolOutput:
    """v1 flattens text blocks; non-text blocks are counted, not rendered (spec §5.5)."""
    parts: list[str] = []
    skipped = 0
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
        else:
            skipped += 1
    if skipped:
        parts.append(f"[{skipped} non-text content block(s) omitted]")
    text_out = "\n".join(parts) if parts else "(empty result)"
    if getattr(result, "isError", False):
        return ToolOutput(
            output=f"MCP tool {server}.{tool_name} returned an error: {text_out}",
            is_error=True)
    return ToolOutput(output=text_out)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_mcp_tool_source.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add agentd/mcp/tool_source.py tests/test_mcp_tool_source.py
git commit -m "feat(mcp): McpToolSource with namespacing, budget guard, approval gating"
```

---

### Task 5: the `"mcp_tool"` gate in `ChatController`

**Files:**
- Modify: `services/agentd-py/agentd/domain/models.py` (immediately after `class CommandDecision`, ~line 183)
- Modify: `services/agentd-py/agentd/chat/models.py:45` (kind Literal)
- Modify: `services/agentd-py/agentd/chat/controller.py` (ctor, `_build_registry`, `_run_loop`, new methods after `resolve_command` ~line 739)
- Test: `services/agentd-py/tests/test_controller_mcp_gate.py`

**Interfaces:**
- Consumes: `McpRuleStore` (Task 3), `McpToolSource` (Task 4), `mcp_decision_timeout_sec` (Task 1), existing gate machinery (`PendingGate`, `set_controller_gate`, `_write_breadcrumb`).
- Produces: `McpToolDecision(approve: bool, remember: bool = False)` in `agentd/domain/models.py`; `ChatController(..., mcp_manager=None)` storing `self._mcp_manager`; `async _mcp_approval_cb(thread_id, channel_id, server, tool, args) -> bool`; `async resolve_mcp(thread_id, decision: McpToolDecision) -> bool`; `_build_registry(..., mcp_approval_cb=None)`.

- [ ] **Step 1: Write failing tests**

Create `tests/test_controller_mcp_gate.py` (harness mirrors `tests/test_controller_command_gate.py` exactly):

```python
"""mcp_tool gate: MCP tool calls in a controller turn pause for live approval —
mirror of the command gate on the same thread-gate machinery (spec §3.4)."""
import asyncio
from pathlib import Path

import pytest

from agentd.chat.controller import ChatController
from agentd.chat.models import PendingGate
from agentd.chat.storage import ChatThreadStore
from agentd.domain.models import McpToolDecision
from agentd.mcp.rules import McpRuleStore
from agentd.orchestrator.broadcaster import EventBroadcaster
from agentd.orchestrator.scripted_engine import ScriptedReasoningEngine


def _controller(tmp_path, store, broadcaster=None, mcp_manager=None):
    return ChatController(
        workspace_path=str(tmp_path),
        reasoning_engine=ScriptedReasoningEngine(None, []),
        thread_store=store, orchestrator=None,
        broadcaster=broadcaster or EventBroadcaster(), retrieval_client=None,
        mcp_manager=mcp_manager)


@pytest.mark.asyncio
async def test_gate_raised_then_approve_resolves(tmp_path: Path):
    store = ChatThreadStore(tmp_path / "c.sqlite3")
    th = store.create_thread(str(tmp_path), title="t")
    ctrl = _controller(tmp_path, store)
    cb_task = asyncio.create_task(ctrl._mcp_approval_cb(
        th.thread_id, f"chat:{th.thread_id}", "gh", "create_issue", {"title": "x"}))
    await asyncio.sleep(0)
    gate = store.get_thread(th.thread_id).pending_controller_gate
    assert gate is not None and gate.kind == "mcp_tool"
    assert gate.payload["server"] == "gh" and gate.payload["tool"] == "create_issue"
    assert gate.payload["args"] == {"title": "x"}

    assert await ctrl.resolve_mcp(th.thread_id, McpToolDecision(approve=True)) is True
    assert await cb_task is True
    assert store.get_thread(th.thread_id).pending_controller_gate is None  # cleared in place


@pytest.mark.asyncio
async def test_reject_returns_false(tmp_path: Path):
    store = ChatThreadStore(tmp_path / "c.sqlite3")
    th = store.create_thread(str(tmp_path), title="t")
    ctrl = _controller(tmp_path, store)
    cb_task = asyncio.create_task(ctrl._mcp_approval_cb(
        th.thread_id, f"chat:{th.thread_id}", "gh", "t", {}))
    await asyncio.sleep(0)
    await ctrl.resolve_mcp(th.thread_id, McpToolDecision(approve=False))
    assert await cb_task is False


@pytest.mark.asyncio
async def test_remember_persists_rule_and_auto_approves_next(tmp_path: Path):
    store = ChatThreadStore(tmp_path / "c.sqlite3")
    th = store.create_thread(str(tmp_path), title="t")
    ctrl = _controller(tmp_path, store)
    cb_task = asyncio.create_task(ctrl._mcp_approval_cb(
        th.thread_id, f"chat:{th.thread_id}", "gh", "t", {}))
    await asyncio.sleep(0)
    await ctrl.resolve_mcp(th.thread_id, McpToolDecision(approve=True, remember=True))
    assert await cb_task is True
    assert McpRuleStore(str(tmp_path)).matches("gh", "t") is True
    # Second call: no gate — remembered rule auto-approves.
    assert await ctrl._mcp_approval_cb(
        th.thread_id, f"chat:{th.thread_id}", "gh", "t", {}) is True
    assert store.get_thread(th.thread_id).pending_controller_gate is None


@pytest.mark.asyncio
async def test_broadcasts_mcp_approval_requested_poke(tmp_path: Path):
    store = ChatThreadStore(tmp_path / "c.sqlite3")
    th = store.create_thread(str(tmp_path), title="t")
    bc = EventBroadcaster()
    ctrl = _controller(tmp_path, store, broadcaster=bc)
    cid = f"chat:{th.thread_id}"
    q = bc.subscribe(cid)
    cb_task = asyncio.create_task(ctrl._mcp_approval_cb(th.thread_id, cid, "gh", "t", {}))
    await asyncio.sleep(0)
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    poke = [e for e in events if e["type"] == "mcp_approval_requested"]
    assert poke and poke[0]["payload"]["server"] == "gh" and poke[0]["payload"]["tool"] == "t"
    await ctrl.resolve_mcp(th.thread_id, McpToolDecision(approve=False))
    await cb_task


@pytest.mark.asyncio
async def test_timeout_rejects(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AI_EDITOR_MCP_DECISION_TIMEOUT_SEC", "0.05")
    store = ChatThreadStore(tmp_path / "c.sqlite3")
    th = store.create_thread(str(tmp_path), title="t")
    ctrl = _controller(tmp_path, store)
    assert await ctrl._mcp_approval_cb(
        th.thread_id, f"chat:{th.thread_id}", "gh", "t", {}) is False
    assert store.get_thread(th.thread_id).pending_controller_gate is None


@pytest.mark.asyncio
async def test_resolve_mcp_no_pending_returns_false_and_clears_orphan(tmp_path: Path):
    store = ChatThreadStore(tmp_path / "c.sqlite3")
    th = store.create_thread(str(tmp_path), title="t")
    ctrl = _controller(tmp_path, store)
    assert await ctrl.resolve_mcp(th.thread_id, McpToolDecision(approve=True)) is False
    # Restart orphan: gate persisted, no in-memory waiter → cleared + breadcrumb.
    store.set_controller_gate(
        th.thread_id, PendingGate(kind="mcp_tool", payload={"server": "s", "tool": "t"}))
    assert await ctrl.resolve_mcp(th.thread_id, McpToolDecision(approve=True)) is False
    assert store.get_thread(th.thread_id).pending_controller_gate is None


@pytest.mark.asyncio
async def test_registry_includes_mcp_source_when_manager_present(tmp_path: Path):
    from agentd.tools.registry import ToolDefinition

    class _StubManager:
        def tool_definitions(self):
            return [ToolDefinition(name="mcp__gh__t", description="d",
                                   parameters={"type": "object", "properties": {}})]

    store = ChatThreadStore(tmp_path / "c.sqlite3")
    store.create_thread(str(tmp_path), title="t")
    ctrl = _controller(tmp_path, store, mcp_manager=_StubManager())

    async def _cb(server, tool, args):
        return True

    registry = ctrl._build_registry(mcp_approval_cb=_cb)
    assert "mcp__gh__t" in [d.name for d in registry.definitions()]
    # No manager → no MCP tools.
    ctrl_off = _controller(tmp_path, store)
    registry_off = ctrl_off._build_registry(mcp_approval_cb=_cb)
    assert not any(d.name.startswith("mcp__") for d in registry_off.definitions())
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_controller_mcp_gate.py -q`
Expected: FAIL — `ImportError: cannot import name 'McpToolDecision'`

- [ ] **Step 3: Implement**

In `agentd/domain/models.py`, directly after the `CommandDecision` class (~line 183), add:

```python
class McpToolDecision(BaseModel):
    """User decision on an mcp_tool approval gate (chat controller). Remember
    persists the exact (server, tool) pair to the workspace McpRuleStore."""
    approve: bool
    remember: bool = False
```

In `agentd/chat/models.py:45`, add `"mcp_tool"` to the Literal:

```python
    kind: Literal["command", "step", "scope", "validation", "mode", "edit", "clarify", "mcp_tool"]
```

In `agentd/chat/controller.py`:

1. Imports: add `McpToolDecision` to the `from agentd.domain.models import ...` line.
2. `__init__` signature: add `mcp_manager: object | None = None,` after `memory_harness`; in the body add:

```python
        # MCP: process-scoped connection manager (None unless AI_EDITOR_MCP_ENABLED —
        # constructed in select_chat_handler, connected in main.py's startup hook).
        self._mcp_manager = mcp_manager
        # thread_id → future for the in-flight mcp_tool gate; same lifecycle as
        # _pending_command.
        self._pending_mcp: dict[str, asyncio.Future[McpToolDecision]] = {}
```

3. `_build_registry` (line 179): add parameter `mcp_approval_cb: object | None = None,` after `active_skills`; before the final `return AggregatingToolRegistry(sources)` add:

```python
        if self._mcp_manager is not None and mcp_approval_cb is not None:
            from agentd.mcp.tool_source import McpToolSource

            sources.append(McpToolSource(self._mcp_manager, mcp_approval_cb))
```

4. `_run_loop`: next to `command_cb = partial(...)` (line ~325) add
   `mcp_cb = partial(self._mcp_approval_cb, thread_id, channel_id)`, and pass
   `mcp_approval_cb=mcp_cb` in the `self._build_registry(...)` call.

5. After `resolve_command` (~line 739), add the two methods (exact mirror of the
   command pair — gate clears in place in the `finally`, decision route only
   `future.set_result`):

```python
    async def _mcp_approval_cb(
        self, thread_id: str, channel_id: str,
        server: str, tool: str, args: dict[str, object],
    ) -> bool:
        """Gate an MCP tool call (mirror of _command_approval_cb on the same
        thread-gate machinery). A remembered (server, tool) rule auto-approves;
        otherwise raise a durable kind="mcp_tool" gate and await /mcp-decision."""
        from agentd.mcp.config import mcp_decision_timeout_sec
        from agentd.mcp.rules import McpRuleStore

        if McpRuleStore(self._workspace_path).matches(server, tool):
            return True

        self._store.set_controller_gate(thread_id, PendingGate(
            kind="mcp_tool", payload={"server": server, "tool": tool, "args": args}))
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[McpToolDecision] = loop.create_future()
        self._pending_mcp[thread_id] = fut
        # Instant-render poke — the card still renders FROM /live (durable on reload).
        self._broadcaster.broadcast(channel_id, {
            "type": "mcp_approval_requested",
            "payload": {"server": server, "tool": tool, "args": args},
        })
        timeout = mcp_decision_timeout_sec()
        try:
            decision = await (asyncio.wait_for(fut, timeout) if timeout > 0 else fut)
        except (TimeoutError, asyncio.TimeoutError):
            decision = McpToolDecision(approve=False)
        finally:
            self._pending_mcp.pop(thread_id, None)
            self._store.set_controller_gate(thread_id, None)

        if decision.approve and decision.remember:
            McpRuleStore(self._workspace_path).add(server, tool)
        self._write_breadcrumb(
            thread_id, channel_id,
            f"✓ MCP tool approved: {server}.{tool}" if decision.approve
            else f"✗ MCP tool rejected: {server}.{tool}")
        return decision.approve

    async def resolve_mcp(self, thread_id: str, decision: McpToolDecision) -> bool:
        """Resolve the mcp_tool gate (POST /mcp-decision). Fires the live waiter;
        never mutates/persists during the await (Class-A). Restart orphan clears
        the stale gate + breadcrumb — mirrors resolve_command."""
        fut = self._pending_mcp.get(thread_id)
        if fut is None or fut.done():
            thread = self._store.get_thread(thread_id)
            gate = thread.pending_controller_gate if thread is not None else None
            if gate is not None and gate.kind == "mcp_tool":
                self._store.set_controller_gate(thread_id, None)
                self._write_breadcrumb(
                    thread_id, f"chat:{thread_id}",
                    "Previous turn ended — please re-send your request.")
            return False
        fut.set_result(decision)
        return True
```

- [ ] **Step 4: Run tests (new + neighbors)**

Run: `pytest tests/test_controller_mcp_gate.py tests/test_controller_command_gate.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add agentd/domain/models.py agentd/chat/models.py agentd/chat/controller.py tests/test_controller_mcp_gate.py
git commit -m "feat(mcp): mcp_tool approval gate in ChatController + registry wiring"
```

---

### Task 6: prompt teaching block

**Files:**
- Modify: `services/agentd-py/agentd/chat/controller_prompts.py` (`_MCP_BLOCK` near `_SKILLS_BLOCK_HEADER` ~line 370; append logic in `format_controller_system_prompt` after the skills-catalog branch ~line 448)
- Test: `services/agentd-py/tests/test_mcp_prompt_block.py`

**Interfaces:**
- Consumes: `format_controller_system_prompt(tool_definitions, *, task_subsystem_enabled=None, memory_enabled=None, project_instructions=None, skills_catalog=None)`.
- Produces: the block auto-appends when any tool definition name starts with `mcp__` — no new parameter, no engine change (the engine already passes the full merged `tool_definitions`).

- [ ] **Step 1: Write failing tests**

Create `tests/test_mcp_prompt_block.py`:

```python
"""The MCP teaching block appends iff MCP tools are present in tool_definitions —
detected from the mcp__ prefix, so the engine needs no new loader parameter."""
from agentd.chat.controller_prompts import format_controller_system_prompt

_BASE = [{"name": "read_file", "description": "d", "parameters": {}}]
_MCP = [{"name": "mcp__github__create_issue", "description": "d", "parameters": {}}]


def _prompt(defs):
    return format_controller_system_prompt(
        defs, task_subsystem_enabled=False, memory_enabled=False)


def test_block_absent_without_mcp_tools():
    assert "EXTERNAL MCP TOOLS" not in _prompt(_BASE)


def test_block_present_with_mcp_tools():
    text = _prompt(_BASE + _MCP)
    assert "EXTERNAL MCP TOOLS" in text
    assert "approval" in text  # teaches the gate pause is expected
    # No superiority framing: the block must not rank tools against each other.
    assert "instead of" not in text.split("EXTERNAL MCP TOOLS")[1].lower()
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_mcp_prompt_block.py -q`
Expected: FAIL on `test_block_present_with_mcp_tools`

- [ ] **Step 3: Implement**

In `agentd/chat/controller_prompts.py`, after `_SKILLS_BLOCK_HEADER`'s definition, add:

```python
_MCP_BLOCK = """

EXTERNAL MCP TOOLS
Tools named `mcp__<server>__<tool>` come from external MCP servers the user
connected (for example GitHub, databases, web services). They act on real
third-party systems and can have side effects — the same weight as run_command,
unlike a local file read. Their parameter schemas are in TOOLS like any other
tool; call one directly when the user's request needs the external system it
exposes.
- Calling one pauses the turn for a live user approval card. That pause is
  expected behavior, not an error — wait for it, do not route around it.
- If the user rejects a call, do not silently retry the same call; adapt your
  approach or ask what they want instead.
"""
```

In `format_controller_system_prompt`, after the `if skills_catalog:` branch and before `return base`, add:

```python
    # MCP teaching block: keyed off the merged tool definitions themselves (the
    # mcp__ namespace), so no separate loader/flag parameter is needed and the
    # block stays in lockstep with what tools_json actually contains.
    if any(str((d or {}).get("name", "")).startswith("mcp__")
           for d in tool_definitions if isinstance(d, dict)):
        base += _MCP_BLOCK
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_mcp_prompt_block.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add agentd/chat/controller_prompts.py tests/test_mcp_prompt_block.py
git commit -m "feat(mcp): controller system-prompt teaching block for external MCP tools"
```

---

### Task 7: flag, factory + `main.py` wiring, routes

**Files:**
- Modify: `services/agentd-py/agentd/chat/controller_factory.py` (new `is_mcp_enabled` after `is_skills_enabled`; wiring inside `select_chat_handler`)
- Modify: `services/agentd-py/agentd/main.py` (after `_chat_agent = ...` ~line 383)
- Modify: `services/agentd-py/agentd/api/routes.py` (`/v1/config` dict ~line 211; new route after `post_chat_command_decision` ~line 1457)
- Test: `services/agentd-py/tests/test_mcp_flag_wiring.py`

**Interfaces:**
- Consumes: `McpConfigLoader` (Task 1), `McpConnectionManager` (Task 2), `resolve_mcp`/`McpToolDecision` (Task 5).
- Produces: `is_mcp_enabled() -> bool`; `ChatController._mcp_manager` set when enabled; `POST /v1/chat/threads/{thread_id}/mcp-decision` → `{"ok": bool}`; `"mcp_enabled"` in `GET /v1/config`.

- [ ] **Step 1: Write failing tests**

Create `tests/test_mcp_flag_wiring.py`:

```python
"""AI_EDITOR_MCP_ENABLED: default OFF; ON builds the manager into the controller.
Route: POST /chat/threads/{id}/mcp-decision resolves via the handler's resolve_mcp."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agentd.api.routes import build_router
from agentd.chat.controller_factory import is_mcp_enabled, select_chat_handler
from agentd.storage.in_memory import InMemoryTaskStore
from agentd.workspace.shadow import ShadowWorkspaceManager


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("AI_EDITOR_MCP_ENABLED", raising=False)
    assert is_mcp_enabled() is False


@pytest.mark.parametrize("raw,expected", [
    ("1", True), ("true", True), ("YES", True), ("on", True),
    ("0", False), ("false", False), ("", False),
])
def test_flag_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("AI_EDITOR_MCP_ENABLED", raw)
    assert is_mcp_enabled() is expected


def _handler(tmp_path, monkeypatch):
    from agentd.chat.storage import ChatThreadStore
    monkeypatch.setenv("AI_EDITOR_CHAT_CONTROLLER", "1")
    return select_chat_handler(
        workspace_path=str(tmp_path),
        transport=object(), model="m",
        thread_store=ChatThreadStore(tmp_path / "c.sqlite3"),
        orchestrator=None, broadcaster=object())


def test_factory_off_no_manager(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("AI_EDITOR_MCP_ENABLED", raising=False)
    assert _handler(tmp_path, monkeypatch)._mcp_manager is None


def test_factory_on_builds_manager(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AI_EDITOR_MCP_ENABLED", "1")
    from agentd.mcp.client import McpConnectionManager
    handler = _handler(tmp_path, monkeypatch)
    assert isinstance(handler._mcp_manager, McpConnectionManager)


class _StubChatHandler:
    """Only what the chat route registration + this route touch."""
    def __init__(self):
        self.calls = []
        self._store = None
        self._broadcaster = None

    async def resolve_mcp(self, thread_id, decision):
        self.calls.append((thread_id, decision))
        return True


@pytest.mark.asyncio
async def test_mcp_decision_route(tmp_path: Path):
    stub = _StubChatHandler()
    app = FastAPI()
    app.include_router(build_router(
        store=InMemoryTaskStore(), orchestrator=None,
        workspace_manager=ShadowWorkspaceManager(root_path=tmp_path / "s"),
        retrieval_client=None, chat_agent=stub))
    async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post("/v1/chat/threads/th1/mcp-decision",
                                 json={"approve": True, "remember": True})
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    (thread_id, decision), = stub.calls
    assert thread_id == "th1" and decision.approve is True and decision.remember is True
```

Note: if `build_router` requires a non-None `orchestrator`, mirror the `_make_orch` helper at the top of `tests/test_command_decision_api.py` (NoopReasoning + AlwaysPassValidator + PatchEngine + ShadowWorkspaceManager) instead of `orchestrator=None` — that file is the canonical harness for router tests.

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_mcp_flag_wiring.py -q`
Expected: FAIL — `ImportError: cannot import name 'is_mcp_enabled'`

- [ ] **Step 3: Implement factory + main + routes**

In `agentd/chat/controller_factory.py`, after `is_skills_enabled`:

```python
def is_mcp_enabled() -> bool:
    """Whether external MCP servers from .ai-editor/mcp.json are connected and
    offered to the controller. Default OFF — external tool execution, ship dark.
    Opt in with AI_EDITOR_MCP_ENABLED=1."""
    return os.getenv("AI_EDITOR_MCP_ENABLED", "0").strip().lower() in _TRUTHY
```

In `select_chat_handler`, after the `skill_catalog_loader = ...` block:

```python
        # MCP servers (default off): the manager is CONSTRUCTED here (frozen
        # workspace_path, mirrors the other loaders) but CONNECTS in main.py's
        # startup event handler — this factory runs at module import with no
        # event loop, and the SDK's transports need one (spec §3.2/§3.6).
        mcp_manager = None
        if is_mcp_enabled():
            from agentd.mcp.client import McpConnectionManager
            from agentd.mcp.config import McpConfigLoader

            mcp_manager = McpConnectionManager(McpConfigLoader(workspace_path))
```

and pass `mcp_manager=mcp_manager,` in the `ChatController(...)` call.

In `agentd/main.py`, after the `_chat_agent = select_chat_handler(...)` statement (line ~383) and before `warn_if_incoherent_flags`:

```python
# MCP servers connect once per process at APP STARTUP, not at construction — this
# module runs synchronously at import (no event loop), and the SDK's stdio/http
# transports are async context managers held open by per-server tasks. Shutdown
# mirrors it so stdio subprocesses die with us.
_mcp_manager = getattr(_chat_agent, "_mcp_manager", None)
if _mcp_manager is not None:
    app.add_event_handler("startup", _mcp_manager.start)
    app.add_event_handler("shutdown", _mcp_manager.shutdown)
```

In `agentd/api/routes.py`:

1. `/v1/config` handler (~line 203): add `is_mcp_enabled` to the existing `from agentd.chat.controller_factory import ...` line and `"mcp_enabled": is_mcp_enabled(),` to the returned dict next to `"skills_enabled"`.
2. Add `McpToolDecision` to the `from agentd.domain.models import ...` block at the top of the file.
3. After the `post_chat_command_decision` route (~line 1457), add:

```python
        @router.post("/chat/threads/{thread_id}/mcp-decision")
        async def post_chat_mcp_decision(
            thread_id: str, request: McpToolDecision,
        ) -> dict:
            # Resolves the held-open mcp_tool gate. The continuation surfaces on the
            # already-open message SSE stream (the loop resumes) — plain JSON ack,
            # mirrors /command-decision (future.set_result).
            resolve = getattr(_chat_agent, "resolve_mcp", None)
            if resolve is None:
                return {"ok": False}
            ok = await resolve(thread_id, request)  # type: ignore[misc]
            return {"ok": ok}
```

- [ ] **Step 4: Run tests + the full backend suite + typecheck**

```bash
pytest tests/test_mcp_flag_wiring.py -q
pytest -q
ruff check agentd/mcp agentd/chat/controller.py agentd/chat/controller_factory.py agentd/api/routes.py
mypy agentd/mcp
```
Expected: new tests PASS; full suite green (a shifting failure set = pre-existing/env — reproduce in isolation before attributing, per CLAUDE.md); ruff/mypy clean.

- [ ] **Step 5: Commit**

```bash
git add agentd/chat/controller_factory.py agentd/main.py agentd/api/routes.py tests/test_mcp_flag_wiring.py
git commit -m "feat(mcp): AI_EDITOR_MCP_ENABLED flag, startup connect wiring, mcp-decision route"
```

---

### Task 8: editor-client contracts + client method

**Files:**
- Modify: `apps/editor-client/src/contracts/task-contracts.ts` (`PendingGateSchema` ~line 248; `StreamEvent` union ~line 179; `BackendTaskClient` interface — find via `postChatCommandDecision`; config schema — find via `skillsEnabled`)
- Modify: `apps/editor-client/src/client/http-backend-client.ts` (next to `postChatCommandDecision` ~line 236)
- Test: extend the existing contracts test file (find via `grep -rn "PendingGateSchema" apps/editor-client/src --include=*.test.ts`; if none exists, create `apps/editor-client/src/contracts/mcp-gate.test.ts`)

**Interfaces:**
- Produces: `"mcp_tool"` in `PendingGateSchema.kind`; `{ type: "mcp_approval_requested"; payload: { server: string; tool: string; args: Record<string, unknown> } }` in `StreamEvent`; `interface McpToolDecision { approve: boolean; remember: boolean }` (exported); `postChatMcpDecision(threadId: string, decision: McpToolDecision): Promise<void>` on `BackendTaskClient` + `HttpBackendClient`; `mcpEnabled: boolean` on the config schema (default false).

- [ ] **Step 1: Write the failing test**

```typescript
import { describe, expect, it } from "vitest";
import { PendingGateSchema } from "../contracts/task-contracts";

describe("mcp_tool gate contract", () => {
  it("parses a kind=mcp_tool pending gate (a kind missing from the Zod enum makes the /live parse throw and the gate silently never renders)", () => {
    const gate = PendingGateSchema.parse({
      kind: "mcp_tool",
      payload: { server: "gh", tool: "create_issue", args: { title: "x" } },
    });
    expect(gate.kind).toBe("mcp_tool");
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `npm run -w @ai-editor/editor-client test`
Expected: FAIL — invalid enum value

- [ ] **Step 3: Implement**

1. `PendingGateSchema` kind enum: `z.enum(["command", "step", "scope", "validation", "mode", "edit", "clarify", "mcp_tool"])`.
2. `StreamEvent` union, directly after the `command_approval_requested` member (line ~179):
   ```typescript
   | { type: "mcp_approval_requested"; payload: { server: string; tool: string; args: Record<string, unknown> } }
   ```
3. Near `CommandDecision`'s type definition (grep for it), add and export:
   ```typescript
   export interface McpToolDecision {
     approve: boolean;
     remember: boolean;
   }
   ```
4. `BackendTaskClient` interface: next to `postChatCommandDecision`, add
   `postChatMcpDecision(threadId: string, decision: McpToolDecision): Promise<void>;`
5. Config schema: grep for `skillsEnabled` in `task-contracts.ts`; add `mcpEnabled` beside it with the same optional/default pattern used there, and mirror the snake→camel mapping (`mcp_enabled` → `mcpEnabled`) wherever `skills_enabled` is mapped in `http-backend-client.ts`.
6. `http-backend-client.ts`, after `postChatCommandDecision` (~line 236):
   ```typescript
   async postChatMcpDecision(threadId: string, decision: McpToolDecision): Promise<void> {
     await this.fetchJson(
       `/v1/chat/threads/${encodeURIComponent(threadId)}/mcp-decision`,
       {
         method: "POST",
         body: JSON.stringify({ approve: decision.approve, remember: decision.remember }),
       }
     );
   }
   ```
   (add `McpToolDecision` to the contracts import).

- [ ] **Step 4: Test + build**

```bash
npm run -w @ai-editor/editor-client test
npm run -w @ai-editor/editor-client build
```
Expected: tests PASS; build clean (build is REQUIRED before Task 9's typecheck).

- [ ] **Step 5: Commit**

```bash
git add apps/editor-client
git commit -m "feat(mcp): editor-client contracts — mcp_tool gate kind, decision method, stream event"
```

---

### Task 9: extension host + webview gate card

**Files:**
- Modify: `apps/vscode-extension/src/controller.ts` (gate-kind union line 90; SSE handlers at ~line 797 and ~line 999; new method after `handleCommandDecisionFromChat` ~line 1463)
- Modify: `apps/vscode-extension/src/chat-panel.ts` (ctor params ~line 44-47 region; message dispatch ~line 151)
- Modify: `apps/vscode-extension/src/extension.ts` (ChatPanel ctor args ~line 21-48)
- Modify: `apps/vscode-extension/webview-ui/src/types.ts:56` (`LiveGateView.kind`)
- Create: `apps/vscode-extension/webview-ui/src/components/messages/gates/McpGate.tsx`
- Modify: `apps/vscode-extension/webview-ui/src/components/LiveSlot.tsx` (import + dispatch case)
- Test: `apps/vscode-extension/webview-ui/src/test/gates.test.tsx` (append) — and if `webview-ui` has its own test script, run it; otherwise the root `npm run test` covers the extension workspace

**Interfaces:**
- Consumes: `postChatMcpDecision` + `McpToolDecision` (Task 8), gate payload `{server, tool, args}` (Task 5).
- Produces: webview message `{ type: "mcpDecision", threadId, approve, remember }`; `AiEditorController.handleMcpDecisionFromChat(threadId, decision)`.

- [ ] **Step 1: Write the failing webview test**

Append to `apps/vscode-extension/webview-ui/src/test/gates.test.tsx` (mirror the existing gate tests' render/assert style in that file — imports, `render`, and the vscode postMessage mock are already set up there):

```tsx
describe("McpGate", () => {
  it("renders server.tool + args and posts mcpDecision on approve", () => {
    render(
      <McpGate
        taskId="th1"
        payload={{ server: "gh", tool: "create_issue", args: { title: "bug" } }}
      />
    );
    expect(screen.getByText(/Call MCP tool: gh\.create_issue/)).toBeTruthy();
    expect(screen.getByText(/"title": "bug"/)).toBeTruthy();
    fireEvent.click(screen.getByText("Approve once"));
    expect(postMessageMock).toHaveBeenCalledWith({
      type: "mcpDecision", threadId: "th1", approve: true, remember: false,
    });
  });

  it("reject posts approve=false", () => {
    render(<McpGate taskId="th1" payload={{ server: "s", tool: "t", args: {} }} />);
    fireEvent.click(screen.getByText("Reject"));
    expect(postMessageMock).toHaveBeenCalledWith({
      type: "mcpDecision", threadId: "th1", approve: false, remember: false,
    });
  });
});
```

(Import `McpGate` at the top of the test file; reuse the file's existing `postMessageMock` name — check the first `CommandGate` test in the file and match its mock variable exactly.)

- [ ] **Step 2: Run to verify failure**

Run: `npm run -w @ai-editor/vscode-extension test`
Expected: FAIL — cannot resolve `McpGate`

- [ ] **Step 3: Implement webview side**

`webview-ui/src/types.ts:56` — add the kind:

```typescript
  kind: "command" | "scope" | "validation" | "step" | "mode" | "edit" | "clarify" | "mcp_tool";
```

Create `webview-ui/src/components/messages/gates/McpGate.tsx`:

```tsx
import { useState } from "react";
import { vscode } from "../../../vscodeApi";
import { CardShell } from "../../shared/CardShell";
import { BtnDanger, BtnGhost, BtnPrimary } from "../../shared/buttons";

interface Props {
  /** Carries the threadId (controller gates have no task — LiveSlot passes activeTaskId ?? threadId). */
  taskId: string;
  payload: Record<string, unknown>;
}

/**
 * McpGate — approval card for an external MCP tool call (kind="mcp_tool").
 * Copy is "Call MCP tool: server.tool" (NOT "Run command:") — spec decision 7.
 * Approve & remember persists the exact (server, tool) pair for this workspace.
 */
export function McpGate({ taskId, payload }: Props) {
  const server = String(payload.server ?? "");
  const tool = String(payload.tool ?? "");
  const args = (payload.args ?? {}) as Record<string, unknown>;
  const [resolved, setResolved] = useState<string | null>(null);

  function submit(approve: boolean, remember: boolean) {
    if (resolved !== null) return; // one-shot guard
    setResolved(approve ? (remember ? "Approved & remembered" : "Approved") : "Rejected");
    vscode.postMessage({ type: "mcpDecision", threadId: taskId, approve, remember });
  }

  return (
    <CardShell
      icon="terminal"
      title={`Call MCP tool: ${server}.${tool}`}
      subtitle="External MCP server — review the arguments before approving"
      borderColor="var(--accent-brd)"
      headerTint="linear-gradient(180deg, var(--accent-bg), transparent)"
    >
      <pre className="max-h-40 overflow-auto px-2.5 py-2 text-[11px] text-text-2 border-t border-border whitespace-pre-wrap">
        {JSON.stringify(args, null, 2)}
      </pre>
      {resolved === null ? (
        <div className="flex flex-wrap items-center gap-1.5 px-2.5 py-2 border-t border-border">
          <BtnPrimary onClick={() => submit(true, false)}>Approve once</BtnPrimary>
          <BtnGhost onClick={() => submit(true, true)}>
            Approve &amp; remember (this workspace)
          </BtnGhost>
          <BtnDanger onClick={() => submit(false, false)}>Reject</BtnDanger>
        </div>
      ) : (
        <div className="px-2.5 py-2 text-[12px] text-text-3 border-t border-border">{resolved}</div>
      )}
    </CardShell>
  );
}
```

(Check the `icon` prop value against the names the `Icon` component supports — use whatever `CommandGate.tsx` passes if `"terminal"` isn't one.)

`LiveSlot.tsx` — add the import next to `CommandGate` and the dispatch case in `GateDispatch`:

```tsx
import { McpGate } from "./messages/gates/McpGate";
// ... in the switch:
    case "mcp_tool":
      return <McpGate taskId={taskId} payload={payload} />;
```

- [ ] **Step 4: Implement extension-host side**

`src/controller.ts`:
1. Line 90 union: append `| "mcp_tool"` to the `kind` union in the extension's own `LiveGateView`.
2. Import `McpToolDecision` from `@ai-editor/editor-client` alongside `CommandDecision`.
3. Chat SSE handler (~line 797, after the `command_approval_requested` branch):
   ```typescript
        } else if (event.type === "mcp_approval_requested") {
          this.forwardGateWait("mcp_tool");
   ```
4. Task-SSE handler (~line 999): same two lines after its `command_approval_requested` branch. (If `forwardGateWait`'s parameter is a literal-union type, add `"mcp_tool"` there too.)
5. After `handleCommandDecisionFromChat` (~line 1463):
   ```typescript
  async handleMcpDecisionFromChat(
    threadId: string,
    decision: McpToolDecision
  ): Promise<void> {
    try {
      // mcp_tool gates are controller-only (no task path) — always the chat route.
      await this.clientForChat().postChatMcpDecision(threadId, decision);
    } catch (err) {
      if (this.isBenignConflict(err)) return;
      this.ui.showError(
        `Failed to send MCP decision: ${err instanceof Error ? err.message : String(err)}`
      );
    }
  }
   ```

`src/chat-panel.ts`:
1. Add a ctor parameter at the END of the parameter list (positional ctor — never insert mid-list):
   `private readonly onMcpDecision: (threadId: string, decision: McpToolDecision) => Promise<void>,` — match the surrounding parameter style, import the type.
2. Message dispatch (next to the `commandDecision` branch, ~line 151):
   ```typescript
      } else if (m["type"] === "mcpDecision") {
        p = this.onMcpDecision(m["threadId"] as string, {
          approve: m["approve"] === true,
          remember: m["remember"] === true,
        });
   ```

`src/extension.ts` — append the matching arg at the END of the `new ChatPanel(...)` argument list (~line 47):
```typescript
    (threadId, decision) => controller.handleMcpDecisionFromChat(threadId, decision),
```

- [ ] **Step 5: Build + test + typecheck**

```bash
npm run build
npm run -w @ai-editor/vscode-extension test
npm run typecheck
```
Expected: all green. (If the webview has a separate Vite build script for `webview-ui/dist`, run it too — grep `apps/vscode-extension/package.json` scripts; a stale `webview-ui/dist` is the documented frontend-smoke footgun.)

- [ ] **Step 6: Commit**

```bash
git add apps/vscode-extension
git commit -m "feat(mcp): McpGate approval card + mcpDecision plumbing through extension host"
```

---

### Task 10: docs + full verification

**Files:**
- Modify: `CLAUDE.md` (new subsection after "Agent Skills (P2, copilot-parity roadmap)")
- Modify: `docs/superpowers/2026-06-29-feature-roadmap-copilot-parity.md` (mark P3 implemented if the roadmap tracks status)

- [ ] **Step 1: Document in CLAUDE.md**

Add after the Agent Skills section, following its exact style:

```markdown
#### MCP client (P3, copilot-parity roadmap)

External MCP tool servers (stdio + HTTP/SSE) connected from `<workspace>/.ai-editor/mcp.json`,
tools callable as `mcp__<server>__<tool>` behind a live `"mcp_tool"` approval gate. Flag-gated,
**default OFF** (`AI_EDITOR_MCP_ENABLED`), **controller-only**. Spec/plan:
`docs/superpowers/specs/2026-07-02-mcp-client-github-integration-design.md` +
`…/plans/2026-07-02-mcp-client-github-integration.md`.

- **Config (`agentd/mcp/config.py::McpConfigLoader`):** mtime-cached `.ai-editor/mcp.json`
  (`{"mcpServers": {name: {command/args/env | type+url/headers, "enabled": true}}}`). An entry
  connects ONLY with explicit `"enabled": true` (allowlist beyond presence). `${VAR}` in
  env/headers resolves against the process environment at connect time; a missing var fails
  that server's connect with a message naming it. Malformed file/entry → skipped with a
  warning, never a crash. Server names: `[A-Za-z0-9][A-Za-z0-9_-]*`, no `__`.
- **Connections (`agentd/mcp/client.py::McpConnectionManager`):** official `mcp` SDK (v1 API,
  pinned `<2`). One background asyncio task per server owns the transport+session context
  managers (anyio cancel scopes are task-pinned) and parks on a stop event. **Connects at app
  startup via `app.add_event_handler("startup", manager.start)`** — NOT at factory time
  (module-level, no event loop). `reconcile(configs)` is the P4 settings-UI seam; per-server
  `McpServerStatus` is queryable. Failed server = zero tools + warning (degrade-not-raise).
- **Tools (`agentd/mcp/tool_source.py::McpToolSource`):** dynamic `definitions()` from
  `list_tools()`, namespaced `mcp__<server>__<tool>`; schemas ride `tools_json` via the
  existing `AggregatingToolRegistry` seam. Budget: `AI_EDITOR_MCP_TOOLS_MAX_CHARS` (default
  16000, order-truncation). Results: text blocks flattened; non-text counted-not-rendered;
  `isError` → `ToolOutput(is_error=True)` — the loop adapts, never crashes.
- **Gate:** every call raises `PendingGate(kind="mcp_tool", payload={server, tool, args})`
  (Class-A: renders from `/live`, survives reload; `mcp_approval_requested` SSE is only the
  instant-render poke). Resolved by `POST /v1/chat/threads/{id}/mcp-decision`
  `{approve, remember}`; remember persists the exact `(server, tool)` pair to
  `.ai-editor/approved-mcp-tools.json` (`McpRuleStore`) — auto-approves next time.
  `AI_EDITOR_MCP_DECISION_TIMEOUT_SEC` (default 0 = wait forever; timeout → reject).
  **`PendingGate.kind` gained `"mcp_tool"` in BOTH `chat/models.py` AND the editor-client
  Zod enum** (the `.min(1)`-class footgun) **AND webview `types.ts`**.
- **Prompt:** `_MCP_BLOCK` teaching block auto-appends when any tool def name starts with
  `mcp__` (detected from `tool_definitions` — no loader param). Teaches: external/side-
  effecting + the approval pause is expected, not an error.
- **Env:** `AI_EDITOR_MCP_ENABLED` (off) · `AI_EDITOR_MCP_DECISION_TIMEOUT_SEC` (0) ·
  `AI_EDITOR_MCP_TOOLS_MAX_CHARS` (16000) · `AI_EDITOR_MCP_CONNECT_TIMEOUT_SEC` (30) ·
  `AI_EDITOR_MCP_CALL_TIMEOUT_SEC` (120). `/v1/config` exposes `mcp_enabled`.
- **GitHub:** proof-via-user-config (no bundled entry): an `mcp.json` entry for the official
  GitHub MCP server with a `${GITHUB_PAT}` header, verified live end-to-end.
```

Also add the five env vars to the "Python backend env vars" **Core** list, one line each, matching the skills entries' style.

- [ ] **Step 2: Full verification (all three stacks)**

```bash
cd services/agentd-py && source .venv/bin/activate
pytest -q            # read the FAILED/summary lines — never pipe
ruff check . && mypy agentd
cd ../.. && npm run build && npm run test && npm run typecheck
```
Expected: everything green. Investigate any failure in isolation before attributing.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/superpowers/2026-06-29-feature-roadmap-copilot-parity.md
git commit -m "docs(mcp): document P3 MCP client architecture in CLAUDE.md"
```

---

### Task 11 (manual, not CI): live smoke — GitHub end-to-end

Spec §7 "Live smoke" + §8 exit criteria. Human-in-the-loop; run from the repo root.

- [ ] 1. In a test workspace, write `.ai-editor/mcp.json`:
```json
{
  "mcpServers": {
    "everything": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-everything"],
      "enabled": true
    },
    "github": {
      "type": "http",
      "url": "https://api.githubcopilot.com/mcp/",
      "headers": { "Authorization": "Bearer ${GITHUB_PAT}" },
      "enabled": true
    }
  }
}
```
Export `GITHUB_PAT` (fine-grained, repo-scoped) in the backend env — never in the file.
- [ ] 2. Start the backend with `AI_EDITOR_MCP_ENABLED=1 AI_EDITOR_CHAT_CONTROLLER=1` via `start-backend.sh` (quote `--workspace`). Log shows `[mcp] connected server=… tools=N` for both.
- [ ] 3. Drive a chat request that needs GitHub ("read issue #1 of <repo> and summarize") → `mcp_tool` gate card renders → Approve → result lands in the transcript.
- [ ] 4. Reject path: repeat, Reject at the gate → the loop adapts (no crash, no silent retry of the identical call).
- [ ] 5. Remember path: Approve & remember → `.ai-editor/approved-mcp-tools.json` written → the same tool next turn runs without a gate.
- [ ] 6. Kill-switch: restart with `AI_EDITOR_MCP_ENABLED=0` → no `[mcp]` connects, no `mcp__` tools in the controller artifacts' `tools_json`, no teaching block.
- [ ] 7. Allowlist: set `"enabled": false` on a server → NOT connected, zero tools (decision 4 holding).
- [ ] 8. Reload-durability: raise a gate, reload the dev-host window → the card re-renders from `/live`.

---

## Self-Review (performed while writing)

- **Spec coverage:** §3.1 config → Task 1; §3.2 client (+ reconcile seam amendment) → Task 2; §3.3 tool source/namespacing/tools_json → Task 4 (+2); §3.4 gate + timeout env + remember-rule → Tasks 3+5 (route in 7); §3.5 teaching block + budget → Tasks 6+4; §3.6 flag/factory → Task 7; §5 data flow → Tasks 4+5; §6 error handling → Tasks 1/2/4 tests; §7 tests incl. real-stdio integration → Tasks 1-9, live smoke → Task 11; §8 exit criteria → Tasks 10+11. UI copy "Call MCP tool:" (decision 7) → Task 9.
- **Deliberate deviations:** none from the spec; the spec's §3.2 reconcile amendment (added 2026-07-02) is implemented as written.
- **Type consistency:** `McpToolDecision(approve, remember)` used identically in Tasks 5/7/8/9; `mcp__<server>__<tool>` naming consistent across Tasks 2/4/6; `session_factory` seam name consistent in Task 2 code+tests; gate payload `{server, tool, args}` identical in Tasks 5/8/9.
- **Known judgment calls for the implementer:** exact insertion lines drift with the codebase — anchor on the named neighbors (e.g. "after `resolve_command`"), not absolute line numbers; the two grep-anchored spots in Task 8 (config schema `skillsEnabled`, contracts test location) are verified-by-pattern rather than pinned lines.
