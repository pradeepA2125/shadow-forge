from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from agentd.domain.models import TaskRecord


DEFAULT_IGNORE_PATTERNS: tuple[str, ...] = (
    ".git",
    "node_modules",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    "target",
    "dist",
)


@dataclass(frozen=True)
class ShadowWorkspace:
    task_id: str
    real_path: Path
    shadow_path: Path


class ShadowWorkspaceManager:
    def __init__(
        self,
        root_path: Path | None = None,
        ignore_patterns: Sequence[str] | None = None,
    ) -> None:
        self._root_path = (root_path or Path(tempfile.gettempdir()) / "ai-editor-shadow").resolve()
        self._ignore_patterns = tuple(ignore_patterns or DEFAULT_IGNORE_PATTERNS)
        self._root_path.mkdir(parents=True, exist_ok=True)

    async def prepare(self, task_id: str, workspace_path: str) -> ShadowWorkspace:
        real_path = Path(workspace_path).resolve()
        if not real_path.exists() or not real_path.is_dir():
            msg = f"Workspace path is not a directory: {workspace_path}"
            raise RuntimeError(msg)

        shadow_path = self._resolve_shadow_path(task_id)
        if shadow_path.exists():
            shutil.rmtree(shadow_path)

        shutil.copytree(
            real_path,
            shadow_path,
            ignore=shutil.ignore_patterns(*self._ignore_patterns),
        )

        return ShadowWorkspace(
            task_id=task_id,
            real_path=real_path,
            shadow_path=shadow_path,
        )

    async def promote(self, task: TaskRecord) -> None:
        if task.shadow_workspace_path is None:
            msg = f"Task {task.task_id} has no shadow workspace"
            raise RuntimeError(msg)

        real_path = Path(task.workspace_path).resolve()
        shadow_path = Path(task.shadow_workspace_path).resolve()

        for relative_path in sorted(set(task.modified_files)):
            source = self._resolve_inside(shadow_path, relative_path)
            destination = self._resolve_inside(real_path, relative_path)

            if source.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                if source.is_dir():
                    if destination.exists():
                        shutil.rmtree(destination)
                    shutil.copytree(source, destination)
                else:
                    shutil.copy2(source, destination)
                continue

            if destination.is_file():
                destination.unlink()
            elif destination.is_dir():
                shutil.rmtree(destination)

    async def cleanup(self, task: TaskRecord) -> None:
        if task.shadow_workspace_path is None:
            return

        shadow_path = Path(task.shadow_workspace_path).resolve()
        if shadow_path.exists():
            shutil.rmtree(shadow_path)

    def _resolve_shadow_path(self, task_id: str) -> Path:
        shadow_path = (self._root_path / task_id).resolve()
        try:
            shadow_path.relative_to(self._root_path)
        except ValueError as exc:
            msg = f"Unsafe task id for shadow workspace path: {task_id}"
            raise RuntimeError(msg) from exc
        return shadow_path

    def _resolve_inside(self, root: Path, relative_path: str) -> Path:
        candidate = (root / relative_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            msg = f"Path escapes workspace: {relative_path}"
            raise RuntimeError(msg) from exc
        return candidate
