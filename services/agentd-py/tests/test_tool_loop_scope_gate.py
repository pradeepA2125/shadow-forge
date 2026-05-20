"""Tests for ToolLoop's scope-extension callback hook."""
from __future__ import annotations

from pathlib import Path

import pytest

from agentd.domain.models import (
    PlanStep,
    PlanTarget,
    PlanTargetIntent,
    TaskBudget,
    TaskUsage,
)
from agentd.orchestrator.broadcaster import PatchEventBroadcaster
from agentd.tools.loop import (
    PlanHandoff,
    ScopeDecision,
    ToolLoop,
    VerifyResult,
)
from agentd.tools.registry import ToolOutput


class _ScriptedReasoning:
    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.received: list[tuple[dict, list, list]] = []

    async def create_tool_step(self, *, step_context, history, tool_definitions, on_thinking=None, state_description="", allowed_action_types=None):
        self.received.append((dict(step_context), list(history), list(tool_definitions)))
        return self._responses.pop(0)

    async def create_planning_step(self, *args, **kwargs):
        msg = "create_planning_step should not be called in tool loop tests"
        raise AssertionError(msg)


class _NoopRegistry:
    """Registry that returns a no-op output for any tool call (we only test emit_patch paths)."""

    def definitions(self, phase: str = "explore"):
        return []

    async def execute(self, name: str, args: dict):
        return ToolOutput(output="(stub)", is_error=False)

    def use_shadow_for_reads(self) -> None:
        pass


def _make_step(targets: list[str]) -> PlanStep:
    return PlanStep(
        id="s1",
        goal="add tests",
        risk="low",
        targets=[PlanTarget(path=t, intent=PlanTargetIntent.NEW) for t in targets],
    )


def _stub_inline_apply(loop: ToolLoop, scope_blocked_path: str) -> None:
    """Replace _apply_patch_inline with a fake that errors when scope_blocked_path
    isn't in step.targets, succeeds when it IS."""

    async def fake(patch_doc, step):
        target_paths = {t.path for t in step.targets}
        if scope_blocked_path not in target_paths:
            return {
                "is_error": True,
                "error": (
                    "Patch preflight failed: Patch op targets file outside "
                    f"current step scope: {scope_blocked_path}"
                ),
                "touched_files": [],
            }
        return {"is_error": False, "touched_files": list(target_paths)}

    loop._apply_patch_inline = fake  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_scope_callback_approve_extends_targets_and_retries(tmp_path: Path) -> None:
    """When the callback approves, step.targets grows and the patch is retried successfully."""
    step = _make_step(["tests/test_x.py"])
    captured: list[tuple[list[str], str]] = []

    async def approve_cb(files: list[str], reason: str) -> ScopeDecision:
        captured.append((files, reason))
        return ScopeDecision(approve=True, extended_files=files, reason="ok")

    reasoning = _ScriptedReasoning([
        {
            "type": "emit_patch",
            "thought": "need init for pytest discovery",
            "patch_ops": [
                {"op": "create_file", "file": "tests/__init__.py", "content": "", "reason": "pkg"},
                {"op": "create_file", "file": "tests/test_x.py", "content": "x = 1", "reason": "test"},
            ],
        },
        {"type": "verify_done", "thought": "scripted", "verified": True, "test_output": ""},
    ])
    loop = ToolLoop(
        reasoning_engine=reasoning,
        registry=_NoopRegistry(),
        broadcaster=PatchEventBroadcaster(),
        task_id="t1",
        patch_engine=object(),       # truthy stub — we override _apply_patch_inline
        shadow_path=tmp_path,
        scope_extension_callback=approve_cb,
    )
    _stub_inline_apply(loop, scope_blocked_path="tests/__init__.py")

    out = await loop.run(
        step=step,
        patch_request_context={"allowed_files": ["tests/test_x.py"]},
        budget=TaskBudget(max_tool_calls_per_step=4, max_verify_calls_per_step=2),
        usage=TaskUsage(),
    )

    assert isinstance(out, VerifyResult)
    assert out.verified is True
    assert captured == [(["tests/__init__.py"], "need init for pytest discovery")]
    assert any(t.path == "tests/__init__.py" for t in step.targets)


@pytest.mark.asyncio
async def test_scope_callback_reject_keeps_existing_behavior(tmp_path: Path) -> None:
    """Reject → loop adds rejection to history, agent emits revision_needed → PlanHandoff."""
    step = _make_step(["tests/test_x.py"])

    async def reject_cb(files: list[str], reason: str) -> ScopeDecision:
        return ScopeDecision(approve=False, extended_files=[], reason="user said no")

    reasoning = _ScriptedReasoning([
        {
            "type": "emit_patch",
            "thought": "need init",
            "patch_ops": [
                {"op": "create_file", "file": "tests/__init__.py", "content": "", "reason": "pkg"},
            ],
        },
        {
            "type": "revision_needed",
            "thought": "blocked by scope",
            "reason": "need __init__.py in scope",
            "evidence": "user denied extension",
            "affected_steps": [],
        },
    ])
    loop = ToolLoop(
        reasoning_engine=reasoning,
        registry=_NoopRegistry(),
        broadcaster=PatchEventBroadcaster(),
        task_id="t1",
        patch_engine=object(),
        shadow_path=tmp_path,
        scope_extension_callback=reject_cb,
    )
    _stub_inline_apply(loop, scope_blocked_path="tests/__init__.py")

    out = await loop.run(
        step=step,
        patch_request_context={"allowed_files": ["tests/test_x.py"]},
        budget=TaskBudget(max_tool_calls_per_step=4, max_verify_calls_per_step=2),
        usage=TaskUsage(),
    )

    assert isinstance(out, PlanHandoff)
    # Rejection must NOT have mutated step.targets
    assert all(t.path != "tests/__init__.py" for t in step.targets)


@pytest.mark.asyncio
async def test_no_callback_falls_back_to_default_reject(tmp_path: Path) -> None:
    """No callback supplied → default _reject_callback fires → existing behavior preserved."""
    step = _make_step(["tests/test_x.py"])

    reasoning = _ScriptedReasoning([
        {
            "type": "emit_patch",
            "thought": "need init",
            "patch_ops": [
                {"op": "create_file", "file": "tests/__init__.py", "content": "", "reason": "pkg"},
            ],
        },
        {
            "type": "revision_needed",
            "thought": "blocked",
            "reason": "need __init__.py",
            "evidence": "rejected",
            "affected_steps": [],
        },
    ])
    loop = ToolLoop(
        reasoning_engine=reasoning,
        registry=_NoopRegistry(),
        broadcaster=PatchEventBroadcaster(),
        task_id="t1",
        patch_engine=object(),
        shadow_path=tmp_path,
        # no scope_extension_callback — default reject applies
    )
    _stub_inline_apply(loop, scope_blocked_path="tests/__init__.py")

    out = await loop.run(
        step=step,
        patch_request_context={"allowed_files": ["tests/test_x.py"]},
        budget=TaskBudget(max_tool_calls_per_step=4, max_verify_calls_per_step=2),
        usage=TaskUsage(),
    )
    assert isinstance(out, PlanHandoff)
    assert all(t.path != "tests/__init__.py" for t in step.targets)
