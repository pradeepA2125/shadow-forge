from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from agentd.domain.models import (
    CreateFileOp,
    DeleteFileOp,
    InsertAfterSymbolOp,
    PatchDocument,
    ReplaceRangeOp,
)
from agentd.patch.policy import ForbiddenPathPolicy


@dataclass(frozen=True)
class PatchResult:
    touched_files: list[str]


class PatchEngine:
    def __init__(self, policy: ForbiddenPathPolicy | None = None) -> None:
        self._policy = policy or ForbiddenPathPolicy()

    async def apply_patch_document(self, base_dir: str | Path, patch: PatchDocument) -> PatchResult:
        base_path = Path(base_dir).resolve()
        if not base_path.exists() or not base_path.is_dir():
            msg = f"Patch base path is not a directory: {base_path}"
            raise RuntimeError(msg)

        self._policy.validate_paths(op.file for op in patch.patch_ops)

        touched: set[str] = set()
        for operation in patch.patch_ops:
            if isinstance(operation, ReplaceRangeOp):
                self._apply_replace_range(base_path, operation)
            elif isinstance(operation, InsertAfterSymbolOp):
                self._apply_insert_after_symbol(base_path, operation)
            elif isinstance(operation, CreateFileOp):
                self._apply_create_file(base_path, operation)
            elif isinstance(operation, DeleteFileOp):
                self._apply_delete_file(base_path, operation)
            else:
                msg = f"Unsupported patch operation type: {type(operation).__name__}"
                raise RuntimeError(msg)
            touched.add(operation.file)

        return PatchResult(touched_files=sorted(touched))

    def _apply_replace_range(self, base_path: Path, operation: ReplaceRangeOp) -> None:
        target = self._resolve_inside(base_path, operation.file)
        lines = target.read_text(encoding="utf-8").splitlines()
        start = operation.anchor.start_line - 1
        end = operation.anchor.end_line - 1

        if start < 0 or end < start or end >= len(lines):
            msg = (
                f"Invalid replace_range for {operation.file}: "
                f"{operation.anchor.start_line}-{operation.anchor.end_line}"
            )
            raise RuntimeError(msg)

        replacement = operation.content.splitlines()
        updated = [*lines[:start], *replacement, *lines[end + 1 :]]
        target.write_text("\n".join(updated), encoding="utf-8")

    def _apply_insert_after_symbol(self, base_path: Path, operation: InsertAfterSymbolOp) -> None:
        target = self._resolve_inside(base_path, operation.file)
        lines = target.read_text(encoding="utf-8").splitlines()

        index = -1
        for idx, line in enumerate(lines):
            if operation.anchor.symbol in line:
                index = idx
                break

        if index == -1:
            msg = f"Symbol '{operation.anchor.symbol}' not found in {operation.file}"
            raise RuntimeError(msg)

        insertion = operation.content.splitlines()
        updated = [*lines[: index + 1], *insertion, *lines[index + 1 :]]
        target.write_text("\n".join(updated), encoding="utf-8")

    def _apply_create_file(self, base_path: Path, operation: CreateFileOp) -> None:
        target = self._resolve_inside(base_path, operation.file)
        if target.exists():
            msg = f"File already exists: {operation.file}"
            raise RuntimeError(msg)

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(operation.content, encoding="utf-8")

    def _apply_delete_file(self, base_path: Path, operation: DeleteFileOp) -> None:
        target = self._resolve_inside(base_path, operation.file)
        if not target.exists():
            msg = f"Cannot delete missing path: {operation.file}"
            raise RuntimeError(msg)

        if target.is_dir():
            shutil.rmtree(target)
            return

        target.unlink()

    def _resolve_inside(self, base_path: Path, relative_path: str) -> Path:
        candidate = (base_path / relative_path).resolve()
        try:
            candidate.relative_to(base_path)
        except ValueError as exc:
            msg = f"Path escapes workspace: {relative_path}"
            raise RuntimeError(msg) from exc
        return candidate
