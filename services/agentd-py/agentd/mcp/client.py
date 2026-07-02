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
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

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
async def _open_session(cfg: McpServerConfig) -> AsyncIterator[Any]:
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
        self.session: Any = None
        self.tools: list[Any] = []
        self.status = McpServerStatus(name=cfg.name, state="connecting")
        self.stop = asyncio.Event()
        self.task: asyncio.Task[None] | None = None


class McpConnectionManager:
    def __init__(
        self,
        loader: McpConfigLoader,
        session_factory: Callable[
            [McpServerConfig], AbstractAsyncContextManager[Any]
        ] = _open_session,
    ) -> None:
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

    async def call_tool(self, server: str, tool: str, args: dict[str, object]) -> Any:
        handle = self._handles.get(server)
        if handle is None or handle.session is None:
            raise McpServerUnavailable(
                f"MCP server '{server}' is not connected"
                + (f" ({handle.status.state}: {handle.status.detail})" if handle else ""))
        return await asyncio.wait_for(
            handle.session.call_tool(tool, arguments=args),
            timeout=mcp_call_timeout_sec() or None)
