"""Structural grouping of CheckRunners by language."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agentd.tools.post_patch.checker import CheckResult, CheckRunner


@dataclass
class LanguageChecker:
    """Owns the file-extension set and ordered runner list for one language."""

    name: str
    extensions: frozenset[str]
    runners: list[CheckRunner] = field(default_factory=list)

    def matches(self, path: Path) -> bool:
        return path.suffix in self.extensions

    async def check(self, files: list[Path], cwd: Path) -> list[CheckResult]:
        """Run every runner against the given files (already filtered to this language)."""
        return [await runner.run(files, cwd) for runner in self.runners]
