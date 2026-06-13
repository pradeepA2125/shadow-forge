from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

from agentd.validation.command_validator import CommandValidator, ValidationCommand


@pytest.mark.asyncio
async def test_command_validator_with_explicit_commands_passes(tmp_path: Path) -> None:
    validator = CommandValidator(
        configured_commands=[
            ValidationCommand(
                stage="syntax",
                name="echo",
                command=f'"{sys.executable}" -c "print(\'ok\')"',
            )
        ]
    )

    result = await validator.run(str(tmp_path))
    assert result.success
    assert result.diagnostics == []


@pytest.mark.asyncio
async def test_command_validator_reports_failure(tmp_path: Path) -> None:
    validator = CommandValidator(
        configured_commands=[
            ValidationCommand(
                stage="test",
                name="force-fail",
                command=f'"{sys.executable}" -c "import sys; sys.exit(2)"',
            )
        ]
    )

    result = await validator.run(str(tmp_path))
    assert not result.success
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0].source == "validator:force-fail"
    assert result.diagnostics[0].level == "error"


@pytest.mark.asyncio
async def test_command_validator_prepends_shadow_import_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Full validation runs against the shadow; its subprocesses must import the shadow's
    # copy of an edited package, not an installed one. Verify the shadow import root lands
    # on the child's PYTHONPATH.
    from agentd.tools import _paths

    (tmp_path / "pkgs" / "mypkg").mkdir(parents=True)
    (tmp_path / "pkgs" / "mypkg" / "__init__.py").write_text("")
    monkeypatch.setattr(_paths, "editable_package_names", lambda: {"mypkg"})

    validator = CommandValidator(
        configured_commands=[
            ValidationCommand(
                stage="test",
                name="show-pythonpath",
                command=f'"{sys.executable}" -c \'import os,sys; print(os.environ.get("PYTHONPATH","")); sys.exit(1)\'',
            )
        ]
    )

    result = await validator.run(str(tmp_path))
    assert not result.success
    assert str(tmp_path / "pkgs") in result.diagnostics[0].message


@pytest.mark.asyncio
async def test_command_validator_fails_when_no_commands_detected(tmp_path: Path) -> None:
    validator = CommandValidator(configured_commands=None)

    result = await validator.run(str(tmp_path))
    assert not result.success
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0].source == "validator"


@pytest.mark.asyncio
async def test_run_touched_python_syntax_error_fails(tmp_path: Path) -> None:
    validator = CommandValidator(configured_commands=None)
    target = tmp_path / "bad.py"
    target.write_text("def broken(:\n    pass\n", encoding="utf-8")

    result = await validator.run_touched(str(tmp_path), ["bad.py"])
    assert not result.success
    assert any(
        diagnostic.source == "validator:fast-python-compile"
        and diagnostic.level == "error"
        for diagnostic in result.diagnostics
    )


def _make_executable(path: Path, content: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)
    return path


def test_detect_default_commands_finds_venv_pytest(tmp_path: Path) -> None:
    """When pytest is in <workspace>/.venv/bin, the validator runs `<venv python> -m
    pytest` (not the bare console script) so cwd lands on sys.path — matching the agent."""
    (tmp_path / "x.py").write_text("x = 1\n")
    _make_executable(tmp_path / ".venv" / "bin" / "pytest")
    venv_python = _make_executable(tmp_path / ".venv" / "bin" / "python")
    validator = CommandValidator(configured_commands=None)
    cmds = validator._detect_default_commands(tmp_path)
    pytest_cmd = next((c for c in cmds if c.name == "pytest"), None)
    assert pytest_cmd is not None, [c.name for c in cmds]
    assert pytest_cmd.command == f"{shlex.quote(str(venv_python))} -m pytest"


@pytest.mark.asyncio
async def test_default_pytest_validation_resolves_root_level_imports(tmp_path: Path) -> None:
    """A test importing a top-level package (`from src.… import …`) must pass full
    validation. The execution agent verifies each step with `python -m pytest` (which
    puts cwd on sys.path[0]); if the validator runs the bare `pytest` console script
    (which does NOT add cwd) the same test fails at collection, tripping a spurious
    REPAIRING loop. The default pytest command must match the agent's invocation."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "__init__.py").write_text("")
    (tmp_path / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text(
        "from src.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    validator = CommandValidator(configured_commands=None)
    result = await validator.run(str(tmp_path))
    assert result.success, [d.message for d in result.diagnostics]


def test_detect_default_commands_finds_node_modules_vitest_without_scripts(tmp_path: Path) -> None:
    """vitest in node_modules/.bin is detected even if package.json scripts.test missing."""
    (tmp_path / "package.json").write_text('{"name":"x","version":"0.0.0"}\n')
    vitest_bin = _make_executable(tmp_path / "node_modules" / ".bin" / "vitest")
    validator = CommandValidator(configured_commands=None)
    cmds = validator._detect_default_commands(tmp_path)
    vitest_cmd = next((c for c in cmds if c.name == "vitest"), None)
    assert vitest_cmd is not None, [c.name for c in cmds]
    assert str(vitest_bin) in vitest_cmd.command


def test_detect_default_commands_prefers_workspace_over_system(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Workspace-local mypy ranks above any system mypy."""
    (tmp_path / "x.py").write_text("x = 1\n")
    workspace_mypy = _make_executable(tmp_path / ".venv" / "bin" / "mypy")
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/local/bin/mypy" if name == "mypy" else None
    )
    validator = CommandValidator(configured_commands=None)
    cmds = validator._detect_default_commands(tmp_path)
    mypy_cmd = next((c for c in cmds if c.name == "mypy"), None)
    assert mypy_cmd is not None
    assert str(workspace_mypy) in mypy_cmd.command
    assert "/usr/local/bin/mypy" not in mypy_cmd.command


@pytest.mark.asyncio
async def test_run_touched_typescript_unavailable_is_warning_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    validator = CommandValidator(configured_commands=None)
    target = tmp_path / "x.ts"
    target.write_text("const x: number = 1;", encoding="utf-8")

    async def _fake_support(workspace_path: Path) -> tuple[bool, str | None]:
        _ = workspace_path
        return False, "typescript unavailable for test"

    monkeypatch.setattr(validator, "_check_typescript_fast_support", _fake_support)
    result = await validator.run_touched(str(tmp_path), ["x.ts"])
    assert result.success
    assert any(
        diagnostic.source == "validator:fast-typescript"
        and diagnostic.level == "warning"
        for diagnostic in result.diagnostics
    )
