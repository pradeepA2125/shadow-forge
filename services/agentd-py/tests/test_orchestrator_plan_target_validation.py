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

    async def create_markdown_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        retrieval_context: dict[str, object],
    ) -> str:
        _ = (task, workspace_path, retrieval_context)
        self.markdown_plan_calls += 1
        return "# Plan\n\n- Update endpoint"

    async def critique_markdown_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        retrieval_context: dict[str, object],
        plan_markdown: str,
    ) -> object:
        _ = (task, workspace_path, retrieval_context, plan_markdown)
        return {"verdict": "pass", "issues": []}

    async def create_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        retrieval_context: dict[str, object],
    ) -> object:
        _ = (task, workspace_path)
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

    async def critique_json_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        retrieval_context: dict[str, object],
        candidate_plan: dict[str, object],
    ) -> object:
        _ = (task, workspace_path, retrieval_context, candidate_plan)
        return {"verdict": "pass", "issues": []}

    async def create_tool_step(
        self,
        step_context: dict[str, object],
        history: list[dict[str, object]],
        tool_definitions: list[dict[str, object]],
    ) -> dict[str, object]:
        _ = (step_context, history, tool_definitions)
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
    ) -> object:
        _ = (task, workspace_path)
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
    async def create_markdown_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        retrieval_context: dict[str, object],
    ) -> str:
        _ = (task, workspace_path, retrieval_context)
        self.markdown_plan_calls += 1
        return "# Plan\n\n- Update `services/agentd-py/agentd/api/routes.py`"

    async def create_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        retrieval_context: dict[str, object],
    ) -> object:
        _ = (task, workspace_path)
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

    async def critique_json_plan(
        self,
        task: TaskRecord,
        workspace_path: str,
        retrieval_context: dict[str, object],
        candidate_plan: dict[str, object],
    ) -> object:
        _ = (task, workspace_path, retrieval_context)
        # Check if plan targets drift from markdown blueprint
        if self.plan_calls == 1:
            # First call has drift - return issues
            return {
                "verdict": "revise",
                "issues": [
                    {
                        "code": "path_prefix_mismatch",
                        "message": "JSON plan target 'services/agentd-py/agentd/storage/base.py' is not part of the approved markdown blueprint.",
                        "file": "services/agentd-py/agentd/storage/base.py",
                        "evidence": "services/agentd-py/agentd/api/routes.py",
                    }
                ]
            }
        # Second call should pass
        return {"verdict": "pass", "issues": []}

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
    ) -> dict[str, object]:
        _ = (step_context, history, tool_definitions)
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
    ) -> object:
        _ = (task, workspace_path, retrieval_context)
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
    ) -> dict[str, object]:
        _ = (step_context, history, tool_definitions)
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

    assert result.status == TaskStatus.READY_FOR_REVIEW
    assert reasoner.markdown_plan_calls == 1
    assert reasoner.plan_calls == 2
    assert "plan_validation_feedback" in reasoner.plan_contexts[1]
    feedback = reasoner.plan_contexts[1]["plan_validation_feedback"]
    assert isinstance(feedback, dict)
    missing_targets = feedback["missing_targets"]
    assert isinstance(missing_targets, list)
    assert missing_targets[0]["target"] == "agentd/api/tasks.py"


@pytest.mark.asyncio
async def test_orchestrator_fails_fast_when_replanned_targets_still_missing(tmp_path: Path) -> None:
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
    assert reasoner.plan_calls == 3  # loop is range(3): initial + 2 repair rounds
    assert any(d.source == "plan_schema_validation" for d in result.diagnostics)
    assert any("steps.0.targets.0.intent" in d.message for d in result.diagnostics)


@pytest.mark.asyncio
async def test_orchestrator_replans_when_json_plan_drifts_from_markdown_blueprint(tmp_path: Path) -> None:
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

    assert result.status == TaskStatus.READY_FOR_REVIEW
    assert reasoner.plan_calls == 2
    feedback = reasoner.plan_contexts[1]["plan_validation_feedback"]
    assert isinstance(feedback, dict)
    grounding_issues = feedback["grounding_issues"]
    assert isinstance(grounding_issues, list)
    assert grounding_issues[0]["code"] == "path_prefix_mismatch"


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
