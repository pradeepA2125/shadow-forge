"""Tests for EnvProfileBuilder — composes probe + draft_conventions."""
from datetime import datetime
from pathlib import Path

import pytest

from agentd.env.profile_builder import EnvProfileBuilder
from agentd.orchestrator.scripted_engine import ScriptedReasoningEngine


@pytest.mark.asyncio
async def test_build_skips_llm_when_python_lockfile_is_unambiguous(tmp_path: Path):
    """W2: python + uv.lock → conventions synthesised, no LLM call.

    interpreter_or_runner is nulled when the .venv isn't present on disk —
    the VENV_ABSENT diagnostic carries that signal instead.
    """
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname=\"x\"\nversion=\"0\"\ndependencies=[\"fastapi\",\"pydantic\"]\n"
    )
    (tmp_path / "uv.lock").write_text("# locked\n")

    class WouldRaiseReasoner:
        async def draft_conventions(self, *, probe):
            raise AssertionError("LLM must not be called when fast-path applies")

    builder = EnvProfileBuilder(reasoner=WouldRaiseReasoner())
    profile = await builder.build(tmp_path)

    assert profile.bootstrap_needed is False
    assert profile.ecosystems[0].package_manager == "uv"
    assert profile.ecosystems[0].install_command == "uv sync"
    # No .venv on disk → interpreter_or_runner is null
    assert profile.ecosystems[0].interpreter_or_runner is None
    assert profile.ecosystems[0].test_command == "pytest"
    assert "fastapi" in profile.ecosystems[0].declared_dependencies_top
    assert "no LLM call" in (profile.conventions_notes or "")


@pytest.mark.asyncio
async def test_build_sets_interpreter_when_venv_actually_exists(tmp_path: Path):
    """When the .venv/bin/python file exists on disk, interpreter_or_runner
    is populated. This is the design intent: the field promises a usable binary."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname=\"x\"\nversion=\"0\"\n")
    (tmp_path / "uv.lock").write_text("# locked\n")
    bin_dir = tmp_path / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").write_text("#!/bin/sh\n")
    (bin_dir / "python").chmod(0o755)

    builder = EnvProfileBuilder(reasoner=ScriptedReasoningEngine(plan=None, patches=[]))
    profile = await builder.build(tmp_path)

    assert profile.ecosystems[0].interpreter_or_runner == ".venv/bin/python"


@pytest.mark.asyncio
async def test_build_sets_node_runner_when_node_modules_bin_exists(tmp_path: Path):
    (tmp_path / "package.json").write_text(
        '{"name": "x", "version": "1.0.0", "devDependencies": {"vitest": "*"}}'
    )
    (tmp_path / "package-lock.json").write_text('{}')
    (tmp_path / "node_modules" / ".bin").mkdir(parents=True)

    builder = EnvProfileBuilder(reasoner=ScriptedReasoningEngine(plan=None, patches=[]))
    profile = await builder.build(tmp_path)

    assert profile.ecosystems[0].interpreter_or_runner == "node_modules/.bin"


@pytest.mark.asyncio
async def test_build_synthesises_node_with_package_lock(tmp_path: Path):
    (tmp_path / "package.json").write_text(
        '{"name": "x", "version": "1.0.0", "devDependencies": {"vitest": "*"}}'
    )
    (tmp_path / "package-lock.json").write_text('{}')

    class WouldRaiseReasoner:
        async def draft_conventions(self, *, probe):
            raise AssertionError("must not be called")

    builder = EnvProfileBuilder(reasoner=WouldRaiseReasoner())
    profile = await builder.build(tmp_path)

    entry = profile.ecosystems[0]
    assert entry.package_manager == "npm"
    assert entry.install_command == "npm ci"
    assert entry.test_command == "vitest run"


@pytest.mark.asyncio
async def test_build_synthesises_with_subdir_prefix_on_paths(tmp_path: Path):
    """Fast-path interpreter_or_runner must include the subdir (W4 origin),
    but only when the binary actually exists at that subdir-prefixed path."""
    sub = tmp_path / "services" / "agentd-py"
    sub.mkdir(parents=True)
    (sub / "pyproject.toml").write_text("[project]\nname=\"x\"\nversion=\"0\"\n")
    (sub / "uv.lock").write_text("# locked\n")
    bin_dir = sub / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").write_text("#!/bin/sh\n")
    (bin_dir / "python").chmod(0o755)

    builder = EnvProfileBuilder(reasoner=ScriptedReasoningEngine(plan=None, patches=[]))
    profile = await builder.build(tmp_path)

    entry = profile.ecosystems[0]
    assert entry.interpreter_or_runner == "services/agentd-py/.venv/bin/python"


@pytest.mark.asyncio
async def test_build_falls_through_to_llm_when_python_has_no_lockfile(tmp_path: Path):
    """Ambiguous case: pyproject only, no lockfile → LLM call required."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname=\"x\"\nversion=\"0\"\n")
    canned = {
        "ecosystems": [{
            "ecosystem": "python", "subdir": "", "manifest_path": "pyproject.toml",
            "package_manager": "uv", "install_command": "uv sync",
            "interpreter_or_runner": ".venv/bin/python", "test_command": "pytest",
            "declared_dependencies_top": [], "notes": None,
        }],
        "conventions_notes": "ambiguous, LLM chose uv",
    }
    engine = ScriptedReasoningEngine(plan=None, patches=[], draft_conventions_responses=[canned])
    builder = EnvProfileBuilder(reasoner=engine)
    profile = await builder.build(tmp_path)

    assert profile.conventions_notes == "ambiguous, LLM chose uv"


