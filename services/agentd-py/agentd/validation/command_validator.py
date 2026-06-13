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
from agentd.tools._paths import prepend_pythonpath, shadow_pythonpath_extras

ValidationStage = Literal["syntax", "type", "lint", "test"]

# Directories that are not part of the project under test — build/dep caches plus
# vendored reference material (e.g. aider-references is a read-only reference dump,
# not project source). Excluded from compileall/mypy so a stray relative-import or
# syntax error in reference material can't abort the whole baseline run. NOTE: we
# exclude by path, NOT via --ignore-missing-imports — genuine missing imports in
# real project code must still be caught.
_NON_PROJECT_EXCLUDE_RE = (
    r"(^|/)(\.venv|node_modules|\.git|target|dist|__pycache__|aider-references)(/|$)"
)


@dataclass(frozen=True)
class ValidationCommand:
    stage: ValidationStage
    name: str
    command: str
    timeout_sec: int = 300
    # When True, a non-zero exit emits a warning instead of an error.
    # Use for linters/type-checkers that should inform but not block.
    warning_only: bool = False


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
            warning_only = bool(item.get("warning_only", False))

            if not command:
                msg = f"Validation command '{name}' is missing command text"
                raise ValueError(msg)

            commands.append(
                ValidationCommand(
                    stage=stage_raw,
                    name=name,
                    command=command,
                    timeout_sec=timeout_sec,
                    warning_only=warning_only,
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

    async def run_touched(self, workspace_path: str, touched_files: list[str]) -> ValidationResult:
        started_at = time.monotonic()
        diagnostics: list[Diagnostic] = []
        root = Path(workspace_path)
        ts_available: bool | None = None
        ts_unavailable_reason: str | None = None
        rustfmt_bin = shutil.which("rustfmt")
        rustfmt_warned = False

        for relative_path in touched_files:
            path = (root / relative_path).resolve()
            try:
                path.relative_to(root.resolve())
            except ValueError:
                diagnostics.append(
                    Diagnostic(
                        source="validator:fast",
                        message=f"Path escapes workspace: {relative_path}",
                        level="error",
                        file=relative_path,
                    )
                )
                continue

            if not path.exists() or not path.is_file():
                continue

            if path.suffix == ".py":
                try:
                    source = path.read_text(encoding="utf-8")
                    compile(source, str(path), "exec")
                except Exception as exc:
                    diagnostics.append(
                        Diagnostic(
                            source="validator:fast-python-compile",
                            message=f"{relative_path}: {exc}",
                            level="error",
                            file=relative_path,
                        )
                    )
            elif path.suffix in {".ts", ".tsx", ".mts", ".cts"}:
                if ts_available is None:
                    ts_available, ts_unavailable_reason = await self._check_typescript_fast_support(root)
                    if not ts_available:
                        diagnostics.append(
                            Diagnostic(
                                source="validator:fast-typescript",
                                message=ts_unavailable_reason
                                or "TypeScript fast validation unavailable; skipping touched-file parse checks",
                                level="warning",
                            )
                        )
                if ts_available:
                    diagnostics.extend(
                        await self._run_typescript_fast_parse(root, path, relative_path)
                    )
            elif path.suffix == ".rs":
                if not rustfmt_bin:
                    if not rustfmt_warned:
                        diagnostics.append(
                            Diagnostic(
                                source="validator:fast-rust",
                                message="rustfmt is not available; skipping Rust touched-file parse checks",
                                level="warning",
                            )
                        )
                        rustfmt_warned = True
                else:
                    diagnostics.extend(
                        await self._run_rust_fast_parse(root, path, relative_path, rustfmt_bin)
                    )

        duration_ms = int((time.monotonic() - started_at) * 1000)
        return ValidationResult(
            success=not any(item.level == "error" for item in diagnostics),
            diagnostics=diagnostics,
            duration_ms=duration_ms,
        )

    async def _check_typescript_fast_support(self, workspace_path: Path) -> tuple[bool, str | None]:
        node_bin = shutil.which("node")
        if not node_bin:
            return False, "Node.js is not available; skipping TypeScript touched-file parse checks"

        returncode, _stdout, stderr, timed_out = await self._run_process_exec(
            [node_bin, "-e", "require.resolve('typescript')"],
            cwd=workspace_path,
            timeout_sec=10,
        )
        if timed_out:
            return False, "Timed out while checking TypeScript parser availability"
        if returncode != 0:
            details = stderr.strip() or "typescript package is not resolvable from workspace"
            return False, f"TypeScript fast validation unavailable: {details}"
        return True, None

    async def _run_typescript_fast_parse(
        self,
        workspace_path: Path,
        file_path: Path,
        relative_path: str,
    ) -> list[Diagnostic]:
        node_bin = shutil.which("node")
        if not node_bin:
            return []

        script = (
            "const fs=require('fs');"
            "const ts=require('typescript');"
            "const file=process.argv[1];"
            "const src=fs.readFileSync(file,'utf8');"
            "const out=ts.transpileModule(src,{fileName:file,reportDiagnostics:true,"
            "compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ESNext,jsx:ts.JsxEmit.Preserve}});"
            "const diags=(out.diagnostics||[]).filter(d=>d.category===ts.DiagnosticCategory.Error);"
            "if(diags.length){"
            "const host={getCurrentDirectory:()=>process.cwd(),getCanonicalFileName:f=>f,getNewLine:()=>\"\\n\"};"
            "console.error(ts.formatDiagnosticsWithColorAndContext(diags,host));"
            "process.exit(1);"
            "}"
        )
        returncode, stdout, stderr, timed_out = await self._run_process_exec(
            [node_bin, "-e", script, str(file_path)],
            cwd=workspace_path,
            timeout_sec=20,
        )
        if timed_out:
            return [
                Diagnostic(
                    source="validator:fast-typescript-parse",
                    message=f"{relative_path}: TypeScript parse check timed out",
                    level="error",
                    file=relative_path,
                )
            ]
        if returncode == 0:
            return []
        output = "\n".join(part.strip() for part in [stdout, stderr] if part.strip())
        message = output or f"{relative_path}: TypeScript parse check failed"
        return [
            Diagnostic(
                source="validator:fast-typescript-parse",
                message=message,
                level="error",
                file=relative_path,
            )
        ]

    async def _run_rust_fast_parse(
        self,
        workspace_path: Path,
        file_path: Path,
        relative_path: str,
        rustfmt_bin: str,
    ) -> list[Diagnostic]:
        returncode, stdout, stderr, timed_out = await self._run_process_exec(
            [rustfmt_bin, "--emit", "stdout", "--edition", "2021", str(file_path)],
            cwd=workspace_path,
            timeout_sec=20,
        )
        if timed_out:
            return [
                Diagnostic(
                    source="validator:fast-rust-parse",
                    message=f"{relative_path}: Rust parse check timed out",
                    level="error",
                    file=relative_path,
                )
            ]
        if returncode == 0:
            return []
        output = "\n".join(part.strip() for part in [stdout, stderr] if part.strip())
        message = output or f"{relative_path}: Rust parse check failed"
        return [
            Diagnostic(
                source="validator:fast-rust-parse",
                message=message,
                level="error",
                file=relative_path,
            )
        ]

    async def _run_process_exec(
        self,
        args: list[str],
        *,
        cwd: Path,
        timeout_sec: int,
    ) -> tuple[int, str, str, bool]:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_raw, stderr_raw = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_sec,
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            return -1, "", "", True

        return (
            process.returncode,
            stdout_raw.decode("utf-8", errors="replace"),
            stderr_raw.decode("utf-8", errors="replace"),
            False,
        )

    async def _run_command(self, command: ValidationCommand, workspace_path: Path) -> list[Diagnostic]:
        # workspace_path is the shadow. Prepend the shadow's import root(s) so pytest/mypy
        # import the edited package under test, not an installed copy — same redirect the
        # agent's run_command applies (e.g. agentd run via --agentd-dir would otherwise
        # import the dev worktree and miss freshly-added symbols).
        env = prepend_pythonpath(os.environ.copy(), shadow_pythonpath_extras(workspace_path))
        process = await asyncio.create_subprocess_shell(
            command.command,
            cwd=str(workspace_path),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=command.timeout_sec)
        except TimeoutError:
            process.kill()
            await process.wait()
            level = "warning" if command.warning_only else "error"
            return [
                Diagnostic(
                    source=f"validator:{command.name}",
                    message=f"Command timed out after {command.timeout_sec}s: {command.command}",
                    level=level,
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
        level = "warning" if command.warning_only else "error"
        return [
            Diagnostic(
                source=f"validator:{command.name}",
                message=message,
                level=level,
            )
        ]

    @staticmethod
    def _resolve_bin(workspace_path: Path, name: str) -> str | None:
        """Workspace-local bin first (.venv/bin, node_modules/.bin, target/...);
        falls back to system PATH. Returns the absolute path string, or None."""
        from agentd.tools._paths import resolve_workspace_bin
        local = resolve_workspace_bin(workspace_path, name)
        if local is not None:
            return str(local)
        return shutil.which(name)

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
                        f"-x '{_NON_PROJECT_EXCLUDE_RE}' ."
                    ),
                )
            )
            mypy = self._resolve_bin(workspace_path, "mypy")
            if mypy:
                commands.append(
                    ValidationCommand(
                        stage="type",
                        name="mypy",
                        command=f"{shlex.quote(mypy)} --exclude {shlex.quote(_NON_PROJECT_EXCLUDE_RE)} .",
                        warning_only=True,
                    )
                )
            ruff = self._resolve_bin(workspace_path, "ruff")
            if ruff:
                commands.append(
                    ValidationCommand(
                        stage="lint",
                        name="ruff",
                        command=f"{shlex.quote(ruff)} check .",
                        warning_only=True,
                    )
                )
            pytest_bin = self._resolve_bin(workspace_path, "pytest")
            if pytest_bin:
                # Invoke `python -m pytest`, not the bare console script: `-m` puts cwd
                # (the shadow root) on sys.path[0], so root-level imports (`from src.…`)
                # resolve the same way the execution agent's per-step `python -m pytest`
                # verify does. The bare pytest entry point does NOT add cwd, so a test
                # that passes every step verify would fail full validation at collection,
                # tripping a spurious REPAIRING loop. Prefer the python that owns the
                # resolved pytest (same venv → its installed deps); fall back to ours.
                sibling_python = Path(pytest_bin).with_name("python")
                python_exec = str(sibling_python) if sibling_python.exists() else sys.executable
                commands.append(
                    ValidationCommand(
                        stage="test",
                        name="pytest",
                        command=f"{shlex.quote(python_exec)} -m pytest",
                    )
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

            # Fallback: detect node_modules-installed test runners even when
            # package.json declares no test/lint scripts. Many bare templates
            # ship a binary under node_modules/.bin without scripts entries.
            scripts_dict = scripts if isinstance(scripts, dict) else {}
            if "test" not in scripts_dict:
                vitest = self._resolve_bin(workspace_path, "vitest")
                if vitest:
                    commands.append(
                        ValidationCommand(
                            stage="test",
                            name="vitest",
                            command=f"{shlex.quote(vitest)} run",
                            timeout_sec=300,
                        )
                    )
                else:
                    jest = self._resolve_bin(workspace_path, "jest")
                    if jest:
                        commands.append(
                            ValidationCommand(
                                stage="test",
                                name="jest",
                                command=shlex.quote(jest),
                                timeout_sec=300,
                            )
                        )
            if "typecheck" not in scripts_dict:
                tsc = self._resolve_bin(workspace_path, "tsc")
                if tsc:
                    commands.append(
                        ValidationCommand(
                            stage="type",
                            name="tsc",
                            command=f"{shlex.quote(tsc)} --noEmit",
                            warning_only=True,
                        )
                    )
            if "lint" not in scripts_dict:
                eslint = self._resolve_bin(workspace_path, "eslint")
                if eslint:
                    commands.append(
                        ValidationCommand(
                            stage="lint",
                            name="eslint",
                            command=f"{shlex.quote(eslint)} .",
                            warning_only=True,
                        )
                    )

        cargo_toml = workspace_path / "Cargo.toml"
        if cargo_toml.exists() and shutil.which("cargo"):
            commands.append(
                ValidationCommand(
                    stage="syntax",
                    name="cargo-check",
                    command="cargo check --all-targets 2>&1",
                    timeout_sec=120,
                )
            )
            commands.append(
                ValidationCommand(
                    stage="test",
                    name="cargo-test",
                    command="cargo test 2>&1",
                    timeout_sec=300,
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
