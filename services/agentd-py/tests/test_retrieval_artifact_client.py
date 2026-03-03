from __future__ import annotations

import json
import time
from pathlib import Path

from agentd.retrieval.artifact_client import RetrievalArtifactClient, RetrievalContext


def _write_snapshot(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _snapshot_payload(generated_at_ms: int) -> dict[str, object]:
    return {
        "schema_version": 1,
        "workspace_root": "/tmp/repo",
        "generated_at_ms": generated_at_ms,
        "graph": {
            "nodes": [
                {"id": "file:src/auth.py", "path": "src/auth.py", "name": "auth.py", "kind": "File"},
                {
                    "id": "function:file:src/auth.py:build_auth",
                    "path": "src/auth.py",
                    "name": "build_auth",
                    "kind": "Function",
                },
                {
                    "id": "function:file:src/auth.py:validate_token",
                    "path": "src/auth.py",
                    "name": "validate_token",
                    "kind": "Function",
                },
            ],
            "edges": [
                {
                    "from": "file:src/auth.py",
                    "to": "function:file:src/auth.py:build_auth",
                    "kind": "references",
                },
                {
                    "from": "function:file:src/auth.py:build_auth",
                    "to": "function:file:src/auth.py:validate_token",
                    "kind": "calls",
                },
            ],
        },
        "diagnostics": [
            {
                "file": "src/auth.py",
                "line": 12,
                "column": 7,
                "message": "name 'tokn' is not defined",
            }
        ],
        "stats": {
            "node_count": 3,
            "edge_count": 2,
            "diagnostic_count": 1,
        },
    }


def test_load_context_from_valid_snapshot(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir(parents=True)
    snapshot_path = workspace / ".ai-editor/index-snapshot.json"
    _write_snapshot(snapshot_path, _snapshot_payload(int(time.time() * 1000)))

    client = RetrievalArtifactClient()
    context, warnings = client.load_context(str(workspace), "build auth token validation")

    assert warnings == []
    assert "src/auth.py" in context.related_files
    assert "build_auth" in context.related_symbols
    assert context.graph_neighbors
    assert context.diagnostics_excerpt
    assert context.snapshot_stats["node_count"] == 3
    assert context.snapshot_age_sec is not None


def test_stale_snapshot_warns_but_returns_context(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir(parents=True)
    snapshot_path = workspace / ".ai-editor/index-snapshot.json"
    old_ms = int((time.time() - 600) * 1000)
    _write_snapshot(snapshot_path, _snapshot_payload(old_ms))

    client = RetrievalArtifactClient(max_age_sec=10)
    context, warnings = client.load_context(str(workspace), "auth")

    assert context.related_files
    assert any("stale" in warning.message for warning in warnings)


def test_missing_snapshot_triggers_auto_index_once(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir(parents=True)
    run_log = workspace / "runs.txt"
    command = (
        "python3 -c \"from pathlib import Path; "
        "p=Path('{workspace}/runs.txt'); "
        "existing=p.read_text(encoding='utf-8') if p.exists() else ''; "
        "p.write_text(existing + '1\\\\n', encoding='utf-8')\""
    )

    client = RetrievalArtifactClient(index_command_template=command, index_timeout_sec=10)
    context, warnings = client.load_context(str(workspace), "auth")

    assert context == RetrievalContext.empty()
    assert run_log.exists()
    assert run_log.read_text(encoding="utf-8").splitlines() == ["1"]
    assert any("unavailable" in warning.message for warning in warnings)


def test_auto_index_failure_warns_and_continues(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir(parents=True)
    command = "python3 -c \"import sys; sys.exit(7)\""

    client = RetrievalArtifactClient(index_command_template=command, index_timeout_sec=10)
    context, warnings = client.load_context(str(workspace), "auth")

    assert context == RetrievalContext.empty()
    assert any("exit code 7" in warning.message for warning in warnings)
    assert any("unavailable" in warning.message for warning in warnings)


def test_corrupt_snapshot_warns_and_returns_empty_context(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir(parents=True)
    snapshot_path = workspace / ".ai-editor/index-snapshot.json"
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text("{bad-json", encoding="utf-8")

    client = RetrievalArtifactClient()
    context, warnings = client.load_context(str(workspace), "auth")

    assert context == RetrievalContext.empty()
    assert any("could not be parsed" in warning.message for warning in warnings)
