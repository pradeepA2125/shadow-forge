from __future__ import annotations

from pathlib import Path

import pytest

from agentd.domain.models import Diagnostic, TaskRecord, TaskStatus, ValidationResult
from agentd.orchestrator.engine import AgentOrchestrator
from agentd.patch.engine import PatchEngine
from agentd.storage.in_memory import InMemoryTaskStore
from agentd.workspace.shadow import ShadowWorkspaceManager


class PassValidator:
    async def run_touched(self, workspace_path: str, touched_files: list[str]) -> ValidationResult:
        _ = (workspace_path, touched_files)
        return ValidationResult(success=True, diagnostics=[], duration_ms=1)

    async def run(self, workspace_path: str) -> ValidationResult:
        _ = workspace_path
        return ValidationResult(success=True, diagnostics=[], duration_ms=1)


class ReplanningReasoner:
    def __init__(self) -> None:
        self.markdown_plan_calls = 0
        self.plan_calls = 0
        self.plan_contexts: list[dict[str, object]] = []

    async def create_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        retrieval_context: dict[str, object],
        on_thinking: object = None,
    ) -> object:
        _ = (task, workspace_path, on_thinking)
        self.plan_calls += 1
        self.plan_contexts.append(dict(retrieval_context))
        if self.plan_calls == 1:
            return {
                "analysis": "bad first plan",
                "steps": [
                    {
                        "id": "S1",
                        "goal": "Update endpoint",
                        "targets": [{"path": "agentd/api/tasks.py", "intent": "existing"}],
                        "risk": "low",
                    }
                ],
                "expected_files": ["agentd/api/tasks.py"],
                "stop_conditions": ["tests pass"],
            }
        return {
            "analysis": "fixed plan",
            "steps": [
                {
                    "id": "S1",
                    "goal": "Update endpoint",
                    "targets": [{"path": "src/example.py", "intent": "existing"}],
                    "risk": "low",
                }
            ],
            "expected_files": ["src/example.py"],
            "stop_conditions": ["tests pass"],
        }

    async def create_patch(
        self,
        task: TaskRecord,
        workspace_path: str,
        diagnostics: list[Diagnostic],
        retrieval_context: dict[str, object],
        **kwargs: object,
    ) -> object:
        _ = (task, workspace_path, diagnostics, retrieval_context, kwargs)
        return {
            "candidates": [
                {
                    "candidate_id": "c1",
                    "patch_ops": [
                        {
                            "op": "replace_node",
                            "file": "src/example.py",
                            "language": "python",
                            "selector": {"kind": "symbol", "value": "X", "match": "exact"},
                            "content": "class X:\n    pass\n    updated = True\n",
                            "reason": "apply update",
                        }
                    ],
                }
            ]
        }

    async def create_tool_step(
        self,
        step_context: dict[str, object],
        history: list[dict[str, object]],
        tool_definitions: list[dict[str, object]],
        on_thinking: object = None,
        state_description: str = "",
        allowed_action_types: frozenset[str] | None = None,
    ) -> dict[str, object]:
        _ = (step_context, tool_definitions, on_thinking)
        in_verify = any(
            isinstance(msg.get("content"), str) and "Patch applied successfully" in msg["content"]
            for msg in history
        )
        if in_verify:
            return {"type": "verify_done", "thought": "scripted", "verified": True, "test_output": ""}
        return {
            "type": "emit_patch",
            "thought": "scripted",
            "patch_ops": [
                {
                    "op": "replace_node",
                    "file": "src/example.py",
                    "language": "python",
                    "selector": {"kind": "symbol", "value": "X", "match": "exact"},
                    "content": "class X:\n    pass\n    updated = True\n",
                    "reason": "apply update",
                }
            ],
        }

    async def create_planning_step(
        self,
        plan_context: dict,
        history: list,
        tool_definitions: list,
        on_thinking: object = None,
        state_description: str = "",
        allowed_action_types: frozenset[str] | None = None,
    ) -> dict:
        _ = (plan_context, history, tool_definitions)
        return {
            "type": "emit_plan",
            "thought": "stub: planning agent bypassed",
            "plan_markdown": "# Stub Plan\n\n- Review generated changes",
            "files_examined": [],
            "confidence": "high",
        }


