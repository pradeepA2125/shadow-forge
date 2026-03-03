# @ai-editor/vscode-extension

VS Code MVP extension for AI Editor task review loop.

## Commands
- `AI Editor: Start Task`
- `AI Editor: Open Review Panel`
- `AI Editor: Accept Patch`
- `AI Editor: Reject Patch`
- `AI Editor: Refresh Task`

## Settings
- `aiEditor.backendBaseUrl` (default `http://127.0.0.1:8000`)
- `aiEditor.defaultMode` (default `project_edit`)
- `aiEditor.pollIntervalMs` (default `1000`)

## Notes
- The extension attaches to an already-running `agentd-py` service.
- It does not start or manage backend processes.
