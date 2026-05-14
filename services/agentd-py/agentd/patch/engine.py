from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Literal

try:
    import libcst as cst
    from libcst.metadata import PositionProvider
except Exception:  # pragma: no cover - optional dependency at import time
    cst = None  # type: ignore[assignment]
    PositionProvider = None  # type: ignore[assignment]

if TYPE_CHECKING:
    import libcst
    from libcst.metadata import PositionProvider

from agentd.domain.models import (
    ApplyDiffOpV2,
    CreateFileOp,
    CreateFileOpV2,
    DeleteFileOp,
    DeleteFileOpV2,
    InsertAfterSymbolOp,
    InsertAfterNodeOpV2,
    NodeSelector,
    PatchCandidateV2,
    PatchFailureCode,
    PatchDocument,
    PatchPreflightIssue,
    PatchPreflightReport,
    ReplaceRangeOp,
    ReplaceNodeOpV2,
    SearchReplaceOpV2,
)
from agentd.patch.parser import AiderDiffParser
from agentd.patch.utils import hunk_to_before_after, RelativeIndenter
from agentd.patch.policy import ForbiddenPathPolicy, PatchPolicyViolation


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PatchResult:
    touched_files: list[str]


class SelectorAmbiguousError(ValueError):
    pass


class ParserUnavailableError(RuntimeError):
    pass


def _python_syntax_check(source: str, *, label: str) -> None:
    """Fast stdlib compile() syntax pre-check before expensive libcst parsing.

    Raises RuntimeError with a clear line:col message if ``source`` is not
    valid Python syntax.  This runs *before* libcst so truncated LLM content
    is rejected immediately with a human-readable error.
    """
    import textwrap
    try:
        compile(textwrap.dedent(source), label, "exec")
    except SyntaxError as exc:
        lineno = exc.lineno or "?"
        offset = exc.offset or "?"
        msg = (
            f"Python syntax error in {label} at line {lineno} col {offset}: "
            f"{exc.msg}"
        )
        raise RuntimeError(msg) from exc


@dataclass(frozen=True)
class PythonDeclMatch:
    kind: Literal["class", "function", "import"]
    name: str
    start_line: int
    start_col: int
    end_line: int
    end_col: int


