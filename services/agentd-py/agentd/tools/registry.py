"""Tool registry: discovers available tools and dispatches execute() calls."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel


class ToolDefinition(BaseModel):
    name: str
    description: str
    parameters: dict[str, object]  # JSON Schema for the args object


@dataclass
class ToolOutput:
    output: str
    is_error: bool = False


class ToolRegistry:
    """Dispatches tool calls to the appropriate implementation.

    read_file, list_directory, search_code: real_workspace_path (stable source of truth).
    run_command: CWD=shadow_root (patched files), binary resolution=real_workspace_path
                 (binaries installed by setup_env live in real .venv/node_modules).
    setup_env, find_binary: real_workspace_path.
    """

    def __init__(
        self,
        shadow_root: Path,
        real_workspace_path: Path,
        semantic_index: object | None = None,
        command_approval_callback: object | None = None,
    ) -> None:
        self._shadow_root = shadow_root
        self._real_workspace_path = real_workspace_path
        self._semantic_index = semantic_index
        self._read_from_shadow: bool = False
        self._ripgrep_cmd = os.environ.get("AI_EDITOR_RIPGREP_CMD", "rg")
        # async (command, args, cwd) -> CommandDecision. When set, run_command
        # consults it before executing. When None, run_command runs unguarded
        # (legacy/test path) — production wires this via the engine.
        self._command_approval_callback = command_approval_callback

    def use_shadow_for_reads(self) -> None:
        """Switch read_file, search_code, list_directory to read from the shadow workspace."""
        self._read_from_shadow = True

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
                    "Read up to 500 lines of a file. Use start_line+end_line to target "
                    "the section you need. When you know a line number from search_code, "
                    "read a precise range around it. When uncertain of a section's length, "
                    "read a wider block (e.g. 200–300 lines) rather than guessing a tight range."
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
                name="read_env_profile",
                description=(
                    "Return the workspace's env profile (JSON). Tells you the "
                    "package manager, install command, interpreter path, and "
                    "test command per ecosystem. Always call this before "
                    "guessing python/node/cargo commands. The "
                    "'interpreter_or_runner' field is the binary to call "
                    "directly — don't try to activate a venv (it won't persist "
                    "across tool calls)."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
            ),
            ToolDefinition(
                name="run_command",
                description=(
                    "Run a shell command inside the shadow workspace. Each command "
                    "is surfaced to the user for approval (Accept / Accept & remember / "
                    "Reject) unless the session was started in allow_all mode. If the "
                    "user rejects, you will receive a tool-result error and should try "
                    "a different approach (e.g. a static check). Use to run tests, "
                    "linters, or type checkers."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Command name or full path"},
                        "args": {"type": "array", "items": {"type": "string"}, "description": "Command arguments"},
                        "cwd": {"type": "string", "description": "Optional working directory, RELATIVE to the workspace root (e.g. 'services/agentd-py'). Empty/omitted = workspace root. Paths that escape the workspace are clamped to root."},
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
                        "'cargo build', 'go mod download', 'poetry install'. "
                        "Python ecosystem: if 'uv' is missing, transparently falls back to "
                        "system python3 + pip; you don't need a separate code path. "
                        "Node/Rust/Go: returns 'AGENT SHOULD: revision_needed' when the "
                        "toolchain is genuinely missing — emit revision_needed in that case."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "command": {"type": "string", "description": "Full command string, e.g. 'uv sync' or 'npm ci'"},
                            "cwd": {"type": "string", "description": "Optional working directory, RELATIVE to the workspace root (e.g. 'services/agentd-py' for a monorepo where pyproject.toml is in a subdir). Empty/omitted = workspace root."},
                        },
                        "required": ["command"],
                    },
                ),
                ToolDefinition(
                    name="init_workspace",
                    description=(
                        "Bootstrap a bare workspace by emitting a minimal manifest "
                        "(pyproject.toml / package.json / Cargo.toml / go.mod) into shadow. "
                        "Use this INSTEAD of hand-writing manifests via emit_patch — "
                        "it guarantees the smallest valid manifest with only the dev_deps "
                        "you declare; no extra packages. Refuses if the manifest already exists."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "ecosystem": {
                                "type": "string",
                                "enum": ["python", "node", "rust", "go"],
                                "description": "Target ecosystem",
                            },
                            "dev_deps": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Exact dev dependencies to declare (e.g. ['pytest'] or ['vitest']). No defaults beyond pytest for python.",
                            },
                        },
                        "required": ["ecosystem", "dev_deps"],
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
        if self._has_graph_snapshot():
            tools.append(
                ToolDefinition(
                    name="query_graph",
                    description=(
                        "Walk the symbol graph from a file or symbol. Returns workspace "
                        "files/symbols connected via Calls / Imports / References / Implements / "
                        "Inherits — both outbound and inbound. After read_file shows you a "
                        "function, use query_graph(node=\"path:Symbol\") to find what it calls "
                        "(outbound Calls), who calls it (inbound Calls), and — for Protocol/ABC "
                        "method calls — the concrete implementations via Implements edges. "
                        "depth=2 chains two hops: Calls -> declaration, Implements <- "
                        "concrete classes."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "node": {
                                "type": "string",
                                "description": (
                                    "Workspace-relative \"path/to/file.ext\" to anchor every "
                                    "symbol in that file, or \"path/to/file.ext:Symbol\" to anchor "
                                    "a specific symbol."
                                ),
                            },
                            "depth": {
                                "type": "integer",
                                "description": "BFS depth (default 1, max 3).",
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Max neighbours (default 20, max 60).",
                            },
                            "edge_kinds": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Filter: subset of Calls, Imports, References, Inherits, "
                                    "Implements. Omit for all."
                                ),
                            },
                        },
                        "required": ["node"],
                    },
                )
            )
        return tools

    def _has_graph_snapshot(self) -> bool:
        override = os.environ.get("AI_EDITOR_RETRIEVAL_SNAPSHOT_PATH")
        candidate = (
            Path(override) if override
            else self._real_workspace_path / ".ai-editor" / "index-snapshot.json"
        )
        return candidate.exists()

    async def execute(self, name: str, args: dict[str, object]) -> ToolOutput:
        """Dispatch a tool call. Returns ToolOutput with output text and error flag."""
        from agentd.tools.arg_aliases import normalize_tool_args
        args = normalize_tool_args(name, args)
        if name == "search_code":
            from agentd.tools.search import search_code
            return await search_code(
                pattern=str(args.get("pattern", "")),
                path_filter=str(args["path_filter"]) if "path_filter" in args else None,
                context_lines=int(args.get("context_lines", 3)),  # type: ignore[call-overload]
                fixed_strings=bool(args.get("fixed_strings", False)),
                shadow_root=(
                    self._shadow_root if self._read_from_shadow
                    else self._real_workspace_path
                ),
                ripgrep_cmd=self._ripgrep_cmd,
            )

        if name == "read_file":
            from agentd.tools.files import read_file
            _MAX_READ_LINES = 500
            start = args.get("start_line")
            end = args.get("end_line")
            result = await read_file(
                path=str(args.get("path", "")),
                start_line=int(start) if start is not None else None,  # type: ignore[call-overload]
                end_line=int(end) if end is not None else None,  # type: ignore[call-overload]
                shadow_root=(
                    self._shadow_root if self._read_from_shadow
                    else self._real_workspace_path
                ),
            )
            if result.is_error:
                return result
            lines = result.output.splitlines()
            if len(lines) > _MAX_READ_LINES:
                truncated = "\n".join(lines[:_MAX_READ_LINES])
                total = len(lines)
                return ToolOutput(
                    output=(
                        truncated
                        + f"\n\n[TRUNCATED: showing {_MAX_READ_LINES} of {total} lines. "
                        "Use search_code to locate the symbol (line number shown as '123: def foo'), "
                        "then call read_file with start_line/end_line around that line.]"
                    ),
                    is_error=False,
                )
            return result

        if name == "list_directory":
            from agentd.tools.files import list_directory
            return await list_directory(
                path=str(args.get("path", ".")),
                root=(
                    self._shadow_root if self._read_from_shadow
                    else self._real_workspace_path
                ),
            )

        if name == "run_command":
            from agentd.tools.shell import run_command
            raw_args = args.get("args", [])
            cmd_args = [str(a) for a in raw_args] if isinstance(raw_args, list) else []
            command = str(args.get("command", ""))
            cwd = str(args.get("cwd", "")) or ""  # relative to shadow_root; "" = shadow_root
            binary_name = Path(command).name  # basename used by binary-rule matching
            if self._command_approval_callback is not None:
                decision = await self._command_approval_callback(command, cmd_args, cwd)
                if not decision.approve:
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
                cwd=cwd or None,
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
                cwd=str(args.get("cwd", "")) or None,
            )

        if name == "init_workspace":
            from agentd.tools.env import init_workspace
            raw_deps = args.get("dev_deps", [])
            dev_deps = [str(d) for d in raw_deps] if isinstance(raw_deps, list) else []
            return await init_workspace(
                ecosystem=str(args.get("ecosystem", "")),
                dev_deps=dev_deps,
                shadow_root=self._shadow_root,
            )

        if name == "read_env_profile":
            from agentd.tools.env_profile import read_env_profile
            return await read_env_profile(real_workspace=self._real_workspace_path)

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
        # Lazy import: graph_walker has its own cost, and the execution-loop
        # registry might never need it on tasks that don't touch query_graph.
        from agentd.planning.registry import _render_query_result  # type: ignore[attr-defined]
        from agentd.retrieval.graph_walker import GraphWalker

        node = str(args.get("node", "")).strip()
        if not node:
            return ToolOutput(output="Error: 'node' is required", is_error=True)

        override = os.environ.get("AI_EDITOR_RETRIEVAL_SNAPSHOT_PATH")
        snapshot = (
            Path(override) if override
            else self._real_workspace_path / ".ai-editor" / "index-snapshot.json"
        )
        if not snapshot.exists():
            return ToolOutput(
                output="Error: symbol-graph snapshot not available (run the indexer first).",
                is_error=True,
            )

        from agentd.retrieval.graph_walker import _coerce_int
        depth = _coerce_int(args.get("depth"), 1)
        limit = _coerce_int(args.get("limit"), 20)
        edge_kinds_raw = args.get("edge_kinds")
        edge_kinds: list[str] | None
        if isinstance(edge_kinds_raw, list):
            edge_kinds = [str(k) for k in edge_kinds_raw]
        else:
            edge_kinds = None

        from agentd.retrieval.graph_walker import GraphWalkerSnapshotError

        walker = GraphWalker(snapshot, self._real_workspace_path)
        try:
            result = walker.query(node, depth=depth, limit=limit, edge_kinds=edge_kinds)
        except FileNotFoundError:
            return ToolOutput(
                output="Error: symbol-graph snapshot not available (indexer hasn't run).",
                is_error=True,
            )
        except GraphWalkerSnapshotError as exc:
            return ToolOutput(
                output=f"Error: symbol-graph snapshot is unreadable ({exc}); use search_code/read_file instead.",
                is_error=True,
            )
        except Exception as exc:  # noqa: BLE001 — last-line defence
            return ToolOutput(
                output=f"Error: query_graph failed: {type(exc).__name__}: {exc}",
                is_error=True,
            )
        return ToolOutput(output=_render_query_result(node, result))
