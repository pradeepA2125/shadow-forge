"""TypeScript static checker: tsc --noEmit."""
from __future__ import annotations

from pathlib import Path

from agentd.tools.post_patch.checker import CheckResult, run_subprocess
from agentd.tools.post_patch.language import LanguageChecker


class TscRunner:
    name = "tsc"

    async def run(self, files: list[Path], cwd: Path) -> CheckResult:
        tsconfig = _find_tsconfig(files[0], cwd)
        if not tsconfig:
            return CheckResult(name=self.name, passed=True, output="", skipped=True)
        code, out = await run_subprocess(
            ["npx", "tsc", "--noEmit", "--project", str(tsconfig)],
            cwd,
            timeout=60,
        )
        if code == -1 and not out:
            return CheckResult(name=self.name, passed=True, output="", skipped=True)
        return CheckResult(name=self.name, passed=(code == 0), output=out)


def _find_tsconfig(start: Path, cwd: Path) -> Path | None:
    """Walk up from start toward cwd looking for the nearest tsconfig.json."""
    current = (cwd / start).parent
    for _ in range(8):
        candidate = current / "tsconfig.json"
        if candidate.exists():
            return candidate
        if current == cwd or current == current.parent:
            break
        current = current.parent
    return None


def make_typescript_checker() -> LanguageChecker:
    return LanguageChecker(
        name="typescript",
        extensions=frozenset({".ts", ".tsx"}),
        runners=[TscRunner()],
    )
