"""Python static checkers: py_compile, ruff, mypy."""
from __future__ import annotations

from pathlib import Path

from agentd.tools.post_patch.checker import CheckResult, run_subprocess, python_executable
from agentd.tools.post_patch.language import LanguageChecker


class PyCompileRunner:
    name = "py_compile"

    async def run(self, files: list[Path], cwd: Path) -> CheckResult:
        issues: list[str] = []
        for f in files:
            code, out = await run_subprocess(
                [python_executable(), "-m", "py_compile", str(f)], cwd
            )
            if code != 0 and out:
                issues.append(out)
        return CheckResult(name=self.name, passed=not issues, output="\n".join(issues))


class RuffRunner:
    name = "ruff"

    async def run(self, files: list[Path], cwd: Path) -> CheckResult:
        code, out = await run_subprocess(
            ["ruff", "check", "--output-format=concise", *(str(f) for f in files)], cwd
        )
        if code == -1 and not out:
            return CheckResult(name=self.name, passed=True, output="", skipped=True)
        return CheckResult(name=self.name, passed=(code == 0), output=out, blocking=False)


class MypyRunner:
    name = "mypy"

    async def run(self, files: list[Path], cwd: Path) -> CheckResult:
        code, out = await run_subprocess(
            [
                python_executable(), "-m", "mypy",
                "--ignore-missing-imports",
                "--no-error-summary",
                *(str(f) for f in files),
            ],
            cwd,
            timeout=60,
        )
        if code == -1 and not out:
            return CheckResult(name=self.name, passed=True, output="", skipped=True)
        # mypy follows imports and reports errors in transitively-imported files that
        # the model never touched. Filter to only lines referencing the requested files
        # so the model isn't blamed for pre-existing issues in unrelated modules.
        file_strs = {str(f) for f in files}
        filtered = "\n".join(
            line for line in out.splitlines()
            if not line or any(line.startswith(f) for f in file_strs)
        )
        return CheckResult(name=self.name, passed=(not filtered.strip()), output=filtered)


def make_python_checker() -> LanguageChecker:
    return LanguageChecker(
        name="python",
        extensions=frozenset({".py"}),
        runners=[PyCompileRunner(), RuffRunner(), MypyRunner()],
    )