class AlwaysBadReasoner(ReplanningReasoner):
    async def create_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        retrieval_context: dict[str, object],
        on_thinking: object = None,
    ) -> object:
        _ = (task, workspace_path, on_thinking)
        self.plan_calls += 1
        self.plan_contexts.append(dict(retrieval_context))
        return {
            "analysis": "bad plan",
            "steps": [
                {
                    "id": "S1",
                    "goal": "Update endpoint",
                    "targets": [{"path": "agentd/api/tasks.py"}],
                    # Intentionally omit intent to verify strict validation diagnostics.
                    "risk": "low",
                }
            ],
            "expected_files": ["agentd/api/tasks.py"],
            "stop_conditions": ["tests pass"],
        }


class MarkdownBlueprintReasoner(ReplanningReasoner):
    async def create_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        retrieval_context: dict[str, object],
        on_thinking: object = None,
    ) -> object:
        _ = (task, workspace_path, on_thinking)
        self.plan_calls += 1
        self.plan_contexts.append(dict(retrieval_context))
        if self.plan_calls == 1:
            return {
                "analysis": "drifted plan",
                "steps": [
                    {
                        "id": "S1",
                        "goal": "Update endpoint",
                        "targets": [
                            {
                                "path": "services/agentd-py/agentd/storage/base.py",
                                "intent": "existing",
                            }
                        ],
                        "risk": "low",
                    }
                ],
                "expected_files": ["services/agentd-py/agentd/storage/base.py"],
                "stop_conditions": ["tests pass"],
            }
        return {
            "analysis": "corrected plan",
            "steps": [
                {
                    "id": "S1",
                    "goal": "Update endpoint",
                    "targets": [
                        {
                            "path": "services/agentd-py/agentd/api/routes.py",
                            "intent": "existing",
                        }
                    ],
                    "risk": "low",
                }
            ],
            "expected_files": ["services/agentd-py/agentd/api/routes.py"],
            "stop_conditions": ["tests pass"],
        }

    async def create_patch(
        self,
        task: TaskRecord,
        workspace_path: str,
        diagnostics: list[Diagnostic],
        retrieval_context: dict[str, object],
        **kwargs: object,
    ) -> object:
        _ = (task, workspace_path, diagnostics, retrieval_context, kwargs)
        return {
            "candidates": [
                {
                    "candidate_id": "c1",
                    "patch_ops": [
                        {
                            "op": "search_replace",
                            "file": "services/agentd-py/agentd/api/routes.py",
                            "search": "router = object()",
                            "replace": "router = object()\nTASK_EVENTS_ROUTE = True",
                            "reason": "apply minimal endpoint marker for validation",
                        }
                    ],
                }
            ]
        }

    async def create_tool_step(
        self,
        step_context: dict[str, object],
        history: list[dict[str, object]],
        tool_definitions: list[dict[str, object]],
        on_thinking: object = None,
        state_description: str = "",
        allowed_action_types: frozenset[str] | None = None,
    ) -> dict[str, object]:
        _ = (step_context, tool_definitions, on_thinking)
        in_verify = any(
            isinstance(msg.get("content"), str) and "Patch applied successfully" in msg["content"]
            for msg in history
        )
        if in_verify:
            return {"type": "verify_done", "thought": "scripted", "verified": True, "test_output": ""}
        return {
            "type": "emit_patch",
            "thought": "scripted",
            "patch_ops": [
                {
                    "op": "search_replace",
                    "file": "services/agentd-py/agentd/api/routes.py",
                    "search": "router = object()",
                    "replace": "router = object()\nTASK_EVENTS_ROUTE = True",
                    "reason": "apply minimal endpoint marker for validation",
                }
            ],
        }


