"""Base types and shared subprocess utility for post-patch checkers."""
from __future__ import annotations

import asyncio
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    output: str          # non-empty only on failure
    skipped: bool = False  # tool not installed / not applicable
    blocking: bool = True  # False = advisory only (e.g. ruff style); True = must fix before verify_done


@runtime_checkable
class CheckRunner(Protocol):
    """Behavioral interface — one checker tool (ruff, mypy, tsc, …)."""

    @property
    def name(self) -> str: ...

    async def run(self, files: list[Path], cwd: Path) -> CheckResult: ...


async def run_subprocess(
    cmd: list[str],
    cwd: Path,
    timeout: int = 30,
) -> tuple[int, str]:
    """Run a command in a thread so we don't block the event loop.

    Returns (returncode, combined stdout+stderr).
    returncode == -1 means the tool was not found or timed out.
    """
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
        )
        return result.returncode, (result.stdout + result.stderr).strip()
    except FileNotFoundError:
        return -1, ""          # tool not installed — caller treats as skipped
    except subprocess.TimeoutExpired:
        return -1, "timed out"


def python_executable() -> str:
    """Return the current interpreter path (safe for py_compile invocations)."""
    return sys.executable