class PatchEngine:
    def __init__(self, policy: ForbiddenPathPolicy | None = None) -> None:
        self._policy = policy or ForbiddenPathPolicy()
        self._ts_parser = None
        self._rs_parser = None
        self._tree_sitter_ready = False

    async def preflight_patch_document(
        self,
        base_dir: str | Path,
        patch: PatchDocument,
        *,
        allowed_files: set[str] | None = None,
    ) -> PatchPreflightReport:
        base_path = Path(base_dir).resolve()
        if not base_path.exists() or not base_path.is_dir():
            msg = f"Patch base path is not a directory: {base_path}"
            return PatchPreflightReport(
                success=False,
                issues=[
                    PatchPreflightIssue(
                        code=PatchFailureCode.FILE_MISSING,
                        message=msg,
                    )
                ],
            )

        try:
            self._policy.validate_paths(op.file for op in patch.patch_ops)
        except PatchPolicyViolation as exc:
            return PatchPreflightReport(
                success=False,
                issues=[
                    PatchPreflightIssue(
                        code=PatchFailureCode.POLICY_VIOLATION,
                        message=str(exc),
                    )
                ],
            )

        issues: list[PatchPreflightIssue] = []
        simulated_files: dict[str, list[str] | None] = {}
        original_files: dict[str, list[str] | None] = {}
        mutated_files: set[str] = set()
        for index, operation in enumerate(patch.patch_ops):
            if allowed_files is not None and operation.file not in allowed_files:
                issues.append(
                    PatchPreflightIssue(
                        op_index=index,
                        code=PatchFailureCode.SCOPE_VIOLATION,
                        file=operation.file,
                        message=f"Patch op targets file outside current step scope: {operation.file}",
                    )
                )
                continue

            try:
                target = self._resolve_inside(base_path, operation.file)
            except RuntimeError as exc:
                issues.append(
                    PatchPreflightIssue(
                        op_index=index,
                        code=PatchFailureCode.PATH_ESCAPE,
                        file=operation.file,
                        message=str(exc),
                    )
                )
                continue

            if operation.file not in simulated_files:
                if target.exists():
                    try:
                        loaded = target.read_text(encoding="utf-8").splitlines()
                    except OSError as exc:
                        issues.append(
                            PatchPreflightIssue(
                                op_index=index,
                                code=PatchFailureCode.APPLY_ERROR,
                                file=operation.file,
                                message=f"Unable to read file for preflight simulation: {exc}",
                            )
                        )
                        continue
                    simulated_files[operation.file] = loaded
                    original_files[operation.file] = [*loaded]
                else:
                    simulated_files[operation.file] = None
                    original_files[operation.file] = None

            current_lines = simulated_files[operation.file]

            if isinstance(operation, CreateFileOp):
                if current_lines is not None:
                    issues.append(
                        PatchPreflightIssue(
                            op_index=index,
                            code=PatchFailureCode.FILE_EXISTS,
                            file=operation.file,
                            message=f"File already exists: {operation.file}",
                        )
                    )
                    continue
                simulated_files[operation.file] = operation.content.splitlines()
                mutated_files.add(operation.file)
                continue

            if current_lines is None:
                issues.append(
                    PatchPreflightIssue(
                        op_index=index,
                        code=PatchFailureCode.FILE_MISSING,
                        file=operation.file,
                        message=f"File is missing for op '{operation.op}': {operation.file}",
                    )
                )
                continue

            if isinstance(operation, DeleteFileOp):
                simulated_files[operation.file] = None
                mutated_files.add(operation.file)
                continue

            if isinstance(operation, ReplaceRangeOp):
                start = operation.anchor.start_line - 1
                end = operation.anchor.end_line - 1

                # Robust capping (same as apply fix)
                if 0 <= start < len(current_lines):
                    if end >= len(current_lines):
                        end = len(current_lines) - 1

                if start < 0 or end < start or end >= len(current_lines):
                    issues.append(
                        PatchPreflightIssue(
                            op_index=index,
                            code=PatchFailureCode.RANGE_INVALID,
                            file=operation.file,
                            message=(
                                f"Invalid replace_range {operation.anchor.start_line}-"
                                f"{operation.anchor.end_line}; file has {len(current_lines)} lines"
                            ),
                        )
                    )
                    continue
                replacement = operation.content.splitlines()
                simulated_files[operation.file] = [
                    *current_lines[:start],
                    *replacement,
                    *current_lines[end + 1 :],
                ]
                mutated_files.add(operation.file)
                continue

            if isinstance(operation, InsertAfterSymbolOp):
                matches = self._find_symbol_indices(current_lines, operation.anchor.symbol)
                if not matches:
                    code = PatchFailureCode.ANCHOR_MISSING
                    message = (
                        f"Symbol '{operation.anchor.symbol}' not found in "
                        f"{operation.file}"
                    )
                    original_lines = original_files.get(operation.file)
                    if (
                        operation.file in mutated_files
                        and original_lines is not None
                        and self._find_symbol_indices(original_lines, operation.anchor.symbol)
                    ):
                        code = PatchFailureCode.ORDER_CONFLICT
                        message = (
                            f"Anchor '{operation.anchor.symbol}' was invalidated by an earlier "
                            f"operation in {operation.file}"
                        )
                    issues.append(
                        PatchPreflightIssue(
                            op_index=index,
                            code=code,
                            file=operation.file,
                            message=message,
                        )
                    )
                    continue

                if len(matches) > 1:
                    issues.append(
                        PatchPreflightIssue(
                            op_index=index,
                            code=PatchFailureCode.ANCHOR_AMBIGUOUS,
                            file=operation.file,
                            message=(
                                f"Symbol '{operation.anchor.symbol}' is ambiguous in "
                                f"{operation.file}; matched {len(matches)} lines"
                            ),
                        )
                    )
                    continue

                if target.suffix == ".py":
                    matched_line = current_lines[matches[0]].lstrip()
                    if matched_line.startswith("def ") or matched_line.startswith("class "):
                        issues.append(
                            PatchPreflightIssue(
                                op_index=index,
                                code=PatchFailureCode.PYTHON_UNSAFE_INSERT,
                                file=operation.file,
                                message=(
                                    "insert_after_symbol on Python def/class signatures is "
                                    f"unsafe: '{operation.anchor.symbol}' in {operation.file}"
                                ),
                            )
                        )
                        continue

                insertion = operation.content.splitlines()
                symbol_index = matches[0]
                simulated_files[operation.file] = [
                    *current_lines[: symbol_index + 1],
                    *insertion,
                    *current_lines[symbol_index + 1 :],
                ]
                mutated_files.add(operation.file)

        return PatchPreflightReport(success=not issues, issues=issues)

    async def apply_patch_document(
        self,
        base_dir: str | Path,
        patch: PatchDocument,
        *,
        allowed_files: set[str] | None = None,
    ) -> PatchResult:
        base_path = Path(base_dir).resolve()
        report = await self.preflight_patch_document(
            base_path,
            patch,
            allowed_files=allowed_files,
        )
        if not report.success:
            if report.issues and report.issues[0].code == PatchFailureCode.POLICY_VIOLATION:
                raise PatchPolicyViolation(report.issues[0].message)
            details = "; ".join(
                issue.message for issue in report.issues[:3]
            )
            raise RuntimeError(f"Patch preflight failed: {details}")

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

        # FIX: automatically cap end_line if start_line is valid
        if 0 <= start < len(lines):
            if end >= len(lines):
                logger.warning(
                    f"Capping end_line {operation.anchor.end_line} to file length {len(lines)} "
                    f"for {operation.file}"
                )
                end = len(lines) - 1

        if start < 0 or end < start or end >= len(lines):
            msg = (
                f"Invalid replace_range for {operation.file}: "
                f"{operation.anchor.start_line}-{operation.anchor.end_line} "
                f"(file has {len(lines)} lines)"
            )
            raise RuntimeError(msg)

        replacement = operation.content.splitlines()
        updated_lines = [*lines[:start], *replacement, *lines[end + 1 :]]
        updated_text = "\n".join(updated_lines)
        if operation.file.endswith(".py"):
            _python_syntax_check(updated_text, label=operation.file)
        target.write_text(updated_text, encoding="utf-8")

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

    def _apply_create_file(self, base_path: Path, operation: CreateFileOp | CreateFileOpV2) -> None:
        target = self._resolve_inside(base_path, operation.file)
        if target.exists():
            msg = f"File already exists: {operation.file}"
            raise RuntimeError(msg)

        target.parent.mkdir(parents=True, exist_ok=True)
        content = operation.content
        if operation.file.endswith(".py"):
            _python_syntax_check(content, label=operation.file)
        target.write_text(content, encoding="utf-8")

    def _apply_delete_file(self, base_path: Path, operation: DeleteFileOp | DeleteFileOpV2) -> None:
        target = self._resolve_inside(base_path, operation.file)
        if not target.exists():
            msg = f"Cannot delete missing path: {operation.file}"
            raise RuntimeError(msg)

        if target.is_dir():
            shutil.rmtree(target)
            return


    def _apply_search_replace(self, base_path: Path, operation: SearchReplaceOpV2) -> None:
        """Apply search/replace operation (Fast Apply).
        
        O(N) text search and replace - very fast for large files.
        """
        target = self._resolve_inside(base_path, operation.file)
        
        if not target.exists():
            msg = f"File not found for search/replace: {operation.file}"
            raise RuntimeError(msg)
        
        original_content = target.read_text(encoding="utf-8")
        
        # Fast Apply: exact text search
        if operation.search not in original_content:
            msg = f"Search text not found in {operation.file}. File may have changed since patch was generated."
            raise RuntimeError(msg)
        
        # Count occurrences
        occurrences = original_content.count(operation.search)
        if occurrences > 1:
            msg = f"Search text appears {occurrences} times in {operation.file}. Search text must be unique for safe replacement."
            raise RuntimeError(msg)
        
        # Apply replacement
        new_content = original_content.replace(operation.search, operation.replace, 1)
        if operation.file.endswith(".py"):
            _python_syntax_check(new_content, label=operation.file)
        target.write_text(new_content, encoding="utf-8")

    def _apply_diff(self, base_path: Path, operation: ApplyDiffOpV2) -> None:
        """Apply unified diff to file using robust Aider-style strategies."""
        target = self._resolve_inside(base_path, operation.file)
        
        if not target.exists():
            msg = f"File not found for diff application: {operation.file}"
            raise RuntimeError(msg)
        
        content = target.read_text(encoding="utf-8")
        parser = AiderDiffParser()
        
        # Parse Codex-style format if present (handling legacy model behavior)
        diff_text = self._parse_codex_diff(operation.diff)
        
        # Find all hunks for this file
        # We wrap in ```diff block to ensure the parser treats it as one
        all_edits = parser.find_diffs(f"```diff\n--- {operation.file}\n+++ {operation.file}\n{diff_text}\n```")
        
        if not all_edits:
            msg = f"Could not parse any hunks from diff for {operation.file}"
            raise RuntimeError(msg)

        original_content = content
        errors = []
        
        for _, hunk in all_edits:
            # Normalize hunk before application
            hunk = parser.normalize_hunk(hunk)
            before_text, after_text = hunk_to_before_after(hunk)
            
            # Apply hunk with fallbacks
            new_content = self.apply_hunk_to_text(content, hunk)
            
            if new_content is not None:
                content = new_content
            else:
                num_lines = len(before_text.splitlines())
                errors.append(
                    f"UnifiedDiffNoMatch: hunk failed to apply to {operation.file}! "
                    f"File does not contain these {num_lines} exact lines in a row:\n"
                    f"```\n{before_text}```"
                )
                # For now, we continue to try other hunks (Aider style)
                # though usually one failure means we should probably stop or report partial success
        
        if errors:
            msg = "\n\n".join(errors)
            if content != original_content:
                msg += "\n\nNote: some hunks did apply successfully."
            raise RuntimeError(msg)

        if operation.file.endswith(".py"):
            _python_syntax_check(content, label=operation.file)
        
        target.write_text(content, encoding="utf-8")

    def apply_hunk_to_text(self, content: str, hunk: List[str]) -> Optional[str]:
        """Try multiple strategies to apply a hunk, from strict to flexible."""
        before_text, after_text = hunk_to_before_after(hunk)
        
        # Strategy 1: Direct Exact Match
        if before_text in content:
            return content.replace(before_text, after_text, 1)

        # Strategy 1b: tolerate trailing newline mismatches at EOF.
        trimmed_before = before_text.rstrip("\n")
        if trimmed_before and trimmed_before != before_text and trimmed_before in content:
            trimmed_after = after_text.rstrip("\n")
            return content.replace(trimmed_before, trimmed_after, 1)

        # Strategy 2: Indentation-Aware Match (using RelativeIndenter)
        try:
            ri = RelativeIndenter([before_text, after_text, content])
            rel_before = ri.make_relative(before_text)
            rel_content = ri.make_relative(content)
            
            if rel_before in rel_content:
                rel_after = ri.make_relative(after_text)
                rel_new_content = rel_content.replace(rel_before, rel_after, 1)
                return ri.make_absolute(rel_new_content)
        except Exception as e:
            logger.debug(f"Relative indentation matching failed: {e}")

        # Strategy 3: Context Shrinking (Iterative reduction of context lines)
        res = self._apply_partial_hunk(content, hunk)
        if res:
            return res

        return None

    def _apply_partial_hunk(self, content: str, hunk: List[str]) -> Optional[str]:
        """Try applying the hunk with reduced context lines (dropping from ends)."""
        # Aider splits hunk into preceding context, changes, following context
        # We can implement a simplified version that drops context lines iteratively
        
        ops = [line[0] for line in hunk]
        
        # Find the core changes (contiguous runs of - and +)
        first_change = next((i for i, op in enumerate(ops) if op in "-+"), 0)
        last_change = next((i for i in range(len(ops)-1, -1, -1) if ops[i] in "-+"), len(ops)-1)
        
        preceding_context = hunk[:first_change]
        changes = hunk[first_change : last_change + 1]
        following_context = hunk[last_change + 1 :]
        removed_lines_count = sum(1 for op in ops if op == "-")
        
        len_prec = len(preceding_context)
        len_foll = len(following_context)
        use_all = len_prec + len_foll

        def _context_to_text(lines: List[str]) -> str:
            normalized: list[str] = []
            for line in lines:
                if not line:
                    normalized.append("")
                    continue
                if line[0] in {" ", "+", "-"}:
                    normalized.append(line[1:])
                else:
                    normalized.append(line)
            return "\n".join(normalized)

        # Iterate through different context sizes, from largest to smallest
        for drop in range(use_all + 1):
            use = use_all - drop
            # Try all combinations of preceding and following context that sum to 'use'
            for use_prec in range(len_prec, -1, -1):
                if use_prec > use:
                    continue
                use_foll = use - use_prec
                if use_foll > len_foll:
                    continue

                # Safety guard: for insert-only hunks with context on both sides, keep
                # both sides unless the dropped side still exists in file content.
                if removed_lines_count == 0 and len_prec > 0 and len_foll > 0:
                    if use_prec == 0:
                        dropped_prec = _context_to_text(preceding_context)
                        if dropped_prec not in content and dropped_prec.rstrip("\n") not in content:
                            continue
                    if use_foll == 0:
                        dropped_foll = _context_to_text(following_context)
                        if dropped_foll not in content and dropped_foll.rstrip("\n") not in content:
                            continue

                this_prec = preceding_context[-use_prec:] if use_prec > 0 else []
                this_foll = following_context[:use_foll] if use_foll > 0 else []
                
                partial_hunk = this_prec + changes + this_foll
                p_before, p_after = hunk_to_before_after(partial_hunk)
                
                if p_before and p_before in content:
                    return content.replace(p_before, p_after, 1)
                    
        return None

    def _parse_codex_diff(self, diff_text: str) -> str:
        """Convert Codex-style diff to unified diff format.
        
        Codex format:
        *** Begin Patch
        @@ context @@
        -old line
        +new line
        *** End Patch
        
        Converts to standard unified diff for processing.
        """
        if "*** Begin Patch" in diff_text and "*** End Patch" in diff_text:
            # Extract content between markers
            start_idx = diff_text.index("*** Begin Patch") + len("*** Begin Patch")
            end_idx = diff_text.index("*** End Patch")
            return diff_text[start_idx:end_idx].strip()
        
        return diff_text  # Already in unified format

    def _resolve_inside(self, base_path: Path, relative_path: str) -> Path:
        candidate = (base_path / relative_path).resolve()
        try:
            candidate.relative_to(base_path)
        except ValueError as exc:
            msg = f"Path escapes workspace: {relative_path}"
            raise RuntimeError(msg) from exc
        return candidate

    def _find_symbol_indices(self, lines: Iterable[str], symbol: str) -> list[int]:
        indices: list[int] = []
        for idx, line in enumerate(lines):
            if symbol in line:
                indices.append(idx)
        return indices

    async def preflight_patch_candidate(
        self,
        base_dir: str | Path,
        candidate: PatchCandidateV2,
        *,
        allowed_files: set[str] | None = None,
    ) -> PatchPreflightReport:
        base_path = Path(base_dir).resolve()
        if not base_path.exists() or not base_path.is_dir():
            msg = f"Patch base path is not a directory: {base_path}"
            return PatchPreflightReport(
                success=False,
                issues=[PatchPreflightIssue(code=PatchFailureCode.FILE_MISSING, message=msg)],
            )

        try:
            self._policy.validate_paths(op.file for op in candidate.patch_ops)
        except PatchPolicyViolation as exc:
            return PatchPreflightReport(
                success=False,
                issues=[PatchPreflightIssue(code=PatchFailureCode.POLICY_VIOLATION, message=str(exc))],
            )

        issues: list[PatchPreflightIssue] = []
        simulated_sources: dict[str, str | None] = {}
        original_sources: dict[str, str | None] = {}
        mutated_files: set[str] = set()
        for index, operation in enumerate(candidate.patch_ops):
            if allowed_files is not None and operation.file not in allowed_files:
                issues.append(
                    PatchPreflightIssue(
                        op_index=index,
                        code=PatchFailureCode.SCOPE_VIOLATION,
                        file=operation.file,
                        message=f"Patch op targets file outside current step scope: {operation.file}",
                    )
                )
                continue

            try:
                target = self._resolve_inside(base_path, operation.file)
            except RuntimeError as exc:
                issues.append(
                    PatchPreflightIssue(
                        op_index=index,
                        code=PatchFailureCode.PATH_ESCAPE,
                        file=operation.file,
                        message=str(exc),
                    )
                )
                continue

            if operation.file not in simulated_sources:
                if target.exists():
                    try:
                        source = target.read_text(encoding="utf-8")
                    except OSError as exc:
                        issues.append(
                            PatchPreflightIssue(
                                op_index=index,
                                code=PatchFailureCode.APPLY_ERROR,
                                file=operation.file,
                                message=f"Unable to read file for preflight simulation: {exc}",
                            )
                        )
                        continue
                    simulated_sources[operation.file] = source
                    original_sources[operation.file] = source
                else:
                    simulated_sources[operation.file] = None
                    original_sources[operation.file] = None

            current_source = simulated_sources[operation.file]

            if isinstance(operation, CreateFileOpV2):
                if current_source is not None:
                    issues.append(
                        PatchPreflightIssue(
                            op_index=index,
                            code=PatchFailureCode.FILE_EXISTS,
                            file=operation.file,
                            message=f"File already exists: {operation.file}",
                        )
                    )
                    continue
                simulated_sources[operation.file] = operation.content
                mutated_files.add(operation.file)
                continue

            if current_source is None:
                issues.append(
                    PatchPreflightIssue(
                        op_index=index,
                        code=PatchFailureCode.FILE_MISSING,
                        file=operation.file,
                        message=f"File is missing for op '{operation.op}': {operation.file}",
                    )
                )
                continue

            if isinstance(operation, DeleteFileOpV2):
                simulated_sources[operation.file] = None
                mutated_files.add(operation.file)
                continue

            if isinstance(operation, ReplaceRangeOp):
                if current_source is None:
                    issues.append(
                        PatchPreflightIssue(
                            op_index=index,
                            code=PatchFailureCode.FILE_MISSING,
                            file=operation.file,
                            message=f"File is missing for op 'replace_range': {operation.file}",
                        )
                    )
                    continue
                
                lines = current_source.splitlines()
                start = operation.anchor.start_line - 1
                end = operation.anchor.end_line - 1
                
                # Robust capping (same as apply fix)
                if 0 <= start < len(lines):
                    if end >= len(lines):
                        end = len(lines) - 1
                
                if start < 0 or end < start or end >= len(lines):
                    issues.append(
                        PatchPreflightIssue(
                            op_index=index,
                            code=PatchFailureCode.RANGE_INVALID,
                            file=operation.file,
                            message=(
                                f"Invalid replace_range {operation.anchor.start_line}-"
                                f"{operation.anchor.end_line}; file has {len(lines)} lines"
                            ),
                        )
                    )
                    continue
                
                replacement = operation.content.splitlines()
                new_lines = [*lines[:start], *replacement, *lines[end + 1 :]]
                simulated_sources[operation.file] = "\n".join(new_lines)
                mutated_files.add(operation.file)
                continue

            try:
                if isinstance(operation, ReplaceNodeOpV2):
                    span = self._resolve_unique_selector_span(
                        operation.language,
                        current_source,
                        operation.selector,
                        operation.file,
                    )
                    if span is None:
                        issues.append(
                            self._missing_or_conflict_issue(
                                index=index,
                                file=operation.file,
                                selector=operation.selector,
                                original_source=original_sources.get(operation.file),
                                mutated=operation.file in mutated_files,
                            )
                        )
                        continue
                    start, end = span
                    simulated_sources[operation.file] = current_source[:start] + operation.content + current_source[end:]
                    mutated_files.add(operation.file)
                    continue

                if isinstance(operation, InsertAfterNodeOpV2):
                    span = self._resolve_unique_selector_span(
                        operation.language,
                        current_source,
                        operation.selector,
                        operation.file,
                    )
                    if span is None:
                        issues.append(
                            self._missing_or_conflict_issue(
                                index=index,
                                file=operation.file,
                                selector=operation.selector,
                                original_source=original_sources.get(operation.file),
                                mutated=operation.file in mutated_files,
                            )
                        )
                        continue
                    _start, end = span
                    insertion = operation.content
                    if insertion and not insertion.endswith("\n"):
                        insertion = insertion + "\n"
                    simulated_sources[operation.file] = current_source[:end] + insertion + current_source[end:]
                    mutated_files.add(operation.file)
                    continue

                # Handle SearchReplaceOpV2 (Fast Apply)
                if isinstance(operation, SearchReplaceOpV2):
                    if operation.search not in current_source:
                        code = PatchFailureCode.ANCHOR_MISSING
                        if operation.file in mutated_files:
                            code = PatchFailureCode.ORDER_CONFLICT
                        issues.append(
                            PatchPreflightIssue(
                                op_index=index,
                                code=code,
                                file=operation.file,
                                message=f"Search text not found in file",
                            )
                        )
                        continue

                    occurrences = current_source.count(operation.search)
                    if occurrences > 1:
                        issues.append(
                            PatchPreflightIssue(
                                op_index=index,
                                code=PatchFailureCode.ANCHOR_AMBIGUOUS,
                                file=operation.file,
                                message=f"Search text appears {occurrences} times (must be unique)",
                            )
                        )
                        continue

                    # Simulate replacement
                    simulated_sources[operation.file] = current_source.replace(operation.search, operation.replace, 1)
                    mutated_files.add(operation.file)
                    continue

                # Handle ApplyDiffOpV2 (Unified Diff) - Aider Style
                if isinstance(operation, ApplyDiffOpV2):
                    parser = AiderDiffParser()
                    diff_text = self._parse_codex_diff(operation.diff)
                    # Find all hunks for this file
                    all_edits = parser.find_diffs(
                        f"```diff\n--- {operation.file}\n+++ {operation.file}\n{diff_text}\n```"
                    )
                    
                    if not all_edits:
                        issues.append(
                            PatchPreflightIssue(
                                op_index=index,
                                code=PatchFailureCode.APPLY_ERROR,
                                file=operation.file,
                                message="Could not parse any hunks from diff content",
                            )
                        )
                        continue

                    current_simulated = current_source
                    for _, hunk in all_edits:
                        # Normalize hunk before preflight
                        hunk = parser.normalize_hunk(hunk)
                        
                        # Simulate application with full fallbacks (Aider style)
                        updated = self.apply_hunk_to_text(current_simulated, hunk)
                        if updated is not None:
                            current_simulated = updated
                            continue

                        # All strategies failed
                        code = PatchFailureCode.ANCHOR_MISSING
                        if operation.file in mutated_files:
                            code = PatchFailureCode.ORDER_CONFLICT
                        
                        issues.append(
                            PatchPreflightIssue(
                                op_index=index,
                                code=code,
                                file=operation.file,
                                message=(
                                    f"Hunk context mismatch for {operation.file}. "
                                    "Preflight check failed even with indentation/context fallbacks."
                                ),
                            )
                        )
                        break
                    
                    if not issues or issues[-1].op_index != index:
                        simulated_sources[operation.file] = current_simulated
                        mutated_files.add(operation.file)
                    continue

            except SelectorAmbiguousError as exc:
                issues.append(
                    PatchPreflightIssue(
                        op_index=index,
                        code=PatchFailureCode.ANCHOR_AMBIGUOUS,
                        file=operation.file,
                        message=str(exc),
                    )
                )
                continue
            except ParserUnavailableError as exc:
                issues.append(
                    PatchPreflightIssue(
                        op_index=index,
                        code=PatchFailureCode.PARSER_UNAVAILABLE,
                        file=operation.file,
                        message=str(exc),
                    )
                )
                continue
            except RuntimeError as exc:
                issues.append(
                    PatchPreflightIssue(
                        op_index=index,
                        code=PatchFailureCode.APPLY_ERROR,
                        file=operation.file,
                        message=str(exc),
                    )
                )
                continue

        return PatchPreflightReport(success=not issues, issues=issues)

    async def apply_patch_candidate(
        self,
        base_dir: str | Path,
        candidate: PatchCandidateV2,
        *,
        allowed_files: set[str] | None = None,
        on_patch_event: Callable[[dict], None] | None = None,
        incremental_validator: Callable[[list[str]], Any] | None = None,
    ) -> PatchResult:
        base_path = Path(base_dir).resolve()
        report = await self.preflight_patch_candidate(
            base_path,
            candidate,
            allowed_files=allowed_files,
        )
        if not report.success:
            if report.issues and report.issues[0].code == PatchFailureCode.POLICY_VIOLATION:
                raise PatchPolicyViolation(report.issues[0].message)
            details = "; ".join(issue.message for issue in report.issues[:3])
            raise RuntimeError(f"Patch preflight failed: {details}")

        touched: set[str] = set()
        incremental_errors: list[str] = []

        for operation in candidate.patch_ops:
            is_write = not isinstance(operation, DeleteFileOpV2)
            try:
                if isinstance(operation, ReplaceNodeOpV2):
                    self._apply_replace_node(base_path, operation)
                elif isinstance(operation, InsertAfterNodeOpV2):
                    self._apply_insert_after_node(base_path, operation)
                elif isinstance(operation, SearchReplaceOpV2):
                    self._apply_search_replace(base_path, operation)
                elif isinstance(operation, ApplyDiffOpV2):
                    self._apply_diff(base_path, operation)
                elif isinstance(operation, ReplaceRangeOp):
                    self._apply_replace_range(base_path, operation)
                elif isinstance(operation, CreateFileOpV2):
                    self._apply_create_file(base_path, operation)
                elif isinstance(operation, DeleteFileOpV2):
                    self._apply_delete_file(base_path, operation)
                else:
                    msg = f"Unsupported patch operation type: {type(operation).__name__}"
                    raise RuntimeError(msg)
                if on_patch_event:
                    on_patch_event({"type": "operation_success", "payload": {"op_type": operation.op, "path": operation.file}})
                touched.add(operation.file)
                if is_write and incremental_validator is not None:
                    iv_result = await incremental_validator([operation.file])
                    for diag in iv_result.diagnostics:
                        if diag.level == "error":
                            incremental_errors.append(f"{operation.file}: {diag.message}")
            except Exception as exc:
                if on_patch_event:
                    on_patch_event({"type": "operation_error", "payload": {"op_type": operation.op, "path": operation.file, "error": str(exc)}})
                incremental_errors.append(f"{operation.file}: {exc}")

        if incremental_errors:
            raise RuntimeError(
                "Incremental syntax validation failed:\n" + "\n".join(incremental_errors)
            )

        return PatchResult(touched_files=sorted(touched))

    def _missing_or_conflict_issue(
        self,
        *,
        index: int,
        file: str,
        selector: NodeSelector,
        original_source: str | None,
        mutated: bool,
    ) -> PatchPreflightIssue:
        code = PatchFailureCode.ANCHOR_MISSING
        message = f"Selector '{selector.value}' not found in {file}"
        if mutated and original_source:
            try:
                original_spans = self._find_symbol_offsets(original_source, selector.value, selector.match)
            except Exception:
                original_spans = []
            if original_spans:
                code = PatchFailureCode.ORDER_CONFLICT
                message = f"Selector '{selector.value}' was invalidated by earlier operation in {file}"

        return PatchPreflightIssue(
            op_index=index,
            code=code,
            file=file,
            message=message,
        )

    def _apply_replace_node(self, base_path: Path, operation: ReplaceNodeOpV2) -> None:
        target = self._resolve_inside(base_path, operation.file)
        source = target.read_text(encoding="utf-8")
        if operation.language == "python":
            # Pre-validate the replacement snippet syntax before any CST work
            _python_syntax_check(operation.content, label=f"replacement snippet for {operation.file}")
            updated = self._apply_python_replace_node(
                source,
                selector=operation.selector,
                replacement_source=operation.content,
                file_path=operation.file,
            )
            # Post-merge: validate the full resulting file to catch splice-induced errors
            _python_syntax_check(updated, label=operation.file)
        else:
            span = self._resolve_unique_selector_span(
                operation.language,
                source,
                operation.selector,
                operation.file,
            )
            if span is None:
                msg = f"Selector '{operation.selector.value}' not found in {operation.file}"
                raise RuntimeError(msg)
            start, end = span
            updated = source[:start] + operation.content + source[end:]
        target.write_text(updated, encoding="utf-8")

    def _apply_insert_after_node(self, base_path: Path, operation: InsertAfterNodeOpV2) -> None:
        target = self._resolve_inside(base_path, operation.file)
        source = target.read_text(encoding="utf-8")
        if operation.language == "python":
            # Pre-validate the insertion snippet syntax
            _python_syntax_check(operation.content, label=f"insertion snippet for {operation.file}")
            updated = self._apply_python_insert_after_node(
                source,
                selector=operation.selector,
                insertion_source=operation.content,
                file_path=operation.file,
            )
            # Post-merge check
            _python_syntax_check(updated, label=operation.file)
        else:
            span = self._resolve_unique_selector_span(
                operation.language,
                source,
                operation.selector,
                operation.file,
            )
            if span is None:
                msg = f"Selector '{operation.selector.value}' not found in {operation.file}"
                raise RuntimeError(msg)
            _start, end = span
            insertion = operation.content
            if insertion and not insertion.endswith("\n"):
                insertion = insertion + "\n"
            updated = source[:end] + insertion + source[end:]
        target.write_text(updated, encoding="utf-8")

    def _apply_python_replace_node(
        self,
        source: str,
        *,
        selector: NodeSelector,
        replacement_source: str,
        file_path: str,
    ) -> str:
        if cst is None or PositionProvider is None:
            msg = "libcst is required for Python AST patching"
            raise ParserUnavailableError(msg)
        module = cst.parse_module(source)
        matches = self._python_declaration_matches(
            source,
            symbol=selector.value,
            match=selector.match,
        )
        if not matches:
            msg = f"Selector '{selector.value}' not found in {file_path}"
            raise RuntimeError(msg)
        if len(matches) > 1:
            msg = f"Selector '{selector.value}' is ambiguous in {file_path}; matched {len(matches)} nodes"
            raise SelectorAmbiguousError(msg)
        target = matches[0]

        replacement_statements = self._python_parse_statements(
            replacement_source,
            file_path=file_path,
        )
        if len(replacement_statements) != 1:
            msg = f"replace_node for Python requires exactly one declaration statement in {file_path}"
            raise RuntimeError(msg)
        replacement_stmt = replacement_statements[0]
        if not isinstance(replacement_stmt, (cst.ClassDef, cst.FunctionDef)):
            msg = f"replace_node for Python requires class/def replacement in {file_path}"
            raise RuntimeError(msg)

        # At this point, PositionProvider is guaranteed to be non-None due to check at line 966
        # Import the actual type for use in the transformer
        from libcst.metadata import PositionProvider as _PositionProvider
        import libcst as _cst
        
        class _ReplaceTransformer(_cst.CSTTransformer):
            METADATA_DEPENDENCIES = (_PositionProvider,)

            def _is_target(self, node: _cst.CSTNode, kind: str) -> bool:
                from libcst.metadata import CodeRange
                position: CodeRange = self.get_metadata(_PositionProvider, node)  # type: ignore[assignment]
                return (
                    target.kind == kind
                    and target.start_line == position.start.line
                    and target.start_col == position.start.column
                    and target.end_line == position.end.line
                    and target.end_col == position.end.column
                )

            def leave_ClassDef(
                self,
                original_node: _cst.ClassDef,  # noqa: N803
                updated_node: _cst.ClassDef,  # noqa: N803
            ) -> _cst.BaseStatement:
                if self._is_target(original_node, "class"):
                    return replacement_stmt
                return updated_node

            def leave_FunctionDef(
                self,
                original_node: _cst.FunctionDef,  # noqa: N803
                updated_node: _cst.FunctionDef,  # noqa: N803
            ) -> _cst.BaseStatement:
                if self._is_target(original_node, "function"):
                    return replacement_stmt
                return updated_node

            def leave_SimpleStatementLine(
                self,
                original_node: _cst.SimpleStatementLine,  # noqa: N803
                updated_node: _cst.SimpleStatementLine,  # noqa: N803
            ) -> _cst.BaseStatement:
                if self._is_target(original_node, "import"):
                    return replacement_stmt
                return updated_node

        wrapper = cst.MetadataWrapper(module)
        updated = wrapper.visit(_ReplaceTransformer())
        return updated.code

    def _apply_python_insert_after_node(
        self,
        source: str,
        *,
        selector: NodeSelector,
        insertion_source: str,
        file_path: str,
    ) -> str:
        if cst is None or PositionProvider is None:
            msg = "libcst is required for Python AST patching"
            raise ParserUnavailableError(msg)
        module = cst.parse_module(source)
        matches = self._python_declaration_matches(
            source,
            symbol=selector.value,
            match=selector.match,
        )
        if not matches:
            msg = f"Selector '{selector.value}' not found in {file_path}"
            raise RuntimeError(msg)
        if len(matches) > 1:
            msg = f"Selector '{selector.value}' is ambiguous in {file_path}; matched {len(matches)} nodes"
            raise SelectorAmbiguousError(msg)
        target = matches[0]
        insertion_stmts = self._python_parse_statements(insertion_source, file_path=file_path)

        # Import the actual type for use in the transformer
        from libcst.metadata import PositionProvider as _PositionProvider
        import libcst as _cst

        class _InsertAfterTransformer(_cst.CSTTransformer):
            METADATA_DEPENDENCIES = (_PositionProvider,)

            def _is_target(self, node: _cst.CSTNode, kind: str) -> bool:
                from libcst.metadata import CodeRange
                position: CodeRange = self.get_metadata(_PositionProvider, node)  # type: ignore[assignment]
                return (
                    target.kind == kind
                    and target.start_line == position.start.line
                    and target.start_col == position.start.column
                    and target.end_line == position.end.line
                    and target.end_col == position.end.column
                )

            def leave_ClassDef(
                self,
                original_node: _cst.ClassDef,  # noqa: N803
                updated_node: _cst.ClassDef,  # noqa: N803
            ) -> _cst.BaseStatement | _cst.FlattenSentinel[_cst.BaseStatement]:
                if self._is_target(original_node, "class"):
                    return _cst.FlattenSentinel([updated_node, *insertion_stmts])
                return updated_node

            def leave_FunctionDef(
                self,
                original_node: _cst.FunctionDef,  # noqa: N803
                updated_node: _cst.FunctionDef,  # noqa: N803
            ) -> _cst.BaseStatement | _cst.FlattenSentinel[_cst.BaseStatement]:
                if self._is_target(original_node, "function"):
                    return _cst.FlattenSentinel([updated_node, *insertion_stmts])
                return updated_node

            def leave_SimpleStatementLine(
                self,
                original_node: _cst.SimpleStatementLine,  # noqa: N803
                updated_node: _cst.SimpleStatementLine,  # noqa: N803
            ) -> _cst.BaseStatement | _cst.FlattenSentinel[_cst.BaseStatement]:
                if self._is_target(original_node, "import"):
                    return _cst.FlattenSentinel([updated_node, *insertion_stmts])
                return updated_node

        wrapper = cst.MetadataWrapper(module)
        updated = wrapper.visit(_InsertAfterTransformer())
        return updated.code

    def _python_parse_statements(
        self,
        content: str,
        *,
        file_path: str,
    ) -> list:  # type: ignore[type-arg]
        if cst is None:
            msg = "libcst is required for Python AST patching"
            raise ParserUnavailableError(msg)
        import textwrap
        try:
            parsed = cst.parse_module(textwrap.dedent(content))
        except Exception as exc:  # pragma: no cover - parser errors vary
            raise RuntimeError(f"Python parse error in replacement for {file_path}: {exc}") from exc
        statements = list(parsed.body)
        if not statements:
            msg = f"Python replacement/insertion content is empty for {file_path}"
            raise RuntimeError(msg)
        return statements

    def _resolve_unique_selector_span(
        self,
        language: Literal["python", "typescript", "rust"],
        source: str,
        selector: NodeSelector,
        file_path: str,
    ) -> tuple[int, int] | None:
        if selector.kind != "symbol":
            msg = f"Unsupported selector kind '{selector.kind}' in {file_path}"
            raise RuntimeError(msg)

        if language == "python":
            spans = self._python_symbol_spans(source, selector.value, selector.match)
        elif language in {"typescript", "rust"}:
            try:
                spans = self._treesitter_symbol_spans(language, source, selector.value, selector.match)
            except ParserUnavailableError:
                # Fallback to basic regex for simple declarations if parser is missing
                # e.g. "interface Name", "class Name", "function name", "struct Name"
                import re
                if language == "typescript":
                    pattern = rf"(?:interface|class|function|type|enum)\s+{re.escape(selector.value)}\b"
                else:  # rust
                    pattern = rf"(?:fn|struct|enum|trait|mod|type)\s+{re.escape(selector.value)}\b"
                
                spans = []
                for match_obj in re.finditer(pattern, source):
                    # For simplicity in regex fallback, we just return the line or a reasonable chunk.
                    # Since we don't have the full AST, it's safer to only support exact match 
                    # and return the start of the match.
                    # NOTE: This is a best-effort fallback for environments like 3.13.
                    start = match_obj.start()
                    # Find end of "line" or find matching braces if we were fancy, but 
                    # for now let's just find the next closing brace at same depth?
                    # Actually, simplest is to just flag it as "selector matched but span uncertain"
                    # OR just use the match end as a starting point.
                    
                    # For replace_node, we REALLY need the end byte. 
                    # Fallback implementation: find the next '}' that seems to close this.
                    # This is very error prone, so let's only do it if the LLM is using 
                    # simple structures.
                    
                    # Better fallback for replace_node: return the match start and end.
                    # If it's a replace_node, the LLM expects to replace the WHOLE node.
                    # Without an AST, we can't accurately find the end.
                    
                    # Let's re-raise if it's not a simple case.
                    raise
        else:
            msg = f"Unsupported selector language '{language}' in {file_path}"
            raise RuntimeError(msg)

        if not spans:
            return None
        if len(spans) > 1:
            msg = f"Selector '{selector.value}' is ambiguous in {file_path}; matched {len(spans)} nodes"
            raise SelectorAmbiguousError(msg)
        return spans[0]

    def _python_symbol_spans(
        self,
        source: str,
        symbol: str,
        match: Literal["exact", "contains"],
    ) -> list[tuple[int, int]]:
        matches = self._python_declaration_matches(source, symbol=symbol, match=match)
        line_starts = self._line_start_offsets(source)
        spans: list[tuple[int, int]] = []
        for item in matches:
            start = self._offset(line_starts, item.start_line, item.start_col)
            end = self._offset(line_starts, item.end_line, item.end_col)
            spans.append((start, end))
        return spans

    def _python_declaration_matches(
        self,
        source: str,
        *,
        symbol: str,
        match: Literal["exact", "contains"],
    ) -> list[PythonDeclMatch]:
        if cst is None or PositionProvider is None:
            msg = "libcst is required for Python AST patching"
            raise ParserUnavailableError(msg)

        try:
            module = cst.parse_module(source)
        except Exception as exc:  # pragma: no cover - parser errors vary
            raise RuntimeError(f"Python parse error: {exc}") from exc

        # Import the actual type for use in the visitor
        from libcst.metadata import PositionProvider as _PositionProvider
        import libcst as _cst

        class _DeclVisitor(_cst.CSTVisitor):
            METADATA_DEPENDENCIES = (_PositionProvider,)

            def __init__(self) -> None:
                self.items: list[PythonDeclMatch] = []
                self._class_stack: list[str] = []

            def _is_match(self, name: str) -> bool:
                if match == "exact":
                    return name == symbol
                return symbol in name

            def visit_ClassDef(self, node: _cst.ClassDef) -> None:  # noqa: N802
                from libcst.metadata import CodeRange
                bare = node.name.value
                self._class_stack.append(bare)
                qualified = ".".join(self._class_stack)
                if not self._is_match(bare) and not self._is_match(qualified):
                    return
                position: CodeRange = self.get_metadata(_PositionProvider, node)  # type: ignore[assignment]
                self.items.append(
                    PythonDeclMatch(
                        kind="class",
                        name=qualified,
                        start_line=position.start.line,
                        start_col=position.start.column,
                        end_line=position.end.line,
                        end_col=position.end.column,
                    )
                )

            def leave_ClassDef(self, node: _cst.ClassDef) -> None:  # noqa: N802
                self._class_stack.pop()

            def visit_FunctionDef(self, node: _cst.FunctionDef) -> None:  # noqa: N802
                from libcst.metadata import CodeRange
                bare = node.name.value
                qualified = ".".join(self._class_stack + [bare]) if self._class_stack else bare
                if not self._is_match(bare) and not self._is_match(qualified):
                    return
                position: CodeRange = self.get_metadata(_PositionProvider, node)  # type: ignore[assignment]
                self.items.append(
                    PythonDeclMatch(
                        kind="function",
                        name=qualified,
                        start_line=position.start.line,
                        start_col=position.start.column,
                        end_line=position.end.line,
                        end_col=position.end.column,
                    )
                )

            def visit_SimpleStatementLine(self, node: _cst.SimpleStatementLine) -> None:  # noqa: N802
                from libcst.metadata import CodeRange
                for small_stmt in node.body:
                    if isinstance(small_stmt, _cst.Import):
                        for name_node in small_stmt.names:
                            name = name_node.asname.name.value if name_node.asname else name_node.name.value
                            if self._is_match(name):
                                position: CodeRange = self.get_metadata(_PositionProvider, node)  # type: ignore[assignment]
                                self.items.append(
                                    PythonDeclMatch(
                                        kind="import",
                                        name=name,
                                        start_line=position.start.line,
                                        start_col=position.start.column,
                                        end_line=position.end.line,
                                        end_col=position.end.column,
                                    )
                                )
                                return
                    elif isinstance(small_stmt, _cst.ImportFrom):
                        if isinstance(small_stmt.names, _cst.ImportStar):
                            continue
                        for name_node in small_stmt.names:
                            name = name_node.asname.name.value if name_node.asname else name_node.name.value
                            if self._is_match(name):
                                position: CodeRange = self.get_metadata(_PositionProvider, node)  # type: ignore[assignment]
                                self.items.append(
                                    PythonDeclMatch(
                                        kind="import",
                                        name=name,
                                        start_line=position.start.line,
                                        start_col=position.start.column,
                                        end_line=position.end.line,
                                        end_col=position.end.column,
                                    )
                                )
                                return

        wrapper = cst.MetadataWrapper(module)
        visitor = _DeclVisitor()
        wrapper.visit(visitor)
        return visitor.items

    def _treesitter_symbol_spans(
        self,
        language: Literal["typescript", "rust"],
        source: str,
        symbol: str,
        match: Literal["exact", "contains"],
    ) -> list[tuple[int, int]]:
        parser = self._get_tree_sitter_parser(language)
        if not parser:
            raise ParserUnavailableError(f"No tree-sitter parser for {language}")

        tree = parser.parse(source.encode())
        root = tree.root_node

        # Find all matching nodes by symbol name
        if match == "exact":
            # Exact match on node text content
            spans = []
            self._find_exact_symbol_matches(root, symbol, source, spans)
            return spans
        else:
            # Contains match (broader search)
            spans = []
            self._find_contains_symbol_matches(root, symbol, source, spans)
            return spans

    def _find_exact_symbol_matches(self, node, symbol: str, source: str, spans: list[tuple[int, int]]) -> None:
        """Find exact symbol matches in AST nodes."""
        # Check if this node matches the symbol
        if node.is_named:
            node_text = source[node.start_byte : node.end_byte]
            if node_text.strip() == symbol:
                spans.append((node.start_byte, node.end_byte))
                return  # Found exact match, no need to search deeper

        # Recursively search children
        for child in node.children:
            self._find_exact_symbol_matches(child, symbol, source, spans)

    def _find_contains_symbol_matches(self, node, symbol: str, source: str, spans: list[tuple[int, int]]) -> None:
        """Find symbol matches that contain the search text."""
        if node.is_named:
            node_text = source[node.start_byte : node.end_byte]
            if symbol in node_text:
                spans.append((node.start_byte, node.end_byte))

        # Recursively search children
        for child in node.children:
            self._find_contains_symbol_matches(child, symbol, source, spans)

    def _selector_matches(
        self,
        text: str,
        symbol: str,
        match: Literal["exact", "contains"],
    ) -> bool:
        if match == "contains":
            return symbol in text
        pattern = re.compile(rf"\\b{re.escape(symbol)}\\b")
        return bool(pattern.search(text))

    def _find_symbol_offsets(
        self,
        source: str,
        symbol: str,
        match: Literal["exact", "contains"],
    ) -> list[tuple[int, int]]:
        if match == "contains":
            indices: list[tuple[int, int]] = []
            start = 0
            while True:
                idx = source.find(symbol, start)
                if idx == -1:
                    break
                indices.append((idx, idx + len(symbol)))
                start = idx + len(symbol)
            return indices
        pattern = re.compile(rf"\\b{re.escape(symbol)}\\b")
        return [(item.start(), item.end()) for item in pattern.finditer(source)]

    def _get_tree_sitter_parser(self, language: Literal["typescript", "rust"]):  # type: ignore[no-untyped-def]
        if not self._tree_sitter_ready:
            try:
                from tree_sitter_language_pack import get_parser  # type: ignore
            except Exception as exc:
                msg = "tree_sitter_language_pack is required for TypeScript/Rust AST patching"
                raise ParserUnavailableError(msg) from exc

            self._ts_parser = get_parser("typescript")
            self._rs_parser = get_parser("rust")
            self._tree_sitter_ready = True

        if language == "typescript":
            return self._ts_parser
        return self._rs_parser

    def _line_start_offsets(self, source: str) -> list[int]:
        starts = [0]
        for idx, char in enumerate(source):
            if char == "\n":
                starts.append(idx + 1)
        return starts

    def _offset(self, line_starts: list[int], lineno: int, col: int) -> int:
        index = max(0, min(lineno - 1, len(line_starts) - 1))
        return line_starts[index] + max(col, 0)

    def _dedupe_spans(self, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
        deduped = sorted(set(spans))
        if not deduped:
            return deduped
        # Keep only minimal spans when one span fully contains another.
        minimal: list[tuple[int, int]] = []
        for span in deduped:
            start, end = span
            contains_other = False
            for other in deduped:
                if other == span:
                    continue
                o_start, o_end = other
                if start <= o_start and end >= o_end:
                    contains_other = True
                    break
            if not contains_other:
                minimal.append(span)
        return minimal or deduped
