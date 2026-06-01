"""Tests for EcosystemProbe — pure filesystem scan, no LLM."""
from pathlib import Path

import pytest

from agentd.env.probe import EcosystemProbe


@pytest.mark.asyncio
async def test_probe_finds_python_manifest_at_root(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = \"demo\"\nversion = \"0\"\ndependencies = [\"fastapi\"]\n"
    )
    result = await EcosystemProbe.scan(tmp_path)
    pythons = [e for e in result.ecosystems if e.ecosystem == "python"]
    assert len(pythons) == 1
    assert pythons[0].subdir == ""
    assert pythons[0].manifest_path == "pyproject.toml"
    assert "fastapi" in pythons[0].manifest_text


@pytest.mark.asyncio
async def test_probe_finds_python_in_monorepo_subdir(tmp_path: Path):
    sub = tmp_path / "services" / "agentd-py"
    sub.mkdir(parents=True)
    (sub / "pyproject.toml").write_text("[project]\nname = \"sub\"\nversion = \"0\"\n")
    result = await EcosystemProbe.scan(tmp_path)
    pythons = [e for e in result.ecosystems if e.ecosystem == "python"]
    assert pythons[0].subdir == "services/agentd-py"


@pytest.mark.asyncio
async def test_probe_finds_node_and_rust_in_same_workspace(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"name": "demo", "version": "1.0.0"}')
    (tmp_path / "Cargo.toml").write_text(
        "[package]\nname = \"demo\"\nversion = \"0.1.0\"\nedition = \"2021\"\n"
    )
    result = await EcosystemProbe.scan(tmp_path)
    ecos = sorted(e.ecosystem for e in result.ecosystems)
    assert ecos == ["node", "rust"]


@pytest.mark.asyncio
async def test_probe_bare_workspace_returns_empty(tmp_path: Path):
    result = await EcosystemProbe.scan(tmp_path)
    assert result.ecosystems == []
    # workspace_tree may be empty for a truly empty dir; that's expected.


@pytest.mark.asyncio
async def test_probe_records_lockfile_presence(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"name": "demo", "version": "1.0.0"}')
    (tmp_path / "package-lock.json").write_text('{}')
    result = await EcosystemProbe.scan(tmp_path)
    node = result.ecosystems[0]
    assert "package-lock.json" in node.lockfiles_present
    assert "yarn.lock" not in node.lockfiles_present


@pytest.mark.asyncio
async def test_probe_flags_setuptools_flat_layout_risk(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = \"demo\"\nversion = \"0\"\n"
        "[build-system]\nrequires = [\"setuptools>=68\"]\nbuild-backend = \"setuptools.build_meta\"\n"
    )
    (tmp_path / "agentd").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "workspaces").mkdir()
    result = await EcosystemProbe.scan(tmp_path)
    assert any("SETUPTOOLS_FLAT_LAYOUT_RISK" in d for d in result.diagnostics)


@pytest.mark.asyncio
async def test_probe_flags_venv_absent_for_python_without_venv(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname=\"x\"\nversion=\"0\"\n")
    result = await EcosystemProbe.scan(tmp_path)
    assert any(d.startswith("VENV_ABSENT:") for d in result.diagnostics)


@pytest.mark.asyncio
async def test_probe_no_venv_absent_when_venv_python_exists(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname=\"x\"\nversion=\"0\"\n")
    bin_dir = tmp_path / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").write_text("#!/bin/sh\n")
    (bin_dir / "python").chmod(0o755)
    result = await EcosystemProbe.scan(tmp_path)
    assert not any(d.startswith("VENV_ABSENT:") for d in result.diagnostics)


@pytest.mark.asyncio
async def test_probe_flags_node_modules_absent(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"name": "x", "version": "1.0.0"}')
    result = await EcosystemProbe.scan(tmp_path)
    assert any(d.startswith("NODE_MODULES_ABSENT:") for d in result.diagnostics)


@pytest.mark.asyncio
async def test_probe_no_node_modules_absent_when_directory_exists(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"name": "x", "version": "1.0.0"}')
    (tmp_path / "node_modules").mkdir()
    result = await EcosystemProbe.scan(tmp_path)
    assert not any(d.startswith("NODE_MODULES_ABSENT:") for d in result.diagnostics)


@pytest.mark.asyncio
async def test_probe_flags_lockfile_missing_for_python_without_lock(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname=\"x\"\nversion=\"0\"\n")
    result = await EcosystemProbe.scan(tmp_path)
    assert any(d.startswith("LOCKFILE_MISSING:python:") for d in result.diagnostics)


@pytest.mark.asyncio
async def test_probe_no_lockfile_missing_when_uv_lock_present(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname=\"x\"\nversion=\"0\"\n")
    (tmp_path / "uv.lock").write_text("# locked\n")
    result = await EcosystemProbe.scan(tmp_path)
    assert not any(d.startswith("LOCKFILE_MISSING:python:") for d in result.diagnostics)


@pytest.mark.asyncio
async def test_probe_skips_node_modules_and_venv_dirs(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"name": "outer"}')
    (tmp_path / "node_modules" / "inner").mkdir(parents=True)
    (tmp_path / "node_modules" / "inner" / "package.json").write_text('{"name": "skip-me"}')
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "pyproject.toml").write_text("[project]\nname=\"x\"\nversion=\"0\"\n")
    result = await EcosystemProbe.scan(tmp_path)
    nodes = [e for e in result.ecosystems if e.ecosystem == "node"]
    pythons = [e for e in result.ecosystems if e.ecosystem == "python"]
    assert len(nodes) == 1  # outer only
    assert pythons == []    # .venv excluded
