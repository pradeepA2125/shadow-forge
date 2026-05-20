from __future__ import annotations

from pathlib import Path

from agentd.domain.models import (
    CandidateScoreBreakdown,
    PatchCandidateV2,
    ValidationResult,
)
from agentd.orchestrator.engine import AgentOrchestrator, _CandidateEvaluation
from agentd.patch.engine import PatchEngine
from agentd.storage.in_memory import InMemoryTaskStore
from agentd.workspace.shadow import ShadowWorkspaceManager


class _DummyReasoningEngine:
    async def create_plan(self, task, workspace_path, retrieval_context, on_thinking=None):  # type: ignore[no-untyped-def]
        _ = (task, workspace_path, retrieval_context, on_thinking)
        raise RuntimeError("not used in scoring tests")

    async def create_patch(self, task, workspace_path, diagnostics, retrieval_context, **kwargs):  # type: ignore[no-untyped-def]
        _ = (task, workspace_path, diagnostics, retrieval_context, kwargs)
        raise RuntimeError("not used in scoring tests")

    async def create_tool_step(self, step_context, history, tool_definitions, on_thinking=None, state_description="", allowed_action_types=None):  # type: ignore[no-untyped-def]
        raise RuntimeError("not used in scoring tests")

    async def create_planning_step(self, plan_context, history, tool_definitions, on_thinking=None):  # type: ignore[no-untyped-def]
        _ = (plan_context, history, tool_definitions)
        return {
            "type": "emit_plan",
            "thought": "stub: planning agent bypassed",
            "plan_markdown": "# Stub Plan\n\n- Review generated changes",
            "files_examined": [],
            "confidence": "high",
        }


class _DummyValidator:
    async def run(self, workspace_path: str) -> ValidationResult:
        _ = workspace_path
        return ValidationResult(success=True, diagnostics=[], duration_ms=0)


def _new_orchestrator(tmp_path: Path) -> AgentOrchestrator:
    return AgentOrchestrator(
        store=InMemoryTaskStore(),
        reasoning_engine=_DummyReasoningEngine(),
        validator=_DummyValidator(),
        patch_engine=PatchEngine(),
        workspace_manager=ShadowWorkspaceManager(root_path=tmp_path / "shadows"),
    )


def test_score_candidate_formula_is_deterministic(tmp_path: Path) -> None:
    orchestrator = _new_orchestrator(tmp_path)
    score = orchestrator._score_candidate(
        preflight_pass=True,
        validation_pass=True,
        changed_lines=10,
        op_count=3,
        new_file_count=1,
    )
    assert score.score == 100.0 + 60.0 - (0.05 * 10) - (2.0 * 3) - (5.0 * 1)


def test_select_best_candidate_uses_fixed_tiebreaker(tmp_path: Path) -> None:
    orchestrator = _new_orchestrator(tmp_path)
    candidate_a = PatchCandidateV2.model_validate(
        {
            "candidate_id": "a",
            "patch_ops": [
                {
                    "op": "create_file",
                    "file": "x.txt",
                    "content": "x",
                    "reason": "x",
                }
            ],
        }
    )
    candidate_b = PatchCandidateV2.model_validate(
        {
            "candidate_id": "b",
            "patch_ops": [
                {
                    "op": "create_file",
                    "file": "y.txt",
                    "content": "y",
                    "reason": "y",
                }
            ],
        }
    )
    eval_a = _CandidateEvaluation(
        candidate=candidate_a,
        score=100.0,
        breakdown=CandidateScoreBreakdown(
            preflight_pass=True,
            validation_pass=False,
            changed_lines=5,
            op_count=1,
            new_file_count=1,
            score=100.0,
        ),
        preflight_issues=[],
        validation=None,
        touched_files=["x.txt"],
        changed_lines=5,
        new_file_count=1,
        preflight_report_path=None,
        validation_report_path=None,
    )
    eval_b = _CandidateEvaluation(
        candidate=candidate_b,
        score=100.0,
        breakdown=CandidateScoreBreakdown(
            preflight_pass=True,
            validation_pass=False,
            changed_lines=5,
            op_count=1,
            new_file_count=1,
            score=100.0,
        ),
        preflight_issues=[],
        validation=None,
        touched_files=["y.txt"],
        changed_lines=5,
        new_file_count=1,
        preflight_report_path=None,
        validation_report_path=None,
    )

    selected = orchestrator._select_best_candidate([eval_b, eval_a])
    assert selected is not None
    assert selected.candidate.candidate_id == "a"
