from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SkillManifest:
    """A discovered skill's catalog entry. The body is read lazily by read_skill."""

    name: str
    description: str
    body_path: Path
    dir: Path
