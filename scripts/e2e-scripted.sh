#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/e2e-scripted.sh [--workspace PATH] [--port PORT] [--backend scripted|gemini] [--gemini-model MODEL] [--goal TEXT] [--open-vscode] [--skip-client-tests] [--skip-build]

What it does:
1. Runs editor-client tests (includes polling null-diagnostic regression test).
2. Builds editor-client and vscode-extension runtime artifacts.
3. Builds an index snapshot for the workspace.
4. Starts agentd-py with the selected reasoning backend.
5. Creates a task, polls until READY_FOR_REVIEW/terminal.
6. Accepts patch if review is ready and verifies file promotion.
7. Optionally opens VS Code Extension Development Host for manual UI check.

Defaults:
  --workspace   /Users/pradeepkumar/projects/AI editor/workspaces/typescript-language-server
  --port        8000
  --backend     scripted
  --gemini-model gemini-3-flash-preview
EOF
}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE="${ROOT}/workspaces/typescript-language-server"
PORT="8000"
BACKEND="scripted"
GEMINI_MODEL="gemini-3-flash-preview"
GOAL="scripted smoke task"
OPEN_VSCODE="0"
SKIP_CLIENT_TESTS="0"
SKIP_BUILD="0"

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
    --backend)
      BACKEND="${2:?missing value for --backend}"
      shift 2
      ;;
    --gemini-model)
      GEMINI_MODEL="${2:?missing value for --gemini-model}"
      shift 2
      ;;
    --goal)
      GOAL="${2:?missing value for --goal}"
      shift 2
      ;;
    --open-vscode)
      OPEN_VSCODE="1"
      shift
      ;;
    --skip-client-tests)
      SKIP_CLIENT_TESTS="1"
      shift
      ;;
    --skip-build)
      SKIP_BUILD="1"
      shift
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

if [[ "$BACKEND" != "scripted" && "$BACKEND" != "gemini" ]]; then
  echo "Unsupported backend: $BACKEND (expected scripted|gemini)" >&2
  exit 1
fi

if [[ "$BACKEND" == "gemini" && -z "${GEMINI_API_KEY:-}" && -z "${GOOGLE_API_KEY:-}" ]]; then
  echo "Gemini backend selected, but GEMINI_API_KEY/GOOGLE_API_KEY is not set." >&2
  exit 1
fi

for bin in curl cargo python3 npm; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "Missing required command: $bin" >&2
    exit 1
  fi
done

if [[ ! -d "$WORKSPACE" ]]; then
  echo "Workspace not found: $WORKSPACE" >&2
  exit 1
fi

AGENTD_DIR="${ROOT}/services/agentd-py"
INDEXER_MANIFEST="${ROOT}/services/indexer-rs/Cargo.toml"
AGENTD_PYTHON="${AGENTD_DIR}/.venv/bin/python"

if [[ ! -x "$AGENTD_PYTHON" ]]; then
  echo "Missing agentd virtualenv python: $AGENTD_PYTHON" >&2
  echo "Create it first: cd \"$AGENTD_DIR\" && python3 -m venv .venv && source .venv/bin/activate && pip install -e .[dev]" >&2
  exit 1
fi

E2E_ROOT="${ROOT}/.tmp/e2e-scripted"
mkdir -p "$E2E_ROOT"
RUN_ID="$(date +%Y%m%d-%H%M%S)"
RUN_DIR="${E2E_ROOT}/${RUN_ID}"
mkdir -p "$RUN_DIR"

SNAPSHOT_PATH="${WORKSPACE}/.ai-editor/index-snapshot.json"
DB_PATH="${RUN_DIR}/agentd.sqlite3"
SHADOW_ROOT="${RUN_DIR}/shadows"
AGENTD_LOG="${RUN_DIR}/agentd.log"
RESULT_JSON="${RUN_DIR}/result.json"
FINAL_JSON="${RUN_DIR}/final.json"

echo "==> Run directory: $RUN_DIR"
echo "==> Workspace: $WORKSPACE"
echo "==> Port: $PORT"
echo "==> Backend: $BACKEND"
if [[ "$BACKEND" == "gemini" ]]; then
  echo "==> Gemini model: $GEMINI_MODEL"
fi

if [[ "$SKIP_CLIENT_TESTS" != "1" ]]; then
  echo "==> Running editor-client tests (includes polling regression coverage)"
  (
    cd "$ROOT"
    npm run -w @ai-editor/editor-client test
  )
fi

if [[ "$SKIP_BUILD" != "1" ]]; then
  echo "==> Building editor-client and vscode-extension artifacts"
  (
    cd "$ROOT"
    npm run -w @ai-editor/editor-client build
    npm run -w @ai-editor/vscode-extension build
  )
fi

echo "==> Building index snapshot"
mkdir -p "$(dirname "$SNAPSHOT_PATH")"
cargo run --manifest-path "$INDEXER_MANIFEST" --quiet -- \
  index \
  --workspace "$WORKSPACE" \
  --snapshot-path "$SNAPSHOT_PATH" \
  --watch 0

SNAPSHOT_STATS="$(
python3 - "$SNAPSHOT_PATH" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
stats = payload.get("stats", {})
print(
    f"nodes={stats.get('node_count', 0)} "
    f"edges={stats.get('edge_count', 0)} "
    f"diagnostics={stats.get('diagnostic_count', 0)}"
)
PY
)"
echo "==> Snapshot ready: $SNAPSHOT_PATH ($SNAPSHOT_STATS)"

