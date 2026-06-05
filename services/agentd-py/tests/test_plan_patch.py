from __future__ import annotations

from pathlib import Path

import pytest

from agentd.planning.plan_patch import apply_plan_patch, PlanPatchError

_PLAN = "# Plan\n\n## Step 1: Alpha\n- do alpha\n\n## Step 2: Beta\n- do beta\n"


@pytest.mark.asyncio
async def test_apply_single_search_replace(tmp_path: Path) -> None:
    ops = [{"op": "search_replace", "search": "- do beta", "replace": "- do beta CHANGED", "reason": "fix"}]
    out = await apply_plan_patch(_PLAN, ops, scratch_dir=tmp_path)
    assert "- do beta CHANGED" in out
    assert "- do alpha" in out  # untouched


@pytest.mark.asyncio
async def test_apply_multiple_disjoint_ops(tmp_path: Path) -> None:
    ops = [
        {"op": "search_replace", "search": "- do alpha", "replace": "- do ALPHA", "reason": "a"},
        {"op": "search_replace", "search": "- do beta", "replace": "- do BETA", "reason": "b"},
    ]
    out = await apply_plan_patch(_PLAN, ops, scratch_dir=tmp_path)
    assert "- do ALPHA" in out and "- do BETA" in out


@pytest.mark.asyncio
async def test_search_not_found_raises_planpatcherror(tmp_path: Path) -> None:
    ops = [{"op": "search_replace", "search": "- nonexistent", "replace": "x", "reason": "r"}]
    with pytest.raises(PlanPatchError) as exc:
        await apply_plan_patch(_PLAN, ops, scratch_dir=tmp_path)
    assert "not found" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_ambiguous_search_raises_planpatcherror(tmp_path: Path) -> None:
    plan = "## Step 1: X\n- shared\n\n## Step 2: Y\n- shared\n"
    ops = [{"op": "search_replace", "search": "- shared", "replace": "- z", "reason": "r"}]
    with pytest.raises(PlanPatchError) as exc:
        await apply_plan_patch(plan, ops, scratch_dir=tmp_path)
    assert "unique" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_empty_ops_raises_planpatcherror(tmp_path: Path) -> None:
    with pytest.raises(PlanPatchError):
        await apply_plan_patch(_PLAN, [], scratch_dir=tmp_path)
