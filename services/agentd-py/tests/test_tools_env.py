"""Tests for find_binary, setup_env, and list_directory tools."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agentd.tools.env import find_binary, setup_env
from agentd.tools.files import list_directory
from agentd.tools.registry import ToolOutput


@pytest.mark.asyncio
async def test_find_binary_finds_python(tmp_path: Path) -> None:
    result = await find_binary(name="python3", real_workspace=tmp_path)
    assert not result.is_error
    assert "python3" in result.output


@pytest.mark.asyncio
async def test_find_binary_not_found(tmp_path: Path) -> None:
    result = await find_binary(name="__nonexistent_binary_xyz__", real_workspace=tmp_path)
    assert not result.is_error
    assert "not found" in result.output.lower()


@pytest.mark.asyncio
async def test_find_binary_finds_in_venv(tmp_path: Path) -> None:
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    fake_pytest = venv_bin / "pytest"
    fake_pytest.write_text("#!/bin/sh\necho ok")
    fake_pytest.chmod(0o755)

    result = await find_binary(name="pytest", real_workspace=tmp_path)
    assert not result.is_error
    assert str(fake_pytest) in result.output


@pytest.mark.asyncio
async def test_setup_env_rejects_unknown_binary(tmp_path: Path) -> None:
    result = await setup_env(
        command="rm -rf /",
        shadow_root=tmp_path,
        real_workspace=tmp_path,
    )
    assert result.is_error
    assert "not allowed" in result.output.lower()


@pytest.mark.asyncio
async def test_setup_env_rejects_empty_command(tmp_path: Path) -> None:
    result = await setup_env(
        command="",
        shadow_root=tmp_path,
        real_workspace=tmp_path,
    )
    assert result.is_error


@pytest.mark.asyncio
async def test_setup_env_uv_uses_shadow_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """setup_env runs with cwd=shadow_root and UV_PROJECT_ENVIRONMENT set."""
    calls: list[dict] = []

    async def fake_exec(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        calls.append({"args": args, "env": kwargs.get("env", {}), "cwd": kwargs.get("cwd")})
        raise FileNotFoundError("uv not installed — test only checks args")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    shadow = tmp_path / "shadow"
    shadow.mkdir()
    real = tmp_path / "real"
    real.mkdir()

    result = await setup_env(command="uv sync", shadow_root=shadow, real_workspace=real)
    assert calls, "create_subprocess_exec must have been called"
    call = calls[0]
    assert call["cwd"] == str(shadow)
    assert call["env"].get("UV_PROJECT_ENVIRONMENT") == str(real / ".venv")


@pytest.mark.asyncio
async def test_list_directory_shows_files(tmp_path: Path) -> None:
    (tmp_path / "foo.py").write_text("x = 1")
    (tmp_path / "bar.txt").write_text("hello")
    (tmp_path / "subdir").mkdir()

    result = await list_directory(path=".", root=tmp_path)
    assert not result.is_error
    assert "foo.py" in result.output
    assert "bar.txt" in result.output
    assert "subdir" in result.output


@pytest.mark.asyncio
async def test_list_directory_rejects_traversal(tmp_path: Path) -> None:
    result = await list_directory(path="../../etc", root=tmp_path)
    assert result.is_error
    assert "traversal" in result.output.lower()


@pytest.mark.asyncio
async def test_list_directory_missing_path(tmp_path: Path) -> None:
    result = await list_directory(path="nonexistent_dir", root=tmp_path)
    assert result.is_error
