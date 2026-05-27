#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  scripts/stress/start-backend.sh [--workspace PATH] [--port N] [--out-dir PATH] [--log-dir PATH]
                                  [--backend NAME] [--model MODEL] [--validation-profile smoke|full|strict|none]
                                  [--artifacts-root PATH] [--tool-loop on|off] [--semantic on|off]
                                  [--agentd-dir PATH]

Defaults:
  workspace:   repository root
  port:        8000
  out-dir:     <repo>/.tmp/stress-<timestamp>
  backend:     auto-detected from available provider keys
  model:       provider-specific default
  artifacts:   <workspace>/.agentd/artifacts
  tool-loop:   on  (ReAct tool-use loop per step)
  semantic:    on  (vector index retrieval; pass --semantic off to disable)
  agentd-dir:  <repo>/services/agentd-py  (override to use a worktree)
USAGE
}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORKSPACE="$ROOT"
PORT="8000"
OUT_DIR="$ROOT/.tmp/stress-$(date +%Y%m%d-%H%M%S)"
LOG_DIR=""
BACKEND=""
MODEL=""
VALIDATION_PROFILE="full"
ARTIFACTS_ROOT=""
TOOL_LOOP=""
SEMANTIC=""
AGENTD_DIR_OVERRIDE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --workspace)
      WORKSPACE="${2:?missing value for --workspace}"
      shift 2
      ;;
    --port)
      PORT="${2:?missing value for --port}"
      shift 2
      ;;
    --out-dir)
      OUT_DIR="${2:?missing value for --out-dir}"
      shift 2
      ;;
    --log-dir)
      LOG_DIR="${2:?missing value for --log-dir}"
      shift 2
      ;;
    --backend)
      BACKEND="${2:?missing value for --backend}"
      shift 2
      ;;
    --model)
      MODEL="${2:?missing value for --model}"
      shift 2
      ;;
    --validation-profile)
      VALIDATION_PROFILE="${2:?missing value for --validation-profile}"
      shift 2
      ;;
    --artifacts-root)
      ARTIFACTS_ROOT="${2:?missing value for --artifacts-root}"
      shift 2
      ;;
    --tool-loop)
      TOOL_LOOP="${2:?missing value for --tool-loop}"
      shift 2
      ;;
    --semantic)
      SEMANTIC="${2:?missing value for --semantic}"
      shift 2
      ;;
    --agentd-dir)
      AGENTD_DIR_OVERRIDE="${2:?missing value for --agentd-dir}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ ! -d "$WORKSPACE" ]]; then
  echo "Workspace directory does not exist: $WORKSPACE" >&2
  exit 1
fi

AGENTD_DIR="${AGENTD_DIR_OVERRIDE:-$ROOT/services/agentd-py}"
MAIN_VENV_DIR="$ROOT/services/agentd-py/.venv"

# Venv for Python: prefer one local to AGENTD_DIR, fall back to main repo's venv
VENV_DIR="$AGENTD_DIR/.venv"
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  VENV_DIR="$MAIN_VENV_DIR"
fi
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "Missing virtualenv python in $AGENTD_DIR/.venv or $MAIN_VENV_DIR" >&2
  echo "Run bootstrap in the main repo first." >&2
  exit 1
fi

# Uvicorn: prefer local venv, fall back to main repo's venv
if [[ -x "$VENV_DIR/bin/uvicorn" ]]; then
  UVICORN_BIN="$VENV_DIR/bin/uvicorn"
elif [[ -x "$MAIN_VENV_DIR/bin/uvicorn" ]]; then
  UVICORN_BIN="$MAIN_VENV_DIR/bin/uvicorn"
else
  echo "uvicorn not found in $VENV_DIR/bin or $MAIN_VENV_DIR/bin" >&2
  exit 1
fi

