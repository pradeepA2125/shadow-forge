"""Apply search/replace ops to the markdown plan, reusing PatchEngine.

The plan is written to <scratch_dir>/plan.md, the model's ops are applied as
SearchReplaceOpV2 against that file, and the patched file is read back. This reuses
the exact op + engine the execution loop already uses for code, and keeps the plan
addressable by content (no line numbers).
"""
from __future__ import annotations

from pathlib import Path

from agentd.domain.models import PatchCandidateV2, SearchReplaceOpV2
from agentd.patch.engine import PatchEngine

_PLAN_FILE = "plan.md"


class PlanPatchError(Exception):
    """A plan patch op could not be applied (search text missing or not unique)."""


async def apply_plan_patch(
    plan_markdown: str,
    ops: list[dict[str, object]],
    *,
    scratch_dir: Path,
    patch_engine: PatchEngine | None = None,
) -> str:
    """Apply search_replace `ops` to `plan_markdown`; return the patched markdown.

    Raises PlanPatchError if any op fails to apply (search text missing or not
    unique), so the caller can inject a correction and let the model retry.
    """
    if not ops:
        raise PlanPatchError("emit_plan_patch had no ops")

    engine = patch_engine or PatchEngine()
    scratch_dir.mkdir(parents=True, exist_ok=True)
    plan_path = scratch_dir / _PLAN_FILE
    plan_path.write_text(plan_markdown, encoding="utf-8")

    patch_ops: list[SearchReplaceOpV2] = []
    for raw in ops:
        if not isinstance(raw, dict) or raw.get("op") != "search_replace":
            raise PlanPatchError(f"unsupported plan patch op: {raw!r} (only search_replace)")
        try:
            patch_ops.append(
                SearchReplaceOpV2(
                    op="search_replace",
                    file=_PLAN_FILE,
                    search=str(raw.get("search", "")),
                    replace=str(raw.get("replace", "")),
                    reason=str(raw.get("reason", "plan edit")),
                )
            )
        except Exception as exc:  # pydantic validation (e.g. empty search)
            raise PlanPatchError(f"invalid plan patch op: {exc}") from exc

    candidate = PatchCandidateV2(candidate_id="plan-patch", patch_ops=patch_ops)
    # apply_patch_candidate signals ALL failures by raising: preflight failures raise
    # PatchPolicyViolation / RuntimeError; per-op apply failures (search not found /
    # not unique) are collected and re-raised as RuntimeError. Wrap as PlanPatchError.
    try:
        await engine.apply_patch_candidate(scratch_dir, candidate, allowed_files={_PLAN_FILE})
    except Exception as exc:
        raise PlanPatchError(str(exc)) from exc

    return plan_path.read_text(encoding="utf-8")
