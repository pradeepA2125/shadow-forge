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

ApprovalCallback = Callable[[str, str, dict[str, object]], Awaitable[bool]]


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