resolve_backend() {
  if [[ -n "$BACKEND" ]]; then
    printf '%s' "$BACKEND"
    return
  fi
  if [[ -n "${GEMINI_API_KEY:-${GOOGLE_API_KEY:-}}" ]]; then
    printf 'gemini'
  elif [[ -n "${GROQ_API_KEY:-}" ]]; then
    printf 'groq'
  elif [[ -n "${OPENROUTER_API_KEY:-}" ]]; then
    printf 'openrouter'
  elif [[ -n "${WATSONX_API_KEY:-}" ]]; then
    printf 'watsonx'
  elif [[ -n "${OPENAI_API_KEY:-}" ]]; then
    printf 'openai'
  elif [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
    printf 'anthropic'
  elif [[ -n "${HF_TOKEN:-}" ]]; then
    printf 'huggingface'
  else
    printf 'scripted'
  fi
}

resolve_default_model() {
  case "$1" in
    scripted) printf 'scripted' ;;
    gemini) printf '%s' "${AI_EDITOR_GEMINI_MODEL:-gemini-3.1-flash-lite-preview}" ;;
    groq) printf '%s' "${AI_EDITOR_GROQ_MODEL:-openai/gpt-oss-120b}" ;;
    openrouter) printf '%s' "${AI_EDITOR_OPENROUTER_MODEL:-stepfun/step-3.5-flash:free}" ;;
    watsonx) printf '%s' "${AI_EDITOR_WATSONX_MODEL:-ibm/granite-3-8b-instruct}" ;;
    openai) printf '%s' "${AI_EDITOR_OPENAI_MODEL:-gpt-5}" ;;
    anthropic) printf '%s' "${AI_EDITOR_ANTHROPIC_MODEL:-claude-3-5-sonnet-latest}" ;;
    huggingface) printf '%s' "${AI_EDITOR_HUGGINGFACE_MODEL:-deepseek-ai/DeepSeek-R1:fastest}" ;;
    ollama) printf '%s' "${AI_EDITOR_OLLAMA_MODEL:-qwen3:latest}" ;;
    turboquant) printf '%s' "${AI_EDITOR_TURBOQUANT_MODEL:-devstral-small-2:24b-q4_k_xl}" ;;
    *)
      echo "Unsupported backend: $1" >&2
      exit 1
      ;;
  esac
}

resolve_validation_commands() {
  case "$VALIDATION_PROFILE" in
    none)
      printf '[]'
      ;;
    smoke)
      printf '[{"stage":"syntax","name":"smoke-pass","command":"true"}]'
      ;;
    full)
      if [[ -n "${AI_EDITOR_VALIDATION_COMMANDS_JSON:-}" ]]; then
        printf '%s' "$AI_EDITOR_VALIDATION_COMMANDS_JSON"
      else
        # Let CommandValidator auto-detect project commands instead of bypassing
        # validation with a no-op command.
        printf '__AUTO_DETECT__'
      fi
      ;;
    strict)
      if [[ -n "${AI_EDITOR_VALIDATION_COMMANDS_JSON:-}" ]]; then
        printf '%s' "$AI_EDITOR_VALIDATION_COMMANDS_JSON"
      else
        printf '__STRICT_MISSING__'
      fi
      ;;
    *)
      echo "Unsupported validation profile: $VALIDATION_PROFILE" >&2
      exit 1
      ;;
  esac
}

BACKEND="$(resolve_backend)"
if [[ -z "$MODEL" ]]; then
  MODEL="$(resolve_default_model "$BACKEND")"
fi
if [[ -z "$LOG_DIR" ]]; then
  LOG_DIR="$OUT_DIR/logs"
fi
if [[ -z "$ARTIFACTS_ROOT" ]]; then
  ARTIFACTS_ROOT="$WORKSPACE/.agentd/artifacts"
fi

mkdir -p "$OUT_DIR" "$LOG_DIR" "$WORKSPACE/.agentd" "$ARTIFACTS_ROOT"
SNAPSHOT_PATH="$WORKSPACE/.ai-editor/index-snapshot.json"
VECTOR_INDEX_PATH="$WORKSPACE/.ai-editor/vector-index"
LOG_FILE="$LOG_DIR/agentd.log"

# Resolve tool-loop flag — CLI arg overrides env, default on
if [[ -z "$TOOL_LOOP" ]]; then
  TOOL_LOOP="${AI_EDITOR_TOOL_LOOP_ENABLED:-true}"
fi
case "$TOOL_LOOP" in
  on|1|true|yes) TOOL_LOOP_VALUE="true" ;;
  off|0|false|no) TOOL_LOOP_VALUE="false" ;;
  *) TOOL_LOOP_VALUE="$TOOL_LOOP" ;;
esac

# Resolve semantic flag — CLI arg overrides env, default on
if [[ -z "$SEMANTIC" ]]; then
  SEMANTIC="${AI_EDITOR_SEMANTIC_RETRIEVAL:-true}"
fi
case "$SEMANTIC" in
  on|1|true|yes) SEMANTIC_VALUE="true" ;;
  *) SEMANTIC_VALUE="false" ;;
esac

VALIDATION_COMMANDS_JSON="$(resolve_validation_commands)"

if [[ "$VALIDATION_COMMANDS_JSON" == "__STRICT_MISSING__" ]]; then
  echo "strict validation profile requires AI_EDITOR_VALIDATION_COMMANDS_JSON to be set" >&2
  exit 1
fi

