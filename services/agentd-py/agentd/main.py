from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Attach handlers to the agentd logger directly so --reload doesn't suppress
# them (basicConfig is a no-op when uvicorn already owns the root logger).
_agentd_logger = logging.getLogger("agentd")
_agentd_logger.setLevel(logging.INFO)
if not _agentd_logger.handlers:
    _fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%H:%M:%S")
    _h_stdout = logging.StreamHandler(sys.stdout)
    _h_stdout.setFormatter(_fmt)
    _agentd_logger.addHandler(_h_stdout)
    # Also write to a file so logs are tailable regardless of how the server was started.
    _log_file = Path(os.environ.get("AI_EDITOR_LOG_FILE", ".agentd/agentd.log"))
    _log_file.parent.mkdir(parents=True, exist_ok=True)
    _h_file = logging.FileHandler(_log_file)
    _h_file.setFormatter(_fmt)
    _agentd_logger.addHandler(_h_file)
_agentd_logger.propagate = False

from fastapi import FastAPI

from agentd.api.routes import build_router
from agentd.patch.engine import PatchEngine
from agentd.domain.models import ScopePolicy, ScopeRemember, ScopeTrigger, ShellPolicy
from agentd.orchestrator.engine import AgentOrchestrator
from agentd.orchestrator.scripted_engine import ScriptedReasoningEngine
from agentd.providers.anthropic_transport import AnthropicJsonTransport
from agentd.providers.gemini_transport import GeminiJsonTransport
from agentd.providers.groq_transport import GroqJsonTransport
from agentd.providers.huggingface_transport import HuggingFaceJsonTransport
from agentd.providers.ollama_transport import OllamaJsonTransport
from agentd.providers.turboquant_transport import TurboQuantTransport
from agentd.providers.openai_transport import OpenAIJsonTransport
from agentd.reasoning.contracts import ReasoningEngine
from agentd.reasoning.engine import DefaultReasoningEngine
from agentd.retrieval.artifact_client import RetrievalArtifactClient
from agentd.runtime.adapters import build_evidence_adapter, build_planning_adapter
from agentd.storage.sqlite_store import SQLiteTaskStore
from agentd.validation.command_validator import CommandValidator
from agentd.workspace.shadow import ShadowWorkspaceManager
from agentd.providers.openrouter_transport import OpenRouterJsonTransport
from agentd.providers.watsonx_transport import WatsonxJsonTransport


app = FastAPI(title="ai-editor agentd-py", version="0.1.0")

database_path = Path(os.getenv("AI_EDITOR_DB_PATH", ".agentd/agentd.sqlite3")).resolve()
shadow_root_path = Path(os.getenv("AI_EDITOR_SHADOW_ROOT", ".agentd/shadows")).resolve()
ast_cutover_mode = os.getenv("AI_EDITOR_AST_CUTOVER_MODE", "hard").strip().lower()
if ast_cutover_mode != "hard":
    msg = (
        "AI_EDITOR_AST_CUTOVER_MODE must be 'hard' for Phase 1 reliability "
        f"(received: {ast_cutover_mode!r})"
    )
    raise RuntimeError(msg)

store = SQLiteTaskStore(database_path=database_path)
raw_checkpoint_retention = os.getenv("AI_EDITOR_CHECKPOINT_RETENTION_TASKS", "20")
try:
    checkpoint_retention_tasks = int(raw_checkpoint_retention)
except ValueError:
    checkpoint_retention_tasks = 20