class NewFileIntentReasoner(ReplanningReasoner):
    async def create_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        retrieval_context: dict[str, object],
        on_thinking: object = None,
    ) -> object:
        _ = (task, workspace_path, retrieval_context, on_thinking)
        self.plan_calls += 1
        self.plan_contexts.append(dict(retrieval_context))
        return {
            "analysis": "create a new test file",
            "steps": [
                {
                    "id": "S1",
                    "goal": "Add new API regression test",
                    "targets": [{"path": "tests/test_task_events_api.py", "intent": "new"}],
                    "risk": "low",
                }
            ],
            "expected_files": ["tests/test_task_events_api.py"],
            "stop_conditions": ["tests pass"],
        }

    async def create_patch(
        self,
        task: TaskRecord,
        workspace_path: str,
        diagnostics: list[Diagnostic],
        retrieval_context: dict[str, object],
        **kwargs: object,
    ) -> object:
        _ = (task, workspace_path, diagnostics, retrieval_context, kwargs)
        return {
            "candidates": [
                {
                    "candidate_id": "c1",
                    "patch_ops": [
                        {
                            "op": "create_file",
                            "file": "tests/test_task_events_api.py",
                            "content": "def test_placeholder():\n    assert True\n",
                            "reason": "add placeholder regression test",
                        }
                    ],
                }
            ]
        }

    async def create_tool_step(
        self,
        step_context: dict[str, object],
        history: list[dict[str, object]],
        tool_definitions: list[dict[str, object]],
        on_thinking: object = None,
        state_description: str = "",
        allowed_action_types: frozenset[str] | None = None,
    ) -> dict[str, object]:
        _ = (step_context, tool_definitions, on_thinking)
        in_verify = any(
            isinstance(msg.get("content"), str) and "Patch applied successfully" in msg["content"]
            for msg in history
        )
        if in_verify:
            return {"type": "verify_done", "thought": "scripted", "verified": True, "test_output": ""}
        return {
            "type": "emit_patch",
            "thought": "scripted",
            "patch_ops": [
                {
                    "op": "create_file",
                    "file": "tests/test_task_events_api.py",
                    "content": "def test_placeholder():\n    assert True\n",
                    "reason": "add placeholder regression test",
                }
            ],
        }


@pytest.mark.asyncio
async def test_orchestrator_replans_when_plan_targets_missing(tmp_path: Path) -> None:
    # With the PlanningAgent architecture, grounding retry is replaced by the
    # explore-then-commit loop. A plan targeting a non-existent file still fails
    # at step execution (preflight scope/missing-target), not at plan generation.
    real_workspace = tmp_path / "real"
    (real_workspace / "src").mkdir(parents=True)
    (real_workspace / "src/example.py").write_text("class X:\n    pass\n", encoding="utf-8")

    store = InMemoryTaskStore()
    task = TaskRecord(
        task_id="task-replan",
        goal="Update endpoint behavior",
        workspace_path=str(real_workspace),
    )
    await store.create(task)

    reasoner = ReplanningReasoner()
    orchestrator = AgentOrchestrator(
        store=store,
        reasoning_engine=reasoner,
        validator=PassValidator(),
        patch_engine=PatchEngine(),
        workspace_manager=ShadowWorkspaceManager(root_path=tmp_path / "shadows"),
    )

    initialized = await orchestrator.run_task(task.task_id)
    assert initialized.status == TaskStatus.AWAITING_PLAN_APPROVAL

    result = await orchestrator.continue_task(task.task_id, feedback=None)

    # Plan is generated once; execution fails because the tool step targets
    # src/example.py but the plan's allowed_files is agentd/api/tasks.py.
    assert result.status == TaskStatus.FAILED
    assert reasoner.plan_calls == 1


