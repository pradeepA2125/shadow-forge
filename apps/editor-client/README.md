# @ai-editor/editor-client

TypeScript package for editor-facing contracts and backend transport.

## Scope
- Shared task/plan/patch schemas used by UI and backend
- Typed HTTP client for backend task APIs
- UI-safe task state lifecycle helpers

## Non-goals
- Agent orchestration runtime
- Patch application execution
- Validation command execution

Those run in `services/agentd-py`.
