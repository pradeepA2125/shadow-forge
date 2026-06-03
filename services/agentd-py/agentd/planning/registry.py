"""Read-only tool registry for the PlanningAgent loop."""
from __future__ import annotations

import os
from pathlib import Path

from agentd.retrieval.graph_walker import GraphWalker
from agentd.tools.registry import ToolDefinition, ToolOutput


# Snapshot path env override mirrors the one used by RetrievalArtifactClient so
# the walker reads the same file the rest of the retrieval stack reads.
_SNAPSHOT_PATH_ENV = "AI_EDITOR_RETRIEVAL_SNAPSHOT_PATH"


class PlanningToolRegistry:
    """Read-only tools for the planning agent.

    All paths resolved relative to real_path (the original, unmodified workspace).
    No run_command — planning is strictly read-only.
    """

    def __init__(
        self,
        real_path: Path,
        semantic_index: object | None = None,
    ) -> None:
        self._real_path = real_path
        self._semantic_index = semantic_index
        self._ripgrep_cmd = os.environ.get("AI_EDITOR_RIPGREP_CMD", "rg")
        self._graph_walker: GraphWalker | None = None

    def definitions(self) -> list[ToolDefinition]:
        tools = [
            ToolDefinition(
                name="search_code",
                description=(
                    "Search for a regex/literal pattern across files in the workspace. "
                    "Use to find where functions, classes, or patterns are defined."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Regex or literal pattern"},
                        "path_filter": {"type": "string", "description": "Glob to restrict search (e.g. '*.py', '*.ts', '*.rs')"},
                        "context_lines": {"type": "integer", "description": "Lines of context around each match (default 10)"},
                        "fixed_strings": {"type": "boolean", "description": "Treat as literal string (default false)"},
                    },
                    "required": ["pattern"],
                },
            ),
            ToolDefinition(
                name="read_file",
                description=(
                    "Read a section of a file. Always use start_line and end_line based on "
                    "line numbers from a prior search_code result. Do NOT read whole files — "
                    "omitting start_line/end_line on a large file wastes your tool budget."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative file path"},
                        "start_line": {"type": "integer", "description": "First line (1-indexed) — required for large files"},
                        "end_line": {"type": "integer", "description": "Last line (1-indexed) — required for large files"},
                    },
                    "required": ["path"],
                },
            ),
            ToolDefinition(
                name="list_directory",
                description="List files and subdirectories at a path. Use to navigate project structure.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative directory path (default: '.')"},
                        "depth": {"type": "integer", "description": "Max recursion depth (default 2)"},
                    },
                    "required": [],
                },
            ),
        ]
        if self._semantic_index is not None:
            tools.append(
                ToolDefinition(
                    name="search_semantic",
                    description=(
                        "Vector similarity search: find code related to a natural-language query."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Natural-language description"},
                            "top_k": {"type": "integer", "description": "Results to return (default 8)"},
                        },
                        "required": ["query"],
                    },
                )
            )
        if self._snapshot_path() is not None:
            tools.append(
                ToolDefinition(
                    name="query_graph",
                    description=(
                        "Walk the symbol graph from a file or symbol. Returns workspace "
                        "files/symbols that the seed Calls / Imports / References / Implements / "
                        "Inherits, plus the same in reverse (who Calls into it, who Implements it, "
                        "etc.). Use this AFTER reading a file to follow its call edges: ask "
                        "query_graph(node=\"path:Symbol\") to discover where that symbol is "
                        "defined elsewhere, what overrides it (Protocol implementations land in "
                        "Implements edges), or who else uses it. Two hops via depth=2 reaches "
                        "Protocol implementations through the declaration: "
                        "engine.run_task -> TaskStore.save (Calls, depth 1) -> "
                        "SQLiteTaskStore.save / InMemoryTaskStore.save (Implements, depth 2)."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "node": {
                                "type": "string",
                                "description": (
                                    "Either a workspace-relative file path (\"src/foo.py\") to "
                                    "anchor every node in that file, or path:Symbol to anchor a "
                                    "single symbol (\"src/foo.py:bar\")."
                                ),
                            },
                            "depth": {
                                "type": "integer",
                                "description": "1 = direct neighbours (default), max 3.",
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Max neighbours to return (default 20, max 60).",
                            },
                            "edge_kinds": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Filter: any subset of Calls, Imports, References, Inherits, "
                                    "Implements. Omit to include all."
                                ),
                            },
                        },
                        "required": ["node"],
                    },
                )
            )
        return tools

    async def execute(self, name: str, args: dict[str, object]) -> ToolOutput:
        if name == "search_code":
            from agentd.tools.search import search_code
            return await search_code(
                pattern=str(args.get("pattern", "")),
                path_filter=str(args["path_filter"]) if "path_filter" in args else None,
                context_lines=int(args.get("context_lines", 10)),  # type: ignore[call-overload]
                fixed_strings=bool(args.get("fixed_strings", False)),
                shadow_root=self._real_path,
                ripgrep_cmd=self._ripgrep_cmd,
            )

        if name == "read_file":
            from agentd.tools.files import read_file
            start = args.get("start_line")
            end = args.get("end_line")
            result = await read_file(
                path=str(args.get("path", "")),
                start_line=int(start) if start is not None else None,  # type: ignore[call-overload]
                end_line=int(end) if end is not None else None,  # type: ignore[call-overload]
                shadow_root=self._real_path,
            )
            # Hard enforcement: cap whole-file reads at 150 lines.
            # The model must use start_line/end_line from search_code results.
            if start is None and end is None and not result.is_error:
                lines = result.output.splitlines()
                if len(lines) > 150:
                    truncated = "\n".join(lines[:150])
                    total = len(lines)
                    return ToolOutput(
                        output=(
                            truncated
                            + f"\n\n[TRUNCATED: file has {total} lines, showing first 150. "
                            "Use search_code or search_semantic to find the relevant section, "
                            "then call read_file with start_line/end_line from those results. "
                            "search_code shows line numbers as '155: def build_router'; "
                            "search_semantic shows 'path:line_start-line_end'.]"
                        ),
                        is_error=False,
                    )
            return result

        if name == "list_directory":
            from agentd.tools.files import list_directory
            return await list_directory(
                path=str(args.get("path", ".")),
                root=self._real_path,
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

        if name == "query_graph":
            return self._execute_query_graph(args)

        return ToolOutput(output=f"Error: unknown tool '{name}'", is_error=True)

    def _execute_query_graph(self, args: dict[str, object]) -> ToolOutput:
        walker = self._ensure_walker()
        if walker is None:
            return ToolOutput(
                output="Error: symbol-graph snapshot not available (run the indexer first).",
                is_error=True,
            )
        node = str(args.get("node", "")).strip()
        if not node:
            return ToolOutput(output="Error: 'node' is required", is_error=True)
        depth = int(args.get("depth", 1) or 1)  # type: ignore[call-overload]
        limit = int(args.get("limit", 20) or 20)  # type: ignore[call-overload]
        edge_kinds_raw = args.get("edge_kinds")
        edge_kinds: list[str] | None
        if isinstance(edge_kinds_raw, list):
            edge_kinds = [str(k) for k in edge_kinds_raw]
        else:
            edge_kinds = None
        try:
            result = walker.query(node, depth=depth, limit=limit, edge_kinds=edge_kinds)
        except FileNotFoundError:
            return ToolOutput(
                output="Error: symbol-graph snapshot not available (indexer hasn't run).",
                is_error=True,
            )
        return ToolOutput(output=_render_query_result(node, result))

    def _ensure_walker(self) -> GraphWalker | None:
        snapshot = self._snapshot_path()
        if snapshot is None:
            return None
        if self._graph_walker is None:
            self._graph_walker = GraphWalker(snapshot, self._real_path)
        return self._graph_walker

    def _snapshot_path(self) -> Path | None:
        override = os.environ.get(_SNAPSHOT_PATH_ENV)
        candidate = Path(override) if override else self._real_path / ".ai-editor" / "index-snapshot.json"
        return candidate if candidate.exists() else None


def _render_query_result(query_node: str, result: object) -> str:
    """Format a `QueryResult` for the LLM. Compact, deterministic; one
    neighbour per line so the model can scan with read_file targets."""
    # Imported here to avoid a top-level import cycle if registry is loaded
    # before retrieval — graph_walker is heavy.
    from agentd.retrieval.graph_walker import QueryResult

    if not isinstance(result, QueryResult):
        return f"Error: unexpected result type {type(result).__name__}"

    if not result.matched_roots:
        return (
            f"No node found for {query_node!r}. Use search_code to confirm "
            "the path/symbol exists in the workspace, then retry."
        )

    lines: list[str] = []
    lines.append(f"matched_roots ({len(result.matched_roots)}):")
    for root in result.matched_roots:
        lines.append(f"  {root.file}:{root.line}  {root.kind} {root.symbol}")
    lines.append("")
    if result.neighbors:
        lines.append(f"neighbors ({len(result.neighbors)}):")
        for n in result.neighbors:
            arrow = "->" if n.direction == "out" else "<-"
            lines.append(
                f"  d={n.distance}  {n.edge_kind:<10s} {arrow}  "
                f"{n.node.file}:{n.node.line}  {n.node.kind} {n.node.symbol}"
            )
    else:
        lines.append("neighbors: (none — try a wider edge_kinds filter or a different node)")
    if result.truncated:
        lines.append("")
        lines.append(
            "[TRUNCATED: hit limit before completing the walk. "
            "Re-query with a higher limit or a narrower edge_kinds filter.]"
        )
    return "\n".join(lines)

    async def _list_directory(self, path: str, depth: int) -> ToolOutput:
        resolved = (self._real_path / path).resolve()
        if not str(resolved).startswith(str(self._real_path)):
            return ToolOutput(output="Error: path traversal rejected", is_error=True)
        if not resolved.is_dir():
            return ToolOutput(output=f"Error: '{path}' is not a directory", is_error=True)

        lines: list[str] = []
        self._walk_dir(resolved, self._real_path, depth, 0, lines)
        return ToolOutput(output="\n".join(lines[:500]))

    def _walk_dir(
        self,
        current: Path,
        root: Path,
        max_depth: int,
        current_depth: int,
        out: list[str],
    ) -> None:
        try:
            entries = sorted(current.iterdir(), key=lambda p: (p.is_file(), p.name))
        except PermissionError:
            return
        for entry in entries:
            if entry.name.startswith(".") or entry.name in ("__pycache__", "node_modules", ".git"):
                continue
            rel = entry.relative_to(root)
            suffix = "/" if entry.is_dir() else ""
            prefix = "  " * current_depth
            out.append(f"{prefix}{rel}{suffix}")
            if entry.is_dir() and current_depth < max_depth - 1:
                self._walk_dir(entry, root, max_depth, current_depth + 1, out)
