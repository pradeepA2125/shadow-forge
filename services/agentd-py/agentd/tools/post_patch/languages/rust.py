"""Rust static checker: cargo check."""
from __future__ import annotations

from pathlib import Path

from agentd.tools.post_patch.checker import CheckResult, run_subprocess
from agentd.tools.post_patch.language import LanguageChecker


class CargoCheckRunner:
    name = "cargo check"

    async def run(self, files: list[Path], cwd: Path) -> CheckResult:
        manifest = _find_manifest(files[0], cwd)
        if not manifest:
            return CheckResult(name=self.name, passed=True, output="", skipped=True)
        code, out = await run_subprocess(
            ["cargo", "check", "--manifest-path", str(manifest), "--message-format=short"],
            cwd,
            timeout=120,
        )
        if code == -1 and not out:
            return CheckResult(name=self.name, passed=True, output="", skipped=True)
        return CheckResult(name=self.name, passed=(code == 0), output=out)


def _find_manifest(start: Path, cwd: Path) -> Path | None:
    """Walk up from start toward cwd looking for the nearest Cargo.toml."""
    current = (cwd / start).parent
    for _ in range(8):
        candidate = current / "Cargo.toml"
        if candidate.exists():
            return candidate
        if current == cwd or current == current.parent:
            break
        current = current.parent
    return None


def make_rust_checker() -> LanguageChecker:
    return LanguageChecker(
        name="rust",
        extensions=frozenset({".rs"}),
        runners=[CargoCheckRunner()],
    )
