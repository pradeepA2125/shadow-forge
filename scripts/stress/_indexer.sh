# Shared indexer-build logic, sourced (not executed) by bootstrap.sh and
# start-backend.sh. No shebang / no `set -e` — it inherits the caller's options.
#
# The Rust indexer is compiled into a SHARED cargo target dir so every git
# worktree reuses one set of compiled artifacts. cargo's own fingerprinting is
# the skip/build decision: a warm cache makes a fresh worktree's "build" a ~1s
# no-op, while a cold cache pays the multi-minute build exactly once (shared,
# not multi-GB per worktree). Source differences across worktrees are handled by
# cargo — it rebuilds only the changed crates. Concurrent builds into the shared
# dir serialize on cargo's own lock (safe, not corrupt); identical sources never
# thrash. Override the cache location with AI_EDITOR_INDEXER_TARGET_DIR.

# Absolute shared cargo target dir.
indexer_target_dir() {
  printf '%s' "${AI_EDITOR_INDEXER_TARGET_DIR:-$HOME/.cache/ai-editor/indexer-target}"
}

# Absolute path to the release binary inside the shared target dir.
indexer_bin_path() {
  printf '%s/release/ai-editor-indexer' "$(indexer_target_dir)"
}

# ensure_indexer_binary <indexer_src_dir>
# Build (incrementally) the indexer into the shared target dir. On success echoes
# the binary path on stdout (progress goes to stderr, so the path is capturable).
# Returns non-zero — with a stderr message — when cargo is unavailable, the source
# dir is missing, or the build fails; callers decide whether that is fatal.
ensure_indexer_binary() {
  local src_dir="$1"
  local target_dir bin
  target_dir="$(indexer_target_dir)"
  bin="$(indexer_bin_path)"

  if [[ -z "$src_dir" ]]; then
    echo "==> indexer: ensure_indexer_binary requires an <indexer_src_dir> argument" >&2
    return 1
  fi
  if ! command -v cargo >/dev/null 2>&1; then
    echo "==> indexer: cargo not found on PATH — cannot build $bin" >&2
    return 1
  fi
  if [[ ! -d "$src_dir" ]]; then
    echo "==> indexer: source dir missing: $src_dir" >&2
    return 1
  fi

  mkdir -p "$target_dir"
  if [[ -x "$bin" ]]; then
    echo "==> indexer: ensuring up-to-date (shared target: $target_dir) ..." >&2
  else
    echo "==> indexer: building (first build may take a few minutes; shared target: $target_dir) ..." >&2
  fi
  # 1>&2: keep cargo's output off stdout so the captured value is only the bin path.
  if ! ( cd "$src_dir" && CARGO_TARGET_DIR="$target_dir" cargo build --release 1>&2 ); then
    echo "==> indexer: cargo build failed" >&2
    return 1
  fi
  if [[ ! -x "$bin" ]]; then
    echo "==> indexer: build reported success but binary not found at $bin" >&2
    return 1
  fi
  printf '%s\n' "$bin"
  return 0
}
