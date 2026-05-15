"""read_file tool implementation (path-traversal safe)."""
from __future__ import annotations

from pathlib import Path

from agentd.tools.registry import ToolOutput

_MAX_LINES = 2000
_MAX_OUTPUT_CHARS = 100_000


async def read_file(
    *,
    path: str,
    start_line: int | None,
    end_line: int | None,
    shadow_root: Path,
) -> ToolOutput:
    if not path:
        return ToolOutput(output="Error: path is required", is_error=True)

    resolved = (shadow_root / path).resolve()
    # Path traversal guard
    try:
        resolved.relative_to(shadow_root.resolve())
    except ValueError:
        return ToolOutput(
            output=f"Error: path traversal rejected — '{path}' is outside the workspace",
            is_error=True,
        )

    if not resolved.exists():
        return ToolOutput(output=f"Error: file not found: {path}", is_error=True)
    if not resolved.is_file():
        return ToolOutput(output=f"Error: '{path}' is a directory, not a file", is_error=True)

    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ToolOutput(output=f"Error reading '{path}': {exc}", is_error=True)

    all_lines = text.splitlines()
    total = len(all_lines)

    # Apply line range
    s = (start_line - 1) if start_line is not None else 0
    e = end_line if end_line is not None else total
    s = max(0, s)
    e = min(total, e)
    selected = all_lines[s:e]

    if len(selected) > _MAX_LINES:
        selected = selected[:_MAX_LINES]
        truncated = True
    else:
        truncated = False

    numbered = [f"{s + i + 1:4d}: {line}" for i, line in enumerate(selected)]
    result = "\n".join(numbered)

    if truncated:
        result += f"\n... (file has {total} lines; showing {s+1}–{s+_MAX_LINES})"

    if len(result) > _MAX_OUTPUT_CHARS:
        result = result[:_MAX_OUTPUT_CHARS] + "\n... (truncated)"

    return ToolOutput(output=result)


async def list_directory(*, path: str, root: Path) -> ToolOutput:
    """List files and directories at path within root.

    Returns one entry per line: 'file  <name>' or 'dir   <name>'.
    Capped at 200 entries. Path traversal rejected.
    """
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
