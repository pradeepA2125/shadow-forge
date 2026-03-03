from __future__ import annotations

from fnmatch import fnmatch
from typing import Iterable, Sequence


DEFAULT_FORBIDDEN_PATTERNS: tuple[str, ...] = (
    ".git/**",
    ".env",
    ".env.*",
    "**/*.pem",
    "**/*.key",
    "**/id_rsa",
    "**/id_ed25519",
)


class PatchPolicyViolation(RuntimeError):
    pass


class ForbiddenPathPolicy:
    def __init__(self, forbidden_patterns: Sequence[str] | None = None) -> None:
        self._forbidden_patterns = tuple(forbidden_patterns or DEFAULT_FORBIDDEN_PATTERNS)

    def validate_paths(self, relative_paths: Iterable[str]) -> None:
        for path in relative_paths:
            normalized = path.replace("\\", "/")
            if normalized.startswith("./"):
                normalized = normalized[2:]

            if normalized in {"", ".", ".."} or normalized.startswith("../"):
                msg = f"Unsafe relative path in patch operation: {path}"
                raise PatchPolicyViolation(msg)

            for pattern in self._forbidden_patterns:
                if fnmatch(normalized, pattern):
                    msg = f"Patch operation targets forbidden path '{path}' (pattern: {pattern})"
                    raise PatchPolicyViolation(msg)
