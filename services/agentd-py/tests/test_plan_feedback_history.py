"""Plan-feedback rounds must CONTINUE the same planning conversation.

KV-cache prefix discipline: the system prompt and every payload field before
`conversation_history` stay byte-identical across feedback rounds, and the prior
planning history is replayed (not re-digested) with the new feedback appended as
the final turn. This keeps the llama-server prompt-prefix cache warm across
re-plans instead of reprefilling the whole history each round.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agentd.domain.models import Diagnostic, TaskRecord, TaskStatus, ValidationResult
from agentd.orchestrator.engine import AgentOrchestrator
from agentd.patch.engine import PatchEngine
from agentd.retrieval.artifact_client import RetrievalContext
from agentd.storage.in_memory import InMemoryTaskStore
from agentd.workspace.shadow import ShadowWorkspaceManager


class _RecordingPlanningEngine:
    """Captures the `history` (seed) handed to each planning step and emits a plan
    immediately, so the loop's final conversation_history == the seed it received."""

    def __init__(self) -> None:
        self.histories: list[list[dict[str, object]]] = []
        self.plan_contexts: list[dict[str, object]] = []

    async def create_planning_step(
        self,
        plan_context: dict[str, object],
        history: list[dict[str, object]],
        tool_definitions: list[dict[str, object]],
        on_thinking: object = None,
        state_description: str = "",
        allowed_action_types: frozenset[str] | None = None,
    ) -> dict[str, object]:
        _ = (tool_definitions, on_thinking, state_description, allowed_action_types)
        self.histories.append([dict(m) for m in history])
        self.plan_contexts.append(plan_context)
        return {
            "type": "emit_plan",
            "thought": "stub",
            "plan_markdown": "# Plan\n\n- Create helper",
            "files_examined": [],
            "confidence": "high",
        }


class _AlwaysPassValidator:
    async def run(self, workspace_path: str) -> ValidationResult:
        _ = workspace_path
        return ValidationResult(success=True, diagnostics=[], duration_ms=1)


class _StubRetrievalClient:
    def __init__(self) -> None:
        self.load_context_calls = 0

    def load_context(
        self, workspace_path: str, goal: str
    ) -> tuple[RetrievalContext, list[Diagnostic]]:
        _ = (workspace_path, goal)
        self.load_context_calls += 1
        return (
            RetrievalContext(
                related_files=["src/auth.py"],
                related_symbols=["build_auth"],
                graph_neighbors=[],
                diagnostics_excerpt=[],
                snapshot_age_sec=1.0,
                snapshot_stats={"node_count": 1, "edge_count": 0, "diagnostic_count": 0},
                planner_evidence={"workspace_files_index": ["src/auth.py"]},
            ),
            [],
        )


def _history_has(history: list[dict[str, object]], text: str) -> bool:
    return any(text in str(m.get("content", "")) for m in history)


async def _make_orchestrator(tmp_path: Path, reasoner: object | None = None) -> tuple[AgentOrchestrator, object, TaskRecord]:
    real = tmp_path / "real"
    real.mkdir(parents=True)
    (real / "README.md").write_text("hello\n", encoding="utf-8")

    store = InMemoryTaskStore()
    task = TaskRecord(task_id="task-fb-hist", goal="Add pause", workspace_path=str(real))
    await store.create(task)

    reasoner = reasoner or _RecordingPlanningEngine()
    orchestrator = AgentOrchestrator(
        store=store,
        reasoning_engine=reasoner,
        validator=_AlwaysPassValidator(),
        patch_engine=PatchEngine(),
        workspace_manager=ShadowWorkspaceManager(root_path=tmp_path / "shadows"),
        retrieval_client=_StubRetrievalClient(),
    )
    return orchestrator, reasoner, task


