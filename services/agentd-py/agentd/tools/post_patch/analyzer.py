"""PostPatchAnalyzer — orchestrates language detection, runs checkers, formats output."""
from __future__ import annotations

import re
from pathlib import Path

from agentd.tools.post_patch.checker import CheckResult
from agentd.tools.post_patch.language import LanguageChecker


def _normalize_line(line: str) -> str:
    """Produce a stable fingerprint for a single error line.

    Strips line:col references so that the same logical error at a shifted
    line number still matches the baseline fingerprint captured before patching.
    """
    return re.sub(r"(:\d+){1,2}(?=:|\s|$)", "", line)


class PostPatchAnalyzer:
    """Runs static checks on touched files and returns a formatted summary string."""

    def __init__(self, checkers: list[LanguageChecker]) -> None:
        self._checkers = checkers

    async def collect_baseline(
        self,
        root: Path,
        files: list[str],
    ) -> frozenset[str]:
        """Run the same checkers on the PRE-PATCH version of *files* and return the
        normalized fingerprints of every non-passing output line.

        This produces a baseline in the EXACT format that analyze()'s per-line
        filter (_normalize_line) expects — unlike the full-validation baseline,
        whose whole-message fingerprints can never match a per-line lookup. Call
        with root pointing at the pre-patch content (real workspace) for the files
        the step is about to modify; pass the result to analyze(baseline=...).
        """
        if not files:
            return frozenset()
        paths = [Path(f) for f in files]
        fingerprints: set[str] = set()
        for checker in self._checkers:
            matched = [p for p in paths if checker.matches(p)]
            if not matched:
                continue
            results = await checker.check(matched, root)
            for r in results:
                if r.skipped or r.passed:
                    continue
                for line in r.output.splitlines():
                    if line.strip():
                        fingerprints.add(_normalize_line(line))
        return frozenset(fingerprints)

    async def analyze(
        self,
        shadow_root: Path,
        touched_files: list[str],
        *,
        baseline: frozenset[str] | None = None,
    ) -> tuple[str, bool]:
        """Return (formatted_text, blocking_clean) where blocking_clean is True when no
        blocking checker (py_compile, mypy) has failures.

        When *baseline* is supplied (normalized error fingerprints from
        _collect_baseline_errors), any checker output line whose fingerprint
        appears in the baseline is treated as pre-existing and suppressed.
        """
        if not touched_files:
            return "", True

        paths = [Path(f) for f in touched_files]
        sections: list[tuple[str, bool]] = []
        has_blocking_failures = False

        for checker in self._checkers:
            matched = [p for p in paths if checker.matches(p)]
            if not matched:
                continue
            results = await checker.check(matched, shadow_root)
            if baseline:
                results = [_filter_result(r, baseline) for r in results]
            if any(r.blocking and not r.passed and not r.skipped for r in results):
                has_blocking_failures = True
            section = _format_section(checker.name, results)
            if section:
                sections.append(section)

        if not sections:
            return "", not has_blocking_failures

        blocking_sections = [s for s, is_blocking in sections if is_blocking]
        advisory_sections = [s for s, is_blocking in sections if not is_blocking]

        parts: list[str] = []
        if blocking_sections:
            parts.append("\nAUTO-CHECKS — BLOCKING (fix before verify_done):")
            parts.extend(blocking_sections)
        if advisory_sections:
            parts.append("\nAUTO-CHECKS — ADVISORY (informational only, do not patch-loop to fix style):")
            parts.extend(advisory_sections)
        return "\n".join(parts), not has_blocking_failures


def _filter_result(result: CheckResult, baseline: frozenset[str]) -> CheckResult:
    """Remove output lines whose normalized fingerprint appears in the baseline."""
    if result.skipped or result.passed:
        return result
    filtered_lines = [
        line for line in result.output.splitlines()
        if _normalize_line(line) not in baseline
    ]
    filtered_output = "\n".join(filtered_lines)
    return CheckResult(
        name=result.name,
        passed=not filtered_output.strip(),
        output=filtered_output,
        blocking=result.blocking,
    )


def _format_section(language: str, results: list[CheckResult]) -> tuple[str, bool] | None:
    """Return (formatted_string, is_blocking) or None if nothing to show.

    is_blocking is True only when at least one *failing* check is blocking.
    A section with only advisory failures is classified as advisory even when
    other (passing) checks in the same section are blocking tools.
    """
    lines: list[str] = [f"  [{language.upper()}]"]
    has_content = False
    has_blocking_failure = False

    for r in results:
        if r.skipped:
            continue
        has_content = True
        if r.passed:
            lines.append(f"    {r.name}: ✓")
        elif r.blocking:
            has_blocking_failure = True
            lines.append(f"    {r.name}: FAIL — must fix before verify_done")
            for out_line in r.output.splitlines()[:20]:
                lines.append(f"      {out_line}")
        else:
            lines.append(f"    {r.name}: issues (for review, no action needed)")
            for out_line in r.output.splitlines()[:20]:
                lines.append(f"      {out_line}")

    if not has_content:
        return None
    return "\n".join(lines), has_blocking_failure
