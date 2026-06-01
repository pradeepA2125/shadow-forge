"""Test-only FastAPI app factory for chat integration tests.

Kept separate from main.py to avoid triggering module-level side effects
(provider transport init, DB connection, etc.) during test collection.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI


def build_app(
    workspace_path: str,
    *,
    draft_conventions_responses: list[dict] | None = None,
) -> FastAPI:
    """Construct a self-contained FastAPI app for a given workspace path.

    draft_conventions_responses: pre-canned LLM responses for the env profile
    build path; threaded through to ScriptedReasoningEngine.
    """
    from agentd.api.routes import build_router
    from agentd.chat.agent import ChatAgent
    from agentd.chat.storage import ChatThreadStore
    from agentd.domain.models import ValidationResult
    from agentd.orchestrator.engine import AgentOrchestrator
    from agentd.orchestrator.scripted_engine import ScriptedReasoningEngine
    from agentd.patch.engine import PatchEngine
    from agentd.storage.in_memory import InMemoryTaskStore
    from agentd.workspace.shadow import ShadowWorkspaceManager

    class _AlwaysPassValidator:
        async def run_touched(self, workspace_path: str, touched_files: list) -> ValidationResult:
            return ValidationResult(success=True, diagnostics=[], duration_ms=1)

        async def run(self, workspace_path: str) -> ValidationResult:
            return ValidationResult(success=True, diagnostics=[], duration_ms=1)

    class _NullTransport:
        async def generate_text(self, **_) -> str:
            return "test response"

        async def generate_json(self, *, schema_name, **_) -> dict:
            if schema_name == "explore_step":
                return {"action": "done"}
            return {"intent": "qa", "rationale": "test", "likely_targets": []}

    store = InMemoryTaskStore()
    ws_manager = ShadowWorkspaceManager(Path(workspace_path) / ".agentd" / "shadows")
    chat_store = ChatThreadStore(Path(workspace_path) / "chat.db")
    scripted_engine = ScriptedReasoningEngine(
        plan={"analysis": "", "steps": []},
        patches=[],
        draft_conventions_responses=draft_conventions_responses,
    )
    orchestrator = AgentOrchestrator(
        store=store,
        reasoning_engine=scripted_engine,
        validator=_AlwaysPassValidator(),
        patch_engine=PatchEngine(),
        workspace_manager=ws_manager,
    )
    agent = ChatAgent(
        workspace_path=workspace_path,
        transport=_NullTransport(),
        model="test-model",
        thread_store=chat_store,
        orchestrator=None,
        broadcaster=orchestrator.broadcaster,
    )
    router = build_router(store, orchestrator, ws_manager, None, agent)
    app = FastAPI()
    app.include_router(router)
    return app
