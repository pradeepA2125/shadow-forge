from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agentd.domain.models import Diagnostic, ValidationResult

ValidationStage = Literal["syntax", "type", "lint", "test"]


@dataclass(frozen=True)
class ValidationCommand:
    stage: ValidationStage
    name: str
    command: str
    timeout_sec: int = 300


class CommandValidator:
    def __init__(
        self,
        configured_commands: list[ValidationCommand] | None = None,
    ) -> None:
        self._configured_commands = configured_commands

    @classmethod
    def from_env(cls) -> "CommandValidator":
        raw = os.getenv("AI_EDITOR_VALIDATION_COMMANDS_JSON", "").strip()
        if not raw:
            return cls(configured_commands=None)

        payload = json.loads(raw)
        if not isinstance(payload, list):
            msg = "AI_EDITOR_VALIDATION_COMMANDS_JSON must be a JSON array"
            raise ValueError(msg)

        commands: list[ValidationCommand] = []
        for item in payload:
            if not isinstance(item, dict):
                msg = "Validation command item must be an object"
                raise ValueError(msg)

            stage_raw = item.get("stage")
            if stage_raw not in {"syntax", "type", "lint", "test"}:
                msg = f"Invalid validation stage: {stage_raw}"
                raise ValueError(msg)

            command = str(item.get("command", "")).strip()
            name = str(item.get("name", "")).strip() or f"{stage_raw}-check"
            timeout_sec = int(item.get("timeout_sec", 300))

            if not command:
                msg = f"Validation command '{name}' is missing command text"
                raise ValueError(msg)

            commands.append(
                ValidationCommand(
                    stage=stage_raw,
                    name=name,
                    command=command,
                    timeout_sec=timeout_sec,
                )
            )

        return cls(configured_commands=commands)

    async def run(self, workspace_path: str) -> ValidationResult:
        started_at = time.monotonic()
        diagnostics: list[Diagnostic] = []

        commands = self._configured_commands or self._detect_default_commands(Path(workspace_path))
        if not commands:
            diagnostics.append(
                Diagnostic(
                    source="validator",
                    message=(
                        "No validation commands configured or detected. "
                        "Set AI_EDITOR_VALIDATION_COMMANDS_JSON or ensure supported project tooling exists."
                    ),
                    level="error",
                )
            )
            return ValidationResult(success=False, diagnostics=diagnostics, duration_ms=0)

        for command in commands:
            diagnostics.extend(await self._run_command(command, Path(workspace_path)))

        has_errors = any(d.level == "error" for d in diagnostics)
        duration_ms = int((time.monotonic() - started_at) * 1000)
        return ValidationResult(success=not has_errors, diagnostics=diagnostics, duration_ms=duration_ms)

    async def _run_command(self, command: ValidationCommand, workspace_path: Path) -> list[Diagnostic]:
        process = await asyncio.create_subprocess_shell(
            command.command,
            cwd=str(workspace_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=command.timeout_sec)
        except TimeoutError:
            process.kill()
            await process.wait()
            return [
                Diagnostic(
                    source=f"validator:{command.name}",
                    message=f"Command timed out after {command.timeout_sec}s: {command.command}",
                    level="error",
                )
            ]

        if process.returncode == 0:
            return []

        output = "\n".join(
            part.strip()
            for part in [stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")]
            if part.strip()
        )
        message = output or f"Command failed with exit code {process.returncode}: {command.command}"
        return [
            Diagnostic(
                source=f"validator:{command.name}",
                message=message,
                level="error",
            )
        ]

    def _detect_default_commands(self, workspace_path: Path) -> list[ValidationCommand]:
        commands: list[ValidationCommand] = []

        has_python = self._has_files(workspace_path, suffixes={".py"})
        if has_python:
            python_exec = shlex.quote(sys.executable)
            commands.append(
                ValidationCommand(
                    stage="syntax",
                    name="python-compileall",
                    command=(
                        f"{python_exec} -m compileall -q "
                        "-x '(^|/)(\\.venv|node_modules|\\.git|target|dist|__pycache__)(/|$)' ."
                    ),
                )
            )
            if shutil.which("mypy"):
                commands.append(
                    ValidationCommand(stage="type", name="mypy", command="mypy .")
                )
            if shutil.which("ruff"):
                commands.append(
                    ValidationCommand(stage="lint", name="ruff", command="ruff check .")
                )
            if shutil.which("pytest"):
                commands.append(
                    ValidationCommand(stage="test", name="pytest", command="pytest -q")
                )

        package_json = workspace_path / "package.json"
        if package_json.exists():
            try:
                payload = json.loads(package_json.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {}

            scripts = payload.get("scripts", {}) if isinstance(payload, dict) else {}
            if isinstance(scripts, dict):
                if "typecheck" in scripts:
                    commands.append(
                        ValidationCommand(
                            stage="type",
                            name="npm-typecheck",
                            command="npm run -s typecheck",
                        )
                    )
                if "lint" in scripts:
                    commands.append(
                        ValidationCommand(
                            stage="lint",
                            name="npm-lint",
                            command="npm run -s lint",
                        )
                    )
                if "test" in scripts:
                    commands.append(
                        ValidationCommand(
                            stage="test",
                            name="npm-test",
                            command="npm run -s test",
                        )
                    )

        return commands

    def _has_files(self, workspace_path: Path, suffixes: set[str]) -> bool:
        skip_dirs = {".git", ".venv", "node_modules", "target", "dist", "__pycache__"}
        for root, dirs, files in os.walk(workspace_path):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for file_name in files:
                if Path(file_name).suffix in suffixes:
                    _ = root
                    return True
        return False
