# agentd-py

Python orchestration backend for AI Editor.

## Responsibilities
- Deterministic task lifecycle orchestration
- Plan/patch/repair execution loop
- Deterministic validation command pipeline (syntax/type/lint/test)
- Integration point for model providers (OpenAI, others)

Patch operations are validated and applied inside shadow workspaces, then promoted on accept.

Review lifecycle states are explicit:
- `READY_FOR_REVIEW` after validation passes in shadow workspace
- `PROMOTING` while applying accepted patch set to the real workspace
- `SUCCEEDED` after successful promotion

## API surface (scaffold)
- `POST /v1/tasks`
- `GET /v1/tasks/{task_id}`
- `GET /v1/tasks/{task_id}/result`
- `GET /v1/tasks/{task_id}/artifacts`
- `POST /v1/tasks/{task_id}/cancel`
- `POST /v1/tasks/{task_id}/accept`
- `POST /v1/tasks/{task_id}/reject`

`accept`, `reject`, and `GET /result` return a `TaskResult` payload with `plan` and `patch` metadata.

## Runtime configuration
- `OPENAI_API_KEY`: required when `AI_EDITOR_REASONING_BACKEND=openai` (default)
- `AI_EDITOR_OPENAI_MODEL`: optional, default `gpt-5`
- `ANTHROPIC_API_KEY`: required when `AI_EDITOR_REASONING_BACKEND=anthropic`
- `AI_EDITOR_ANTHROPIC_MODEL`: optional, default `claude-3-5-sonnet-latest`
- `AI_EDITOR_ANTHROPIC_ENDPOINT`: optional, default `https://api.anthropic.com/v1/messages` (converted to SDK base URL internally)
- `AI_EDITOR_ANTHROPIC_VERSION`: optional, default `2023-06-01` (sent via SDK default headers)
- `AI_EDITOR_ANTHROPIC_MAX_TOKENS`: optional, default `4096`
- `AI_EDITOR_ANTHROPIC_TIMEOUT_SEC`: optional, default `60.0`
- `GEMINI_API_KEY` or `GOOGLE_API_KEY`: required when `AI_EDITOR_REASONING_BACKEND=gemini`
- `AI_EDITOR_GEMINI_MODEL`: optional, default `gemini-3-flash-preview`
- `AI_EDITOR_GEMINI_THINKING_ENABLED`: optional, default `1` (enables Gemini thinking mode)
- `AI_EDITOR_GEMINI_THINKING_BUDGET`: optional integer budget; default dynamic `-1` when thinking is enabled and no level is set
- `AI_EDITOR_GEMINI_THINKING_LEVEL`: optional thinking level hint (for models that support levels)
- `AI_EDITOR_GEMINI_INCLUDE_THOUGHTS`: optional (`1|0`), default `0`
- `HF_TOKEN` (or `HUGGING_FACE_HUB_TOKEN` / `HUGGINGFACEHUB_API_TOKEN`): required when `AI_EDITOR_REASONING_BACKEND=huggingface`
- `AI_EDITOR_HUGGINGFACE_MODEL`: optional, default `deepseek-ai/DeepSeek-R1:fastest` (set to a coding model such as `deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct:fastest` if preferred)
- `AI_EDITOR_HUGGINGFACE_MAX_NEW_TOKENS`: optional, default `4096`
- `AI_EDITOR_HUGGINGFACE_SEED`: optional integer seed for reproducibility
- `AI_EDITOR_HUGGINGFACE_TIMEOUT_SEC`: optional, default `60.0`
- `GROQ_API_KEY`: required when `AI_EDITOR_REASONING_BACKEND=groq`
- `AI_EDITOR_GROQ_MODEL`: optional, default `openai/gpt-oss-120b`
- `AI_EDITOR_GROQ_ENDPOINT`: optional custom base URL for Groq-compatible endpoint
- `AI_EDITOR_GROQ_MAX_TOKENS`: optional, default `4096`
- `AI_EDITOR_GROQ_TIMEOUT_SEC`: optional, default `60.0`
- `AI_EDITOR_REASONING_BACKEND`: `openai` (default), `anthropic`, `gemini`, `huggingface`, `groq`, or `scripted` (debug)
- `AI_EDITOR_VALIDATION_COMMANDS_JSON`: optional JSON array of commands; if unset, validator auto-detects defaults
- `AI_EDITOR_STEP_SCOPED_MODE`: optional (`1|0`), default `1`; enables step-scoped patching with preflight gates
- `AI_EDITOR_AST_CUTOVER_MODE`: optional, default `hard`; any value other than `hard` fails startup
- `AI_EDITOR_MAX_ATTEMPTS_PER_STEP`: optional, default `3`
- `AI_EDITOR_PATCH_CANDIDATE_COUNT`: optional, default `3`
- `AI_EDITOR_CHECKPOINT_RETENTION_TASKS`: optional, default `20`
- `AI_EDITOR_DB_PATH`: optional SQLite path, default `.agentd/agentd.sqlite3`
- `AI_EDITOR_SHADOW_ROOT`: optional shadow root, default `.agentd/shadows`

AST patching dependencies:
- Python AST/CST patching requires `libcst` (installed by default dependencies).
- TypeScript/Rust selector resolution uses `tree_sitter_languages` when available.
- If tree-sitter parsers are unavailable in runtime, candidate preflight fails deterministically with `parser_unavailable`.

## Run (after deps install)
```bash
cd services/agentd-py
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
uvicorn agentd.main:app --reload --port 8000
```

## Development profile (Hugging Face serverless)
```bash
export AI_EDITOR_REASONING_BACKEND=huggingface
export HF_TOKEN=hf_xxx
export AI_EDITOR_HUGGINGFACE_MODEL=deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct:fastest
```

## Development profile (Groq Cloud)
```bash
export AI_EDITOR_REASONING_BACKEND=groq
export GROQ_API_KEY=gsk_xxx
export AI_EDITOR_GROQ_MODEL=openai/gpt-oss-120b
```

## Phase 0 evaluation commands
```bash
# 1) Seed/freeze benchmark corpus manifest
ai-editor-eval init-corpus-manifest \
  --workspace-root /path/to/workspaces \
  --output /path/to/repo/docs/benchmarks/benchmark-corpus.v1.json \
  --freeze

# 2) Export deterministic replay bundle for a task from SQLite
ai-editor-eval export-bundle \
  --db-path /path/to/repo/services/agentd-py/.agentd/agentd.sqlite3 \
  --task-id task-123 \
  --output /tmp/benchmarks/bundle.task-123.json

# 3) Replay/verify deterministic bundle fingerprint
ai-editor-eval replay-bundle \
  --bundle /tmp/benchmarks/bundle.task-123.json

# 4) Produce score + weekly report from bundles directory
ai-editor-eval score --bundles-root /tmp/benchmarks
ai-editor-eval weekly-report --bundles-root /tmp/benchmarks

# 5) Phase 1 reliability gate (baseline vs current)
ai-editor-eval phase1-gate-report \
  --baseline-bundles-root /tmp/benchmarks/baseline \
  --bundles-root /tmp/benchmarks/current
```