@pytest.mark.asyncio
async def test_feedback_rounds_replay_and_accumulate_planning_history(tmp_path: Path) -> None:
    orchestrator, reasoner, task = await _make_orchestrator(tmp_path)

    await orchestrator.run_task(task.task_id)              # round 1: no feedback
    await orchestrator.continue_task(task.task_id, feedback="USE-THE-EXISTING-ENUM")   # round 2
    await orchestrator.continue_task(task.task_id, feedback="ALSO-COVER-RESUME")       # round 3

    assert len(reasoner.histories) == 3

    # Round 1 starts from an empty conversation.
    assert reasoner.histories[0] == []

    # Round 2 replays round 1's (empty) history with the first feedback appended.
    assert _history_has(reasoner.histories[1], "USE-THE-EXISTING-ENUM")
    assert not _history_has(reasoner.histories[1], "ALSO-COVER-RESUME")

    # Round 3 replays round 2's full history AND appends the second feedback —
    # first feedback still present and ordered before the second (append-only prefix).
    assert _history_has(reasoner.histories[2], "USE-THE-EXISTING-ENUM")
    assert _history_has(reasoner.histories[2], "ALSO-COVER-RESUME")
    idx1 = next(i for i, m in enumerate(reasoner.histories[2]) if "USE-THE-EXISTING-ENUM" in str(m.get("content", "")))
    idx2 = next(i for i, m in enumerate(reasoner.histories[2]) if "ALSO-COVER-RESUME" in str(m.get("content", "")))
    assert idx1 < idx2

    # Round 2's history is a prefix of round 3's history (nothing rewritten).
    assert reasoner.histories[2][: len(reasoner.histories[1])] == reasoner.histories[1]

    # Persisted on the task so the NEXT feedback round can replay it too.
    refreshed = await orchestrator._store.get(task.task_id)
    assert refreshed.planning_conversation_history is not None
    assert _history_has(refreshed.planning_conversation_history, "USE-THE-EXISTING-ENUM")
    assert _history_has(refreshed.planning_conversation_history, "ALSO-COVER-RESUME")


@pytest.mark.asyncio
async def test_feedback_round_with_pinned_context_skips_retrieval(tmp_path: Path) -> None:
    """A feedback round reuses round 1's pinned retrieval — it must NOT recompute
    load_context (which could diverge via background re-index and break the prefix,
    besides paying for a reindex it would discard)."""
    orchestrator, _reasoner, task = await _make_orchestrator(tmp_path)
    client = orchestrator._retrieval_client  # type: ignore[attr-defined]

    await orchestrator.run_task(task.task_id)
    assert client.load_context_calls == 1  # round 1 computes + pins retrieval

    await orchestrator.continue_task(task.task_id, feedback="tweak it")
    assert client.load_context_calls == 1  # feedback round reused the pin, no recompute


@pytest.mark.asyncio
async def test_feedback_round_replays_the_prior_emitted_plan(tmp_path: Path) -> None:
    """On feedback the model must see the plan it is being asked to revise — the
    emit_plan turn (carrying plan_markdown) is part of the replayed history."""
    orchestrator, reasoner, task = await _make_orchestrator(tmp_path)

    await orchestrator.run_task(task.task_id)
    await orchestrator.continue_task(task.task_id, feedback="tweak the helper")

    # Round 2's replayed history contains round 1's emitted plan markdown.
    assert _history_has(reasoner.histories[1], "Create helper")

    # And it is persisted, so later rounds keep seeing it too.
    refreshed = await orchestrator._store.get(task.task_id)
    assert refreshed.planning_conversation_history is not None
    assert _history_has(refreshed.planning_conversation_history, "Create helper")


@pytest.mark.asyncio
async def test_feedback_travels_as_history_turn_with_pinned_prefix(tmp_path: Path) -> None:
    orchestrator, reasoner, task = await _make_orchestrator(tmp_path)

    await orchestrator.run_task(task.task_id)
    await orchestrator.continue_task(task.task_id, feedback="FB-ONE")
    await orchestrator.continue_task(task.task_id, feedback="FB-TWO")

    # Feedback must NOT ride in initial_context (that field sits BEFORE
    # conversation_history in the payload — a changing value there reprefills the
    # whole history). It only enters via the appended history turn.
    for ctx in reasoner.plan_contexts:
        initial = ctx.get("initial_context") or {}
        assert "plan_feedback" not in initial

    # The cacheable prefix (everything before conversation_history) is pinned:
    # initial_context is byte-identical across all rounds.
    first_ic = reasoner.plan_contexts[0].get("initial_context")
    assert all(ctx.get("initial_context") == first_ic for ctx in reasoner.plan_contexts)


