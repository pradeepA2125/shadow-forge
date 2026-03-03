from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI

from agentd.api.routes import build_router
from agentd.patch.engine import PatchEngine
from agentd.orchestrator.engine import AgentOrchestrator
from agentd.orchestrator.scripted_engine import ScriptedReasoningEngine
from agentd.providers.anthropic_transport import AnthropicJsonTransport
from agentd.providers.gemini_transport import GeminiJsonTransport
from agentd.providers.openai_transport import OpenAIJsonTransport
from agentd.reasoning.contracts import ReasoningEngine
from agentd.reasoning.engine import DefaultReasoningEngine
from agentd.retrieval.artifact_client import RetrievalArtifactClient
from agentd.storage.sqlite_store import SQLiteTaskStore
from agentd.validation.command_validator import CommandValidator
from agentd.workspace.shadow import ShadowWorkspaceManager

app = FastAPI(title="ai-editor agentd-py", version="0.1.0")

database_path = Path(os.getenv("AI_EDITOR_DB_PATH", ".agentd/agentd.sqlite3")).resolve()
shadow_root_path = Path(os.getenv("AI_EDITOR_SHADOW_ROOT", ".agentd/shadows")).resolve()

store = SQLiteTaskStore(database_path=database_path)
workspace_manager = ShadowWorkspaceManager(root_path=shadow_root_path)
patch_engine = PatchEngine()


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _optional_int_env(name: str) -> int | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


reasoning_backend = os.getenv("AI_EDITOR_REASONING_BACKEND", "openai").strip().lower()
reasoning_engine: ReasoningEngine
if reasoning_backend == "scripted":
    reasoning_engine = ScriptedReasoningEngine(
        plan={
            "analysis": "Scaffold run",
            "steps": [{"id": "S1", "goal": "No-op", "targets": ["README.md"], "risk": "low"}],
            "expected_files": ["README.md"],
            "stop_conditions": ["validation passes"],
        },
        patches=[
            {
                "patch_ops": [
                    {
                        "op": "create_file",
                        "file": "generated.txt",
                        "content": "ok",
                        "reason": "demo",
                    }
                ]
            }
        ],
    )
elif reasoning_backend == "anthropic":
    transport = AnthropicJsonTransport(
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        endpoint=os.getenv("AI_EDITOR_ANTHROPIC_ENDPOINT", "https://api.anthropic.com/v1/messages"),
        anthropic_version=os.getenv("AI_EDITOR_ANTHROPIC_VERSION", "2023-06-01"),
        max_tokens=_int_env("AI_EDITOR_ANTHROPIC_MAX_TOKENS", 4096),
        timeout_sec=_float_env("AI_EDITOR_ANTHROPIC_TIMEOUT_SEC", 60.0),
    )
    reasoning_engine = DefaultReasoningEngine(
        model=os.getenv("AI_EDITOR_ANTHROPIC_MODEL", "claude-3-5-sonnet-latest"),
        transport=transport,
    )
elif reasoning_backend == "gemini":
    thinking_level = os.getenv("AI_EDITOR_GEMINI_THINKING_LEVEL")
    thinking_budget = _optional_int_env("AI_EDITOR_GEMINI_THINKING_BUDGET")
    thinking_enabled = _bool_env("AI_EDITOR_GEMINI_THINKING_ENABLED", True)
    if thinking_enabled and thinking_budget is None and not thinking_level:
        # Enable dynamic thinking by default for Gemini backend unless explicitly configured.
        thinking_budget = -1

    transport = GeminiJsonTransport(
        api_key=os.getenv("GEMINI_API_KEY"),
        thinking_enabled=thinking_enabled,
        thinking_budget=thinking_budget,
        thinking_level=thinking_level,
        include_thoughts=_bool_env("AI_EDITOR_GEMINI_INCLUDE_THOUGHTS", False),
    )
    reasoning_engine = DefaultReasoningEngine(
        model=os.getenv("AI_EDITOR_GEMINI_MODEL", "gemini-3-flash-preview"),
        transport=transport,
    )
else:
    transport = OpenAIJsonTransport()
    reasoning_engine = DefaultReasoningEngine(
        model=os.getenv("AI_EDITOR_OPENAI_MODEL", "gpt-5"),
        transport=transport,
    )

validator = CommandValidator.from_env()
retrieval_client = RetrievalArtifactClient.from_env()
orchestrator = AgentOrchestrator(
    store=store,
    reasoning_engine=reasoning_engine,
    validator=validator,
    patch_engine=patch_engine,
    workspace_manager=workspace_manager,
    retrieval_client=retrieval_client,
)

app.include_router(build_router(store, orchestrator, workspace_manager))


@app.get("/health")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}
