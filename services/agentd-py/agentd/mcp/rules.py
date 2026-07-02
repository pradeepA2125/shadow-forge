"""Per-workspace store of user-approved (server, tool) MCP pairs — backs the
mcp_tool gate's "Approve & remember (this workspace)" choice. The MCP analog of
CommandRuleStore, keyed on the exact pair (spec §3.4: not a command string)."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path


class McpRuleStore:
    def __init__(self, workspace_path: str | Path) -> None:
        self._path = Path(workspace_path) / ".ai-editor" / "approved-mcp-tools.json"

    def load(self) -> list[dict[str, str]]:
        if not self._path.exists():
            return []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        return [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []

    def matches(self, server: str, tool: str) -> bool:
        return any(r.get("server") == server and r.get("tool") == tool for r in self.load())

    def add(self, server: str, tool: str) -> None:
        rules = self.load()
        if any(r.get("server") == server and r.get("tool") == tool for r in rules):
            return
        rules.append({"server": server, "tool": tool,
                      "added_at": datetime.now(UTC).isoformat()})
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(rules, indent=2), encoding="utf-8")
