#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  scripts/stress/bootstrap.sh [--workspace PATH] [--snapshot-path PATH] [--skip-npm] [--skip-python] [--skip-index]

Defaults:
  workspace: repository root
  snapshot:  <workspace>/.ai-editor/index-snapshot.json
USAGE
}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Shared indexer-build helpers (indexer_bin_path, ensure_indexer_binary).
# shellcheck source=scripts/stress/_indexer.sh
source "$ROOT/scripts/stress/_indexer.sh"

WORKSPACE="$ROOT"
SNAPSHOT_PATH=""
SKIP_NPM="0"
SKIP_PYTHON="0"
SKIP_INDEX="0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --workspace)
      WORKSPACE="${2:?missing value for --workspace}"
      shift 2
      ;;
    --snapshot-path)
      SNAPSHOT_PATH="${2:?missing value for --snapshot-path}"
      shift 2
      ;;
    --skip-npm)
      SKIP_NPM="1"
      shift
      ;;
    --skip-python)
      SKIP_PYTHON="1"
      shift
      ;;
    --skip-index)
      SKIP_INDEX="1"
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

if [[ -z "$SNAPSHOT_PATH" ]]; then
  SNAPSHOT_PATH="$WORKSPACE/.ai-editor/index-snapshot.json"
fi

for cmd in npm python3 cargo; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Missing required command: $cmd" >&2
    exit 1
  fi
done

if [[ ! -d "$WORKSPACE" ]]; then
  echo "Workspace directory does not exist: $WORKSPACE" >&2
  exit 1
fi

echo "==> bootstrap workspace=$WORKSPACE"
echo "==> snapshot_path=$SNAPSHOT_PATH"

if [[ "$SKIP_NPM" != "1" ]]; then
  echo "==> npm install/build"
  (
    cd "$WORKSPACE"
    npm install
    npm run -w @ai-editor/editor-client build
    npm run -w @ai-editor/vscode-extension build
  )

  test -f "$WORKSPACE/apps/editor-client/dist/index.js"
  test -f "$WORKSPACE/apps/vscode-extension/dist/extension.js"
  echo "==> npm artifacts verified"
fi

if [[ "$SKIP_PYTHON" != "1" ]]; then
  echo "==> python venv/install"
  (
    cd "$WORKSPACE/services/agentd-py"
    if [[ ! -d .venv ]]; then
      python3 -m venv .venv
    fi
    .venv/bin/pip install -e '.[dev]'
  )
  echo "==> python dependencies verified"
fi

if [[ "$SKIP_INDEX" != "1" ]]; then
  echo "==> indexer build/index"
  mkdir -p "$(dirname "$SNAPSHOT_PATH")"
  # Build the indexer from the REPO source ($ROOT, not $WORKSPACE — a target
  # workspace may not contain the Rust source) into the shared cargo target dir,
  # then index $WORKSPACE with the resolved binary.
  INDEXER_BIN="$(ensure_indexer_binary "$ROOT/services/indexer-rs")"
  "$INDEXER_BIN" index \
    --workspace "$WORKSPACE" \
    --snapshot-path "$SNAPSHOT_PATH" \
    --watch 0

  if [[ ! -f "$SNAPSHOT_PATH" ]]; then
    echo "Snapshot file not found after indexing: $SNAPSHOT_PATH" >&2
    exit 1
  fi

  python3 - "$SNAPSHOT_PATH" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
stats = payload.get("stats", {})
node_count = int(stats.get("node_count", 0))
edge_count = int(stats.get("edge_count", 0))
diag_count = int(stats.get("diagnostic_count", 0))
print(f"snapshot_stats nodes={node_count} edges={edge_count} diagnostics={diag_count}")
if node_count <= 0:
    raise SystemExit("node_count must be > 0")
if edge_count <= 0:
    raise SystemExit("edge_count must be > 0")
PY

  echo "==> snapshot verified"
fi

echo "==> bootstrap completed"