workspace_manager = ShadowWorkspaceManager(
    root_path=shadow_root_path,
    checkpoint_retention_tasks=checkpoint_retention_tasks,
)
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
            "steps": [
                {
                    "id": "S1",
                    "goal": "Create scaffold file",
                    "targets": [{"path": "generated.txt", "intent": "new"}],
                    "risk": "low",
                }
            ],
            "expected_files": ["generated.txt"],
            "stop_conditions": ["validation passes"],
        },
        patches=[
            {
                "candidates": [
                    {
                        "candidate_id": "c1",
                        "patch_ops": [
                            {
                                "op": "create_file",
                                "file": "generated.txt",
                                "content": "ok",
                                "reason": "demo",
                            }
                        ],
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
        # Default to high reasoning for Gemini 3.x models (thinking_level, not thinking_budget).
        # thinking_budget=-1 is the Gemini 2.5 dynamic-budget API; 3.x models use thinking_level.
        thinking_level = "high"

    transport = GeminiJsonTransport(
        api_key=os.getenv("GEMINI_API_KEY"),
        thinking_enabled=thinking_enabled,
        thinking_budget=thinking_budget,
        thinking_level=thinking_level,
        include_thoughts=_bool_env("AI_EDITOR_GEMINI_INCLUDE_THOUGHTS", False),
        timeout_sec=_float_env("AI_EDITOR_GEMINI_TIMEOUT_SEC", 120.0),
        max_retries=_int_env("AI_EDITOR_GEMINI_MAX_RETRIES", 4),
    )
    reasoning_engine = DefaultReasoningEngine(
        model=os.getenv("AI_EDITOR_GEMINI_MODEL", "gemini-3-flash-preview"),
        transport=transport,
    )
elif reasoning_backend == "huggingface":
    transport = HuggingFaceJsonTransport(
        api_key=os.getenv("HF_TOKEN"),
        max_new_tokens=_int_env("AI_EDITOR_HUGGINGFACE_MAX_NEW_TOKENS", 4096),
        seed=_optional_int_env("AI_EDITOR_HUGGINGFACE_SEED"),
        timeout_sec=_float_env("AI_EDITOR_HUGGINGFACE_TIMEOUT_SEC", 60.0),
    )
    reasoning_engine = DefaultReasoningEngine(
        model=os.getenv(
            "AI_EDITOR_HUGGINGFACE_MODEL",
            "deepseek-ai/DeepSeek-R1:fastest",
        ),
        transport=transport,
    )
elif reasoning_backend == "groq":
    transport = GroqJsonTransport(
        api_key=os.getenv("GROQ_API_KEY"),
        endpoint=os.getenv("AI_EDITOR_GROQ_ENDPOINT"),
        max_tokens=_int_env("AI_EDITOR_GROQ_MAX_TOKENS", 4096),
        timeout_sec=_float_env("AI_EDITOR_GROQ_TIMEOUT_SEC", 60.0),
        max_retries=_int_env("AI_EDITOR_GROQ_MAX_RETRIES", 4),
    )
    reasoning_engine = DefaultReasoningEngine(
        model=os.getenv("AI_EDITOR_GROQ_MODEL", "openai/gpt-oss-120b"),
        transport=transport,
    )
elif reasoning_backend == "openrouter":

    transport = OpenRouterJsonTransport(
        api_key=os.getenv("OPENROUTER_API_KEY"),
        max_tokens=_int_env("AI_EDITOR_OPENROUTER_MAX_TOKENS", 4096),
        timeout_sec=_float_env("AI_EDITOR_OPENROUTER_TIMEOUT_SEC", 120.0),
        max_retries=_int_env("AI_EDITOR_OPENROUTER_MAX_RETRIES", 4),
    )
    reasoning_engine = DefaultReasoningEngine(
        model=os.getenv(
            "AI_EDITOR_OPENROUTER_MODEL", "stepfun/step-3.5-flash:free"
        ),
        transport=transport,
    )
elif reasoning_backend == "watsonx":
    transport = WatsonxJsonTransport(
        api_key=os.getenv("WATSONX_API_KEY"),
        project_id=os.getenv("WATSONX_PROJECT_ID"),
        url=os.getenv("WATSONX_URL"),
        space_id=os.getenv("WATSONX_SPACE_ID"),
    )
    reasoning_engine = DefaultReasoningEngine(
        model=os.getenv("AI_EDITOR_WATSONX_MODEL", "ibm/granite-3-8b-instruct"),
        transport=transport,
    )
elif reasoning_backend == "ollama":
    transport = OllamaJsonTransport(
        host=os.getenv("OLLAMA_HOST"),
        keep_alive=os.getenv("AI_EDITOR_OLLAMA_KEEP_ALIVE"),
        timeout_sec=_float_env("AI_EDITOR_OLLAMA_TIMEOUT_SEC", 600.0),
        max_retries=_int_env("AI_EDITOR_OLLAMA_MAX_RETRIES", 4),
    )
    reasoning_engine = DefaultReasoningEngine(
        model=os.getenv("AI_EDITOR_OLLAMA_MODEL", "glm-4.7-flash:latest"),
        transport=transport,
    )
elif reasoning_backend == "turboquant":
    transport = TurboQuantTransport.from_env()
    reasoning_engine = DefaultReasoningEngine(
        model=os.getenv("AI_EDITOR_TURBOQUANT_MODEL", "devstral-small-2:24b-q4_k_xl"),
        transport=transport,
    )
else:
    transport = OpenAIJsonTransport()
    reasoning_engine = DefaultReasoningEngine(
        model=os.getenv("AI_EDITOR_OPENAI_MODEL", "gpt-5"),
        transport=transport,
    )

validator = CommandValidator.from_env()
evidence_adapter = build_evidence_adapter(os.getenv("AI_EDITOR_EVIDENCE_ADAPTER", "generic"))
planning_adapter = build_planning_adapter(os.getenv("AI_EDITOR_PLANNING_ADAPTER", "generic"))

_semantic_index: object = None
if _bool_env("AI_EDITOR_SEMANTIC_RETRIEVAL", False):
    try:
        from agentd.retrieval.semantic_index import SemanticIndex
        _semantic_index = SemanticIndex.from_env()
    except ImportError:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "AI_EDITOR_SEMANTIC_RETRIEVAL=true but lancedb/sentence-transformers not installed; "
            "falling back to graph-only retrieval. "
            "Install with: pip install 'ai-editor-agentd[semantic]'"
        )

retrieval_client = RetrievalArtifactClient.from_env(
    evidence_adapter=evidence_adapter,
    semantic_index=_semantic_index,
)
def _scope_policy_env() -> ScopePolicy:
    raw = os.getenv("AI_EDITOR_SCOPE_POLICY", "strict").strip().lower()
    try:
        return ScopePolicy(raw)
    except ValueError:
        return ScopePolicy.STRICT


def _scope_trigger_env() -> ScopeTrigger:
    raw = os.getenv("AI_EDITOR_SCOPE_TRIGGER", "nearby").strip().lower()
    try:
        return ScopeTrigger(raw)
    except ValueError:
        return ScopeTrigger.NEARBY


def _shell_policy_env() -> ShellPolicy:
    raw = os.getenv("AI_EDITOR_SHELL_POLICY", "ask").strip().lower()
    try:
        return ShellPolicy(raw)
    except ValueError:
        return ShellPolicy.ASK


def _scope_remember_env() -> ScopeRemember:
    raw = os.getenv("AI_EDITOR_SCOPE_REMEMBER", "task").strip().lower()
    try:
        return ScopeRemember(raw)
    except ValueError:
        return ScopeRemember.TASK


from agentd.chat.agent import ChatAgent
from agentd.chat.storage import ChatThreadStore

_chat_db_path = Path(os.getenv("AI_EDITOR_CHAT_DB_PATH", ".agentd/chat.sqlite3")).resolve()
_chat_db_path.parent.mkdir(parents=True, exist_ok=True)
_chat_thread_store = ChatThreadStore(_chat_db_path)

orchestrator = AgentOrchestrator(
    store=store,
    reasoning_engine=reasoning_engine,
    validator=validator,
    patch_engine=patch_engine,
    workspace_manager=workspace_manager,
    retrieval_client=retrieval_client,
    planning_adapter=planning_adapter,
    max_attempts_per_step=_int_env("AI_EDITOR_MAX_ATTEMPTS_PER_STEP", 3),
    step_scoped_mode=_bool_env("AI_EDITOR_STEP_SCOPED_MODE", True),
    patch_candidate_count=_int_env("AI_EDITOR_PATCH_CANDIDATE_COUNT", 3),
    scope_policy=_scope_policy_env(),
    scope_trigger=_scope_trigger_env(),
    scope_remember=_scope_remember_env(),
    scope_timeout_sec=_float_env("AI_EDITOR_SCOPE_TIMEOUT_SEC", 600.0),
    shell_policy=_shell_policy_env(),
    command_decision_timeout_sec=_float_env("AI_EDITOR_COMMAND_DECISION_TIMEOUT_SEC", 0.0),
    chat_store=_chat_thread_store,
)

# workspace_path for ChatAgent — the real repo being edited; defaults to cwd if not set
_chat_workspace_path = os.getenv("AI_EDITOR_WORKSPACE_PATH", str(Path.cwd()))
_BACKEND_MODEL_ENVVAR: dict[str, str] = {
    "anthropic":   "AI_EDITOR_ANTHROPIC_MODEL",
    "gemini":      "AI_EDITOR_GEMINI_MODEL",
    "huggingface": "AI_EDITOR_HUGGINGFACE_MODEL",
    "groq":        "AI_EDITOR_GROQ_MODEL",
    "openrouter":  "AI_EDITOR_OPENROUTER_MODEL",
    "watsonx":     "AI_EDITOR_WATSONX_MODEL",
    "ollama":      "AI_EDITOR_OLLAMA_MODEL",
    "turboquant":  "AI_EDITOR_TURBOQUANT_MODEL",
    "openai":      "AI_EDITOR_OPENAI_MODEL",
}
_chat_model = os.getenv(
    _BACKEND_MODEL_ENVVAR.get(reasoning_backend, "AI_EDITOR_OPENAI_MODEL"), "gpt-4o"
)

# scripted backend has no provider transport — ChatAgent requires a real one
_chat_agent = ChatAgent(
    workspace_path=_chat_workspace_path,
    transport=transport,  # type: ignore[possibly-unbound]  # defined for all real backends
    model=_chat_model,
    thread_store=_chat_thread_store,
    orchestrator=orchestrator,
    broadcaster=orchestrator.broadcaster,
    retrieval_client=retrieval_client,
) if reasoning_backend != "scripted" else None

app.include_router(build_router(store, orchestrator, workspace_manager, retrieval_client, _chat_agent))



@app.get("/health")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}
