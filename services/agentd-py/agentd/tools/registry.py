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

    read_file and list_directory operate on real_workspace_path (stable, immutable
    throughout the task). search_code and run_command operate on shadow_root (patched
    code). setup_env and find_binary operate on real_workspace_path.
    """

    def __init__(
        self,
        shadow_root: Path,
        real_workspace_path: Path,
        semantic_index: object | None = None,
    ) -> None:
        self._shadow_root = shadow_root
        self._real_workspace_path = real_workspace_path
        self._semantic_index = semantic_index
        self._ripgrep_cmd = os.environ.get("AI_EDITOR_RIPGREP_CMD", "rg")
        allowlist_raw = os.environ.get(
            "AI_EDITOR_SHELL_ALLOWLIST",
            "pytest,npm,cargo,ruff,mypy,tsc,eslint,jest,vitest",
        )
        self._shell_allowlist = {c.strip() for c in allowlist_raw.split(",") if c.strip()}

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

    async def execute(self, name: str, args: dict[str, object]) -> ToolOutput:
        """Dispatch a tool call. Returns ToolOutput with output text and error flag."""
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
            raw_args = args.get("args", [])
            cmd_args = [str(a) for a in raw_args] if isinstance(raw_args, list) else []
            command = str(args.get("command", ""))
            binary_name = Path(command).name  # basename check allows full paths
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