def test_build_payload_does_not_place_feedback_before_history() -> None:
    from agentd.planning.prompts import build_planning_step_payload

    plan_context = {
        "goal": "g",
        "workspace_path": "/w",
        "initial_context": {"plan_feedback": "LEGACY-FIELD", "evidence": "x"},
    }
    history = [
        {"role": "assistant", "content": "{}"},
        {"role": "tool_result", "tool": "", "content": "user feedback: revise"},
    ]
    payload = build_planning_step_payload(plan_context, history, [])

    # No top-level plan_feedback key may precede conversation_history.
    assert "plan_feedback" not in payload
    keys = list(payload.keys())
    assert "conversation_history" in keys
    # budget_status stays last; only instruction/budget_status follow the history.
    assert keys[-1] == "budget_status"
    assert keys.index("conversation_history") < keys.index("budget_status")


# --- emit_plan_patch feedback integration (Task 4) ---
class _PatchEmittingEngine:
    """emit_plan on round 1; emit_plan_patch (search_replace) on feedback rounds."""

    def __init__(self) -> None:
        self.saw_plan_patch = False
        self.histories: list[list[dict[str, object]]] = []

    async def create_planning_step(
        self,
        plan_context: dict[str, object],
        history: list[dict[str, object]],
        tool_definitions: list[dict[str, object]],
        on_thinking: object = None,
        state_description: str = "",
        allowed_action_types: frozenset[str] | None = None,
    ) -> dict[str, object]:
        _ = (tool_definitions, on_thinking, state_description, allowed_action_types)
        self.histories.append([dict(m) for m in history])
        is_feedback = any("gave this feedback" in str(m.get("content", "")) for m in history)
        if is_feedback and plan_context.get("allow_plan_patch"):
            self.saw_plan_patch = True
            return {
                "type": "emit_plan_patch",
                "thought": "small edit",
                "ops": [{"op": "search_replace", "search": "- Create helper",
                         "replace": "- Create helper PATCHED", "reason": "feedback"}],
            }
        return {
            "type": "emit_plan", "thought": "stub",
            "plan_markdown": "# Plan\n\n- Create helper",
            "files_examined": [], "confidence": "high",
        }


@pytest.mark.asyncio
async def test_feedback_round_applies_plan_patch(tmp_path: Path) -> None:
    engine = _PatchEmittingEngine()
    orchestrator, _reasoner, task = await _make_orchestrator(tmp_path, reasoner=engine)

    await orchestrator.run_task(task.task_id)
    await orchestrator.continue_task(task.task_id, feedback="tweak the helper")

    refreshed = await orchestrator._store.get(task.task_id)
    assert engine.saw_plan_patch
    assert refreshed.status == TaskStatus.AWAITING_PLAN_APPROVAL
    assert "- Create helper PATCHED" in (refreshed.plan_markdown or "")


@pytest.mark.asyncio
async def test_plan_patch_feedback_rounds_are_append_only(tmp_path: Path) -> None:
    """Two plan-patch feedback rounds: round-2 history must be a verbatim prefix of
    round-3 history (the current-plan embed in the feedback turn never mutates an
    earlier entry — KV prefix invariant)."""
    engine = _PatchEmittingEngine()
    orchestrator, _r, task = await _make_orchestrator(tmp_path, reasoner=engine)

    await orchestrator.run_task(task.task_id)
    await orchestrator.continue_task(task.task_id, feedback="one")
    h1 = [dict(m) for m in engine.histories[1]]
    await orchestrator.continue_task(task.task_id, feedback="two")
    h2 = engine.histories[2]

    assert h2[: len(h1)] == h1
