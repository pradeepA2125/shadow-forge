"""Tests for ToolRegistry phase-gated definitions and basename allowlist."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agentd.tools.registry import ToolRegistry


def test_explore_phase_omits_env_tools(tmp_path: Path) -> None:
    registry = ToolRegistry(shadow_root=tmp_path, real_workspace_path=tmp_path)
    names = {t.name for t in registry.definitions(phase="explore")}
    assert "search_code" in names
    assert "read_file" in names
    assert "list_directory" in names
    assert "setup_env" not in names
    assert "find_binary" not in names


def test_verify_phase_includes_env_tools(tmp_path: Path) -> None:
    registry = ToolRegistry(shadow_root=tmp_path, real_workspace_path=tmp_path)
    names = {t.name for t in registry.definitions(phase="verify")}
    assert "setup_env" in names
    assert "find_binary" in names


def test_run_command_allows_full_path(tmp_path: Path) -> None:
    """Basename of a full path must pass the allowlist check."""
    registry = ToolRegistry(shadow_root=tmp_path, real_workspace_path=tmp_path)
    fake = tmp_path / "pytest"
    fake.write_text("#!/bin/sh\necho ok")
    fake.chmod(0o755)

    result = asyncio.get_event_loop().run_until_complete(
        registry.execute("run_command", {"command": str(fake), "args": ["--version"]})
    )
    assert "not in the shell allowlist" not in result.output


def test_run_command_blocks_unlisted_binary(tmp_path: Path) -> None:
    registry = ToolRegistry(shadow_root=tmp_path, real_workspace_path=tmp_path)
    result = asyncio.get_event_loop().run_until_complete(
        registry.execute("run_command", {"command": "rm", "args": ["-rf", "/"]})
    )
    assert result.is_error
    assert "allowlist" in result.output.lower()
