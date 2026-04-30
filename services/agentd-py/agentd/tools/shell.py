"""run_command tool implementation (allow-listed, sandbox-scoped)."""
from __future__ import annotations

import asyncio
from asyncio.subprocess import PIPE, STDOUT
from pathlib import Path

from agentd.tools.registry import ToolOutput

_MAX_OUTPUT_CHARS = 8000
_DEFAULT_TIMEOUT_SEC = 60


async def run_command(
    *,
    command: str,
    args: list[str],
    shadow_root: Path,
    allowlist: set[str],
    timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
    binary_name_override: str | None = None,
) -> ToolOutput:
    if not command:
        return ToolOutput(output="Error: command is required", is_error=True)

    check_name = binary_name_override or command
    if check_name not in allowlist:
        return ToolOutput(
            output=(
                f"Error: '{check_name}' is not in the shell allowlist. "
                f"Allowed: {', '.join(sorted(allowlist))}"
            ),
            is_error=True,
        )

    try:
        proc = await asyncio.create_subprocess_exec(
            command,
            *args,
            cwd=str(shadow_root),
            stdout=PIPE,
            stderr=STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        return ToolOutput(
            output=f"Error: command '{command}' timed out after {timeout_sec}s",
            is_error=True,
        )
    except FileNotFoundError:
        return ToolOutput(
            output=f"Error: '{command}' not found on PATH",
            is_error=True,
        )
    except Exception as exc:
        return ToolOutput(output=f"Error running '{command}': {exc}", is_error=True)

    output = stdout.decode("utf-8", errors="replace")
    exit_code = proc.returncode or 0
    header = f"$ {command} {' '.join(args)}\n(exit code: {exit_code})\n"
    full = header + output

    if len(full) > _MAX_OUTPUT_CHARS:
        # Keep the tail (more useful for error messages)
        keep = _MAX_OUTPUT_CHARS - len(header) - 100
        full = header + f"... (output truncated, showing last {keep} chars)\n" + output[-keep:]

    is_error = exit_code != 0
    return ToolOutput(output=full, is_error=is_error)
