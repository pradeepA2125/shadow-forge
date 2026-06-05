"""search_code (ripgrep) and search_semantic tool implementations."""
from __future__ import annotations

import asyncio
import json
import shutil
from asyncio.subprocess import PIPE
from pathlib import Path

from agentd.tools.registry import ToolOutput

_MAX_MATCHES = 50
_MAX_OUTPUT_CHARS = 8000


async def search_code(
    *,
    pattern: str,
    path_filter: str | None,
    context_lines: int,
    fixed_strings: bool,
    shadow_root: Path,
    ripgrep_cmd: str = "rg",
) -> ToolOutput:
    if not pattern:
        return ToolOutput(
            output=(
                "Error: the 'pattern' argument is required (the regex/text to search for). "
                'Example: search_code(pattern="def build_router"). '
                "Optional: path_filter (a glob like '*.py'), context_lines, fixed_strings."
            ),
            is_error=True,
        )

    rg = shutil.which(ripgrep_cmd) or ripgrep_cmd
    cmd = [rg, "--json", "-C", str(max(0, context_lines))]
    if fixed_strings:
        cmd.append("--fixed-strings")
    if path_filter:
        # Ripgrep glob matching fails for exact relative paths (e.g. "a/b/c.py")
        # when an explicit absolute search root is passed — they must be anchored.
        # Prepend "**/" when the filter looks like an exact path (has "/" but no glob chars).
        glob = path_filter
        if "/" in glob and not any(c in glob for c in ("*", "?", "[", "{")):
            glob = "**/" + glob
        cmd += ["-g", glob]
    cmd += [pattern, str(shadow_root)]

    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    except asyncio.TimeoutError:
        return ToolOutput(output="Error: search timed out after 15s", is_error=True)
    except FileNotFoundError:
        return ToolOutput(
            output=f"Error: ripgrep not found at '{ripgrep_cmd}'. Install ripgrep or set AI_EDITOR_RIPGREP_CMD.",
            is_error=True,
        )
    except Exception as exc:
        return ToolOutput(output=f"Error: search failed: {exc}", is_error=True)

    # Parse ripgrep --json output into readable text
    lines = stdout.decode("utf-8", errors="replace").splitlines()
    matches: list[str] = []
    current_file: str | None = None
    match_count = 0

    for line in lines:
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_type = obj.get("type")
        data = obj.get("data", {})

        if msg_type == "begin":
            path_obj = data.get("path", {})
            current_file = path_obj.get("text") if isinstance(path_obj, dict) else str(path_obj)
            if current_file:
                # Strip shadow root prefix to show relative paths
                try:
                    current_file = str(Path(current_file).relative_to(shadow_root))
                except ValueError:
                    pass
            matches.append(f"\n--- {current_file} ---")

        elif msg_type == "match":
            match_count += 1
            if match_count > _MAX_MATCHES:
                matches.append(f"... (truncated after {_MAX_MATCHES} matches)")
                break
            lines_obj = data.get("lines", {})
            text = lines_obj.get("text", "") if isinstance(lines_obj, dict) else ""
            line_num = data.get("line_number", "?")
            matches.append(f"  {line_num}: {text.rstrip()}")

        elif msg_type == "context":
            lines_obj = data.get("lines", {})
            text = lines_obj.get("text", "") if isinstance(lines_obj, dict) else ""
            line_num = data.get("line_number", "?")
            matches.append(f"  {line_num}| {text.rstrip()}")

    if not matches or match_count == 0:
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        if stderr_text:
            return ToolOutput(output=f"No matches found. (stderr: {stderr_text[:500]})")
        return ToolOutput(output="No matches found.")

    result = "\n".join(matches)
    if len(result) > _MAX_OUTPUT_CHARS:
        # Find the last visible line number so the model knows where to read_file from.
        last_line_num: str | None = None
        for chunk in reversed(matches):
            stripped = chunk.strip()
            if stripped and stripped[0].isdigit() and ("|" in stripped or ":" in stripped):
                last_line_num = stripped.split("|")[0].split(":")[0].strip()
                break
        hint = (
            f" The result is cut off. Do NOT increase context_lines — it will not help. "
            f"Instead call read_file with start_line/end_line around the line numbers shown above"
            + (f" (last visible line: {last_line_num})" if last_line_num else "")
            + " to read the section you need."
        )
        result = result[:_MAX_OUTPUT_CHARS] + f"\n...{hint}"
    return ToolOutput(output=result)


async def search_semantic(
    *,
    query: str,
    top_k: int,
    semantic_index: object,
) -> ToolOutput:
    if not query:
        return ToolOutput(output="Error: query is required", is_error=True)
    try:
        chunks = semantic_index.query(query, top_k=max(1, min(top_k, 20)))  # type: ignore[attr-defined]
    except Exception as exc:
        return ToolOutput(output=f"Error: semantic search failed: {exc}", is_error=True)

    if not chunks:
        return ToolOutput(output="No semantically similar code found.")

    lines: list[str] = []
    for sc in chunks:
        chunk = sc.chunk
        score = sc.score
        lines.append(f"\n[score={score:.3f}] {chunk.path}:{chunk.line_start}-{chunk.line_end}")
        if chunk.name:
            lines.append(f"  {chunk.kind}: {chunk.name}")
        lines.append(chunk.text_with_lines[:600])

    result = "\n".join(lines)
    if len(result) > _MAX_OUTPUT_CHARS:
        result = result[:_MAX_OUTPUT_CHARS] + "\n... (truncated)"
    return ToolOutput(output=result)