case "$BACKEND" in
  gemini)
    if [[ -z "${GEMINI_API_KEY:-${GOOGLE_API_KEY:-}}" ]]; then
      echo "GEMINI_API_KEY or GOOGLE_API_KEY is required for gemini backend" >&2
      exit 1
    fi
    ;;
  groq)
    if [[ -z "${GROQ_API_KEY:-}" ]]; then
      echo "GROQ_API_KEY is required for groq backend" >&2
      exit 1
    fi
    ;;
  openrouter)
    if [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
      echo "OPENROUTER_API_KEY is required for openrouter backend" >&2
      exit 1
    fi
    ;;
  watsonx)
    if [[ -z "${WATSONX_API_KEY:-}" ]]; then
      echo "WATSONX_API_KEY is required for watsonx backend" >&2
      exit 1
    fi
    ;;
  openai)
    if [[ -z "${OPENAI_API_KEY:-}" ]]; then
      echo "OPENAI_API_KEY is required for openai backend" >&2
      exit 1
    fi
    ;;
  anthropic)
    if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
      echo "ANTHROPIC_API_KEY is required for anthropic backend" >&2
      exit 1
    fi
    ;;
  huggingface)
    if [[ -z "${HF_TOKEN:-}" ]]; then
      echo "HF_TOKEN is required for huggingface backend" >&2
      exit 1
    fi
    ;;
  ollama)
    ;;
  turboquant)
    ;;
  scripted)
    ;;
  *)
    echo "Unsupported backend: $BACKEND" >&2
    exit 1
    ;;
esac

echo "==> starting backend"
echo "agentd_dir=$AGENTD_DIR"
echo "venv_dir=$VENV_DIR"
echo "uvicorn_bin=$UVICORN_BIN"
echo "workspace=$WORKSPACE"
echo "port=$PORT"
echo "backend=$BACKEND"
echo "model=$MODEL"
echo "snapshot=$SNAPSHOT_PATH"
echo "db_path=$WORKSPACE/.agentd/agentd.sqlite3"
echo "shadow_root=$WORKSPACE/.agentd/shadows"
echo "artifacts_root=$ARTIFACTS_ROOT"
echo "validation_profile=$VALIDATION_PROFILE"
if [[ "$VALIDATION_COMMANDS_JSON" == "__AUTO_DETECT__" ]]; then
  echo "validation_commands=auto-detect"
else
  echo "validation_commands=configured"
fi
echo "tool_loop=$TOOL_LOOP_VALUE"
echo "semantic=$SEMANTIC_VALUE"
echo "vector_index=$VECTOR_INDEX_PATH"
echo "log_file=$LOG_FILE"

# Start uvicorn in the background so we can wait for it to be ready before
# pre-warming the semantic index (guaranteeing no cold-start on the first task).
(
  cd "$AGENTD_DIR"
  export AI_EDITOR_REASONING_BACKEND="$BACKEND"
  export AI_EDITOR_WORKSPACE_PATH="$WORKSPACE"
  export AI_EDITOR_DB_PATH="$WORKSPACE/.agentd/agentd.sqlite3"
  export AI_EDITOR_SHADOW_ROOT="$WORKSPACE/.agentd/shadows"
  export AI_EDITOR_RETRIEVAL_SNAPSHOT_PATH="$SNAPSHOT_PATH"
  export AI_EDITOR_ARTIFACTS_ROOT="$ARTIFACTS_ROOT"
  if [[ "$VALIDATION_COMMANDS_JSON" == "__AUTO_DETECT__" ]]; then
    unset AI_EDITOR_VALIDATION_COMMANDS_JSON
  else
    export AI_EDITOR_VALIDATION_COMMANDS_JSON="$VALIDATION_COMMANDS_JSON"
  fi

  # Agentic tool-loop flags (Phase 5+)
  export AI_EDITOR_TOOL_LOOP_ENABLED="$TOOL_LOOP_VALUE"
  export AI_EDITOR_TOOL_RESULT_MAX_CHARS="${AI_EDITOR_TOOL_RESULT_MAX_CHARS:-4000}"
  export AI_EDITOR_RIPGREP_CMD="${AI_EDITOR_RIPGREP_CMD:-rg}"
  export AI_EDITOR_SHELL_ALLOWLIST="${AI_EDITOR_SHELL_ALLOWLIST:-pytest,npm,cargo,ruff,mypy,tsc,eslint}"

  # Semantic / vector index (workspace-scoped path)
  export AI_EDITOR_SEMANTIC_RETRIEVAL="$SEMANTIC_VALUE"
  export AI_EDITOR_VECTOR_INDEX_PATH="$VECTOR_INDEX_PATH"
  export AI_EDITOR_EMBEDDING_MODEL="${AI_EDITOR_EMBEDDING_MODEL:-BAAI/bge-small-en-v1.5}"
  export AI_EDITOR_EMBED_BATCH_SIZE="${AI_EDITOR_EMBED_BATCH_SIZE:-64}"

  case "$BACKEND" in
    gemini)
      export AI_EDITOR_GEMINI_MODEL="$MODEL"
      ;;
    groq)
      export AI_EDITOR_GROQ_MODEL="$MODEL"
      ;;
    openrouter)
      export AI_EDITOR_OPENROUTER_MODEL="$MODEL"
      ;;
    watsonx)
      export AI_EDITOR_WATSONX_MODEL="$MODEL"
      export WATSONX_URL="${WATSONX_URL:-https://us-south.ml.cloud.ibm.com}"
      ;;
    openai)
      export AI_EDITOR_OPENAI_MODEL="$MODEL"
      ;;
    anthropic)
      export AI_EDITOR_ANTHROPIC_MODEL="$MODEL"
      ;;
    huggingface)
      export AI_EDITOR_HUGGINGFACE_MODEL="$MODEL"
      ;;
    ollama)
      export AI_EDITOR_OLLAMA_MODEL="$MODEL"
      ;;
    turboquant)
      export AI_EDITOR_TURBOQUANT_MODEL="$MODEL"
      export TURBOQUANT_HOST="${TURBOQUANT_HOST:-http://localhost:11435}"
      ;;
    scripted)
      ;;
  esac

  source ".venv/bin/activate" 2>/dev/null || source "$MAIN_VENV_DIR/bin/activate"
  uvicorn agentd.main:app --port "$PORT" --reload 2>&1 | tee "$LOG_FILE"
) &
_SERVER_PID=$!

