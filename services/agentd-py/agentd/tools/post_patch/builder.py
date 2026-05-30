"""AnalyzerBuilder — fluent builder for PostPatchAnalyzer."""
from __future__ import annotations

from agentd.tools.post_patch.analyzer import PostPatchAnalyzer
from agentd.tools.post_patch.language import LanguageChecker
from agentd.tools.post_patch.languages.python import make_python_checker
from agentd.tools.post_patch.languages.rust import make_rust_checker
from agentd.tools.post_patch.languages.typescript import make_typescript_checker


class AnalyzerBuilder:
    """Fluent builder — call .with_*() for each language you want, then .build()."""

    def __init__(self) -> None:
        self._checkers: list[LanguageChecker] = []

    def with_python(self) -> "AnalyzerBuilder":
        self._checkers.append(make_python_checker())
        return self

    def with_typescript(self) -> "AnalyzerBuilder":
        self._checkers.append(make_typescript_checker())
        return self

    def with_rust(self) -> "AnalyzerBuilder":
        self._checkers.append(make_rust_checker())
        return self

    def build(self) -> PostPatchAnalyzer:
        return PostPatchAnalyzer(list(self._checkers))

    @classmethod
    def default(cls) -> PostPatchAnalyzer:
        """All three supported languages."""
        return cls().with_python().with_typescript().with_rust().build()