@pytest.mark.asyncio
async def test_orchestrator_fails_fast_when_replanned_targets_still_missing(tmp_path: Path) -> None:
    # With the PlanningAgent architecture, create_plan() is called exactly once.
    # A plan targeting a non-existent file without a valid intent causes step
    # execution to fail (preflight rejects the tool step's scope violation).
    real_workspace = tmp_path / "real"
    (real_workspace / "src").mkdir(parents=True)
    (real_workspace / "src/example.py").write_text("class X:\n    pass\n", encoding="utf-8")

    store = InMemoryTaskStore()
    task = TaskRecord(
        task_id="task-replan-fail",
        goal="Update endpoint behavior",
        workspace_path=str(real_workspace),
    )
    await store.create(task)

    reasoner = AlwaysBadReasoner()
    orchestrator = AgentOrchestrator(
        store=store,
        reasoning_engine=reasoner,
        validator=PassValidator(),
        patch_engine=PatchEngine(),
        workspace_manager=ShadowWorkspaceManager(root_path=tmp_path / "shadows"),
    )

    initialized = await orchestrator.run_task(task.task_id)
    assert initialized.status == TaskStatus.AWAITING_PLAN_APPROVAL

    result = await orchestrator.continue_task(task.task_id, feedback=None)

    assert result.status == TaskStatus.FAILED
    assert reasoner.plan_calls == 1  # single call — grounding retry loop is removed


@pytest.mark.asyncio
async def test_orchestrator_replans_when_json_plan_drifts_from_markdown_blueprint(tmp_path: Path) -> None:
    # With the PlanningAgent architecture, there is no JSON-vs-markdown grounding
    # critique loop. create_plan() is called once; if the step targets a file that
    # conflicts with what the tool step actually patches, execution fails.
    real_workspace = tmp_path / "real"
    (real_workspace / "services/agentd-py/agentd/api").mkdir(parents=True)
    (real_workspace / "services/agentd-py/agentd/storage").mkdir(parents=True)
    (real_workspace / "services/agentd-py/agentd/api/routes.py").write_text("router = object()\n", encoding="utf-8")
    (real_workspace / "services/agentd-py/agentd/storage/base.py").write_text("class TaskStore:\n    pass\n", encoding="utf-8")

    store = InMemoryTaskStore()
    task = TaskRecord(
        task_id="task-blueprint-drift",
        goal="Update endpoint behavior",
        workspace_path=str(real_workspace),
    )
    await store.create(task)

    reasoner = MarkdownBlueprintReasoner()
    orchestrator = AgentOrchestrator(
        store=store,
        reasoning_engine=reasoner,
        validator=PassValidator(),
        patch_engine=PatchEngine(),
        workspace_manager=ShadowWorkspaceManager(root_path=tmp_path / "shadows"),
    )

    initialized = await orchestrator.run_task(task.task_id)
    assert initialized.status == TaskStatus.AWAITING_PLAN_APPROVAL

    result = await orchestrator.continue_task(task.task_id, feedback=None)

    # Plan is generated once; execution fails because MarkdownBlueprintReasoner's
    # create_tool_step() patches routes.py but the drifted plan only allows storage/base.py.
    assert result.status == TaskStatus.FAILED
    assert reasoner.plan_calls == 1


@pytest.mark.asyncio
async def test_orchestrator_allows_missing_target_when_intent_is_new(tmp_path: Path) -> None:
    real_workspace = tmp_path / "real"
    real_workspace.mkdir(parents=True)

    store = InMemoryTaskStore()
    task = TaskRecord(
        task_id="task-new-intent",
        goal="Add new API regression test file",
        workspace_path=str(real_workspace),
    )
    await store.create(task)

    reasoner = NewFileIntentReasoner()
    orchestrator = AgentOrchestrator(
        store=store,
        reasoning_engine=reasoner,
        validator=PassValidator(),
        patch_engine=PatchEngine(),
        workspace_manager=ShadowWorkspaceManager(root_path=tmp_path / "shadows"),
    )

    initialized = await orchestrator.run_task(task.task_id)
    assert initialized.status == TaskStatus.AWAITING_PLAN_APPROVAL

    result = await orchestrator.continue_task(task.task_id, feedback=None)

    assert result.status == TaskStatus.READY_FOR_REVIEW
    assert "tests/test_task_events_api.py" in result.modified_files