# Wait for backend to become healthy.
_health_url="http://localhost:${PORT}/health"
echo "==> waiting for backend on port $PORT ..."
for _i in $(seq 1 60); do
  if curl -sf "$_health_url" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
if ! curl -sf "$_health_url" >/dev/null 2>&1; then
  echo "Backend did not become healthy within 60 s" >&2
  kill "$_SERVER_PID" 2>/dev/null || true
  exit 1
fi
echo "==> backend healthy"

# Pre-warm the semantic index synchronously — no task can be submitted until
# this completes, so the first task is guaranteed to have a warm index.
if [[ "$SEMANTIC_VALUE" == "true" ]]; then
  _build_url="http://localhost:${PORT}/v1/index/build"
  _status_url="http://localhost:${PORT}/v1/index/status"
  echo "==> semantic index pre-warm: triggering build for $WORKSPACE ..."
  if ! curl -sf -X POST "$_build_url" \
      -H "Content-Type: application/json" \
      -d "{\"workspace_path\": \"$WORKSPACE\"}" >/dev/null; then
    echo "==> semantic index pre-warm: trigger failed (non-fatal, check backend log)" >&2
  else
    echo "==> semantic index pre-warm: waiting for completion ..."
    for _j in $(seq 1 120); do
      _building=$(curl -sf "$_status_url" \
        | python3 -c "import sys,json; print(json.load(sys.stdin).get('building', True))" 2>/dev/null \
        || echo "True")
      if [[ "$_building" == "False" ]]; then
        echo "==> semantic index pre-warm: ready"
        break
      fi
      sleep 1
    done
  fi
fi

# Self-updating index: launch the incremental indexer watcher. It re-indexes changed source
# files → rewrites the snapshot (atomic) → notifies the backend via AI_EDITOR_BACKEND_URL, which
# delta-re-embeds. LSP is off here — embedding only needs the tree-sitter symbol graph, and a
# full LSP (rust-analyzer/pyright) is memory-heavy.
_INDEXER_BIN="$AGENTD_DIR/../indexer-rs/target/release/ai-editor-indexer"
_WATCHER_PID=""
if [[ "$SEMANTIC_VALUE" == "true" && -x "$_INDEXER_BIN" ]]; then
  AI_EDITOR_BACKEND_URL="http://localhost:${PORT}" AI_EDITOR_LSP_ENABLED=false \
    "$_INDEXER_BIN" index --workspace "$WORKSPACE" --snapshot-path "$SNAPSHOT_PATH" --watch true \
    >> "$LOG_DIR/indexer-watch.log" 2>&1 &
  _WATCHER_PID=$!
  echo "==> indexer watch started (self-updating index): pid=$_WATCHER_PID log=$LOG_DIR/indexer-watch.log"
elif [[ "$SEMANTIC_VALUE" == "true" ]]; then
  echo "==> indexer watch NOT started — binary missing: $_INDEXER_BIN (build: cargo build --release in services/indexer-rs)" >&2
fi

# Don't orphan child processes (backend + watcher) on exit/interrupt.
trap '[[ -n "$_WATCHER_PID" ]] && kill "$_WATCHER_PID" 2>/dev/null; kill "$_SERVER_PID" 2>/dev/null' EXIT INT TERM

echo "==> backend ready — submitting tasks is now safe"
wait "$_SERVER_PID"
