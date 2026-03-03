from __future__ import annotations

from pathlib import Path

import pytest

from agentd.validation.command_validator import CommandValidator, ValidationCommand


@pytest.mark.asyncio
async def test_command_validator_with_explicit_commands_passes(tmp_path: Path) -> None:
    validator = CommandValidator(
        configured_commands=[
            ValidationCommand(stage="syntax", name="echo", command="python -c \"print('ok')\"")
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
                command="python -c \"import sys; sys.exit(2)\"",
            )
        ]
    )

    result = await validator.run(str(tmp_path))
    assert not result.success
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0].source == "validator:force-fail"
    assert result.diagnostics[0].level == "error"


@pytest.mark.asyncio
async def test_command_validator_fails_when_no_commands_detected(tmp_path: Path) -> None:
    validator = CommandValidator(configured_commands=None)

    result = await validator.run(str(tmp_path))
    assert not result.success
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0].source == "validator"