@pytest.mark.asyncio
async def test_build_normalises_interpreter_path_from_llm(tmp_path: Path):
    """W4: LLM returns interpreter_or_runner without subdir prefix → prepend it.
    Then verify the path exists; otherwise null it."""
    sub = tmp_path / "services" / "agentd-py"
    sub.mkdir(parents=True)
    (sub / "pyproject.toml").write_text("[project]\nname=\"x\"\nversion=\"0\"\n")
    # Create the venv so existence check passes
    bin_dir = sub / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").write_text("#!/bin/sh\n")
    (bin_dir / "python").chmod(0o755)
    # No lockfile in subdir → fast-path returns None → LLM call.
    canned = {
        "ecosystems": [{
            "ecosystem": "python",
            "subdir": "services/agentd-py",
            "manifest_path": "services/agentd-py/pyproject.toml",
            "package_manager": "uv", "install_command": "uv sync",
            "interpreter_or_runner": ".venv/bin/python",  # ← missing subdir
            "test_command": "pytest",
            "declared_dependencies_top": [], "notes": None,
        }],
        "conventions_notes": None,
    }
    engine = ScriptedReasoningEngine(plan=None, patches=[], draft_conventions_responses=[canned])
    builder = EnvProfileBuilder(reasoner=engine)
    profile = await builder.build(tmp_path)

    assert profile.ecosystems[0].interpreter_or_runner == "services/agentd-py/.venv/bin/python"


@pytest.mark.asyncio
async def test_build_nulls_llm_interpreter_when_file_absent(tmp_path: Path):
    """LLM may return a conventional path; if the file isn't on disk yet,
    null it. The agent reads the diagnostic and learns to setup_env first."""
    sub = tmp_path / "services" / "agentd-py"
    sub.mkdir(parents=True)
    (sub / "pyproject.toml").write_text("[project]\nname=\"x\"\nversion=\"0\"\n")
    # No venv created.
    canned = {
        "ecosystems": [{
            "ecosystem": "python",
            "subdir": "services/agentd-py",
            "manifest_path": "services/agentd-py/pyproject.toml",
            "package_manager": "uv", "install_command": "uv sync",
            "interpreter_or_runner": "services/agentd-py/.venv/bin/python",
            "test_command": "pytest",
            "declared_dependencies_top": [], "notes": None,
        }],
        "conventions_notes": None,
    }
    engine = ScriptedReasoningEngine(plan=None, patches=[], draft_conventions_responses=[canned])
    builder = EnvProfileBuilder(reasoner=engine)
    profile = await builder.build(tmp_path)

    assert profile.ecosystems[0].interpreter_or_runner is None


