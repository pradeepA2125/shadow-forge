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