cleanup() {
  if [[ -n "${AGENT_PID:-}" ]]; then
    kill "$AGENT_PID" >/dev/null 2>&1 || true
    wait "$AGENT_PID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

echo "==> Starting agentd-py (${BACKEND} backend)"
if [[ "$BACKEND" == "gemini" ]]; then
  (
    cd "$AGENTD_DIR"
    AI_EDITOR_REASONING_BACKEND=gemini \
    AI_EDITOR_GEMINI_MODEL="$GEMINI_MODEL" \
    AI_EDITOR_DB_PATH="$DB_PATH" \
    AI_EDITOR_SHADOW_ROOT="$SHADOW_ROOT" \
    AI_EDITOR_RETRIEVAL_SNAPSHOT_PATH="$SNAPSHOT_PATH" \
    AI_EDITOR_VALIDATION_COMMANDS_JSON='[{"stage":"syntax","name":"smoke-pass","command":"true"}]' \
    "$AGENTD_PYTHON" -m uvicorn agentd.main:app --port "$PORT"
  ) >"$AGENTD_LOG" 2>&1 &
else
  (
    cd "$AGENTD_DIR"
    AI_EDITOR_REASONING_BACKEND=scripted \
    AI_EDITOR_DB_PATH="$DB_PATH" \
    AI_EDITOR_SHADOW_ROOT="$SHADOW_ROOT" \
    AI_EDITOR_RETRIEVAL_SNAPSHOT_PATH="$SNAPSHOT_PATH" \
    AI_EDITOR_VALIDATION_COMMANDS_JSON='[{"stage":"syntax","name":"smoke-pass","command":"true"}]' \
    "$AGENTD_PYTHON" -m uvicorn agentd.main:app --port "$PORT"
  ) >"$AGENTD_LOG" 2>&1 &
fi
AGENT_PID="$!"

echo "==> Waiting for backend health"
READY="0"
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    READY="1"
    break
  fi
  sleep 0.2
done
if [[ "$READY" != "1" ]]; then
  echo "Backend did not become healthy. Log: $AGENTD_LOG" >&2
  tail -n 120 "$AGENTD_LOG" >&2 || true
  exit 1
fi

echo "==> Creating task"
GOAL_JSON="$(E2E_GOAL="$GOAL" python3 - <<'PY'
import json
import os
print(json.dumps(os.environ["E2E_GOAL"]))
PY
)"
TASK_RESPONSE="$(
curl -fsS -X POST "http://127.0.0.1:${PORT}/v1/tasks" \
  -H 'content-type: application/json' \
  --data-binary @- <<EOF
{"goal":$GOAL_JSON,"workspace_path":"$WORKSPACE","mode":"project_edit"}
EOF
)"
TASK_ID="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["task_id"])' <<<"$TASK_RESPONSE")"
echo "==> Task ID: $TASK_ID"

STATUS=""
for attempt in $(seq 1 120); do
  TASK_JSON="$(curl -fsS "http://127.0.0.1:${PORT}/v1/tasks/${TASK_ID}")"
  STATUS="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])' <<<"$TASK_JSON")"
  echo "    poll[$attempt]: $STATUS"
  case "$STATUS" in
    READY_FOR_REVIEW|SUCCEEDED|FAILED|ABORTED)
      break
      ;;
  esac
  sleep 0.25
done

curl -fsS "http://127.0.0.1:${PORT}/v1/tasks/${TASK_ID}/result" >"$RESULT_JSON"

if [[ "$STATUS" == "READY_FOR_REVIEW" ]]; then
  echo "==> Accepting patch"
  curl -fsS -X POST "http://127.0.0.1:${PORT}/v1/tasks/${TASK_ID}/accept" >"$FINAL_JSON"
else
  cp "$RESULT_JSON" "$FINAL_JSON"
fi

PATCH_OP_COUNT="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1], encoding="utf-8")); p=d.get("patch") or {}; print(len(p.get("patch_ops", [])))' "$RESULT_JSON")"
FINAL_STATUS="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["status"])' "$FINAL_JSON")"

PROMOTED_FILE="${WORKSPACE}/generated.txt"
if [[ -f "$PROMOTED_FILE" ]]; then
  PROMOTED="yes"
else
  PROMOTED="no"
fi

echo
echo "==> E2E summary"
echo "task_id=$TASK_ID"
echo "status_at_poll_stop=$STATUS"
echo "final_status=$FINAL_STATUS"
echo "result_patch_ops=$PATCH_OP_COUNT"
echo "promoted_generated_file=$PROMOTED"
echo "agentd_log=$AGENTD_LOG"
echo "result_json=$RESULT_JSON"
echo "final_json=$FINAL_JSON"

if [[ "$OPEN_VSCODE" == "1" ]]; then
  echo
  echo "==> Opening VS Code Extension Development Host"
  if command -v code >/dev/null 2>&1; then
    code --extensionDevelopmentPath "${ROOT}/apps/vscode-extension" "$WORKSPACE"
  else
    echo "VS Code CLI ('code') not found. Open manually:"
    echo "  code --extensionDevelopmentPath \"${ROOT}/apps/vscode-extension\" \"$WORKSPACE\""
  fi
fi

cat <<'EOF'

Manual UI check for polling warning regression:
1. In Extension Host run "AI Editor: Start Task".
2. Keep the review panel open while task polls.
3. PASS if no warning toast appears containing:
   "Polling failed ... invalid_type ... expected string"
EOF