@pytest.mark.asyncio
async def test_build_returns_profile_from_probe_and_llm(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname=\"demo\"\nversion=\"0\"\ndependencies=[\"fastapi\"]\n"
    )
    canned = {
        "ecosystems": [{
            "ecosystem": "python", "subdir": "", "manifest_path": "pyproject.toml",
            "package_manager": "uv", "install_command": "uv sync",
            "interpreter_or_runner": ".venv/bin/python", "test_command": "pytest",
            "declared_dependencies_top": ["fastapi"], "notes": None,
        }],
        "conventions_notes": "uses uv",
    }
    engine = ScriptedReasoningEngine(
        plan=None, patches=[], draft_conventions_responses=[canned],
    )
    builder = EnvProfileBuilder(reasoner=engine)
    profile = await builder.build(tmp_path)

    assert profile.workspace_root == str(tmp_path.resolve())
    assert profile.bootstrap_needed is False
    assert len(profile.ecosystems) == 1
    assert profile.ecosystems[0].install_command == "uv sync"
    assert profile.conventions_notes == "uses uv"
    assert isinstance(profile.built_at, datetime)


@pytest.mark.asyncio
async def test_build_on_bare_workspace_sets_bootstrap_needed(tmp_path: Path):
    engine = ScriptedReasoningEngine(
        plan=None, patches=[], draft_conventions_responses=[],
    )
    builder = EnvProfileBuilder(reasoner=engine)
    profile = await builder.build(tmp_path)

    assert profile.bootstrap_needed is True
    assert profile.ecosystems == []
    assert any("no manifests" in d.lower() for d in profile.diagnostics)


@pytest.mark.asyncio
async def test_build_passes_diagnostics_through_to_profile(tmp_path: Path):
    # Trigger the SETUPTOOLS_FLAT_LAYOUT_RISK diagnostic via the probe.
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname=\"demo\"\nversion=\"0\"\n"
        "[build-system]\nrequires=[\"setuptools>=68\"]\n"
        "build-backend=\"setuptools.build_meta\"\n"
    )
    (tmp_path / "agentd").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "workspaces").mkdir()
    canned = {
        "ecosystems": [{
            "ecosystem": "python", "subdir": "", "manifest_path": "pyproject.toml",
            "package_manager": "uv", "install_command": "uv sync",
            "interpreter_or_runner": ".venv/bin/python", "test_command": "pytest",
            "declared_dependencies_top": [], "notes": "flat-layout — added find stanza",
        }],
        "conventions_notes": None,
    }
    engine = ScriptedReasoningEngine(
        plan=None, patches=[], draft_conventions_responses=[canned],
    )
    builder = EnvProfileBuilder(reasoner=engine)
    profile = await builder.build(tmp_path)

    assert any("SETUPTOOLS_FLAT_LAYOUT_RISK" in d for d in profile.diagnostics)


@pytest.mark.asyncio
async def test_build_retries_once_on_llm_error_then_marks_bootstrap_needed(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname=\"x\"\nversion=\"0\"\n")

    class BrokenEngine:
        calls = 0

        async def draft_conventions(self, *, probe):
            BrokenEngine.calls += 1
            raise RuntimeError("boom")

    builder = EnvProfileBuilder(reasoner=BrokenEngine())
    profile = await builder.build(tmp_path)
    assert BrokenEngine.calls == 2  # one retry
    assert profile.bootstrap_needed is True
    assert any("convention drafting failed" in d for d in profile.diagnostics)
