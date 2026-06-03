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
    (workspace / "src").mkdir(parents=True)
    (workspace / "src/auth.py").write_text(
        "def build_auth(token: str) -> str:\n    return validate_token(token)\n\n\ndef validate_token(token: str) -> str:\n    return token.strip()\n",
        encoding="utf-8",
    )
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
    assert context.planner_evidence.evidence_files
    assert context.planner_evidence.evidence_files[0].path == "src/auth.py"
    assert "build_auth" in context.planner_evidence.evidence_files[0].excerpt
    assert context.planner_evidence.evidence_symbols
    assert context.planner_evidence.evidence_symbols[0].file == "src/auth.py"


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


def test_context_filters_shadow_paths_and_normalizes_to_repo_relative(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir(parents=True)
    snapshot_path = workspace / ".ai-editor/index-snapshot.json"
    payload = _snapshot_payload(int(time.time() * 1000))
    payload["workspace_root"] = str(workspace)
    payload["graph"] = {
        "nodes": [
            {
                "id": "file:shadow",
                "path": str(
                    workspace
                    / ".agentd/shadows/task-1/services/agentd-py/agentd/api/tasks.py"
                ),
                "name": "tasks.py",
                "kind": "File",
            },
            {
                "id": "file:routes",
                "path": str(workspace / "services/agentd-py/agentd/api/routes.py"),
                "name": "routes.py",
                "kind": "File",
            },
            {
                "id": "function:file:routes:get_task_result",
                "path": str(workspace / "services/agentd-py/agentd/api/routes.py"),
                "name": "get_task_result",
                "kind": "Function",
            },
        ],
        "edges": [],
    }
    payload["diagnostics"] = [
        {
            "file": str(workspace / ".agentd/shadows/task-1/bad.py"),
            "line": 1,
            "column": 1,
            "message": "bad shadow diagnostic",
        },
        {
            "file": str(workspace / "services/agentd-py/agentd/api/routes.py"),
            "line": 12,
            "column": 1,
            "message": "route warning",
        },
    ]

    _write_snapshot(snapshot_path, payload)
    client = RetrievalArtifactClient()
    context, warnings = client.load_context(str(workspace), "task events route")

    assert warnings == []
    assert "services/agentd-py/agentd/api/routes.py" in context.related_files
    assert all(not path.startswith("/") for path in context.related_files)
    assert all(".agentd/shadows" not in path for path in context.related_files)
    assert context.diagnostics_excerpt == [
        "services/agentd-py/agentd/api/routes.py:12: route warning"
    ]


def test_load_context_ranks_by_goal_terms_without_repo_specific_bias(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir(parents=True)
    snapshot_path = workspace / ".ai-editor/index-snapshot.json"
    payload = _snapshot_payload(int(time.time() * 1000))
    payload["workspace_root"] = str(workspace)
    payload["graph"] = {
        "nodes": [
            {
                "id": "file:ui",
                "path": "ui/button.tsx",
                "name": "button.tsx",
                "kind": "File",
            },
            {
                "id": "file:auth",
                "path": "src/auth/session_auth.py",
                "name": "session_auth.py",
                "kind": "File",
            },
            {
                "id": "file:payments",
                "path": "src/payments/processor.py",
                "name": "processor.py",
                "kind": "File",
            },
        ],
        "edges": [],
    }
    payload["diagnostics"] = []
    _write_snapshot(snapshot_path, payload)

    client = RetrievalArtifactClient()
    context, warnings = client.load_context(
        str(workspace),
        "Improve session auth flow",
    )

    assert warnings == []
    assert context.related_files
    assert context.related_files[0] == "src/auth/session_auth.py"


def test_graph_neighbor_files_includes_imports_reached_via_semantic_seed(
    tmp_path: Path,
) -> None:
    """When a file lands in semantic top-K but isn't keyword-matched, its
    file-level Imports edges should still expand graph_neighbor_files to the
    imported workspace files. This was the failure mode behind the planner
    missing state_machine.py / models.py — the seed was symbol-only, so the
    file→file import edges never participated in neighbour expansion."""
    workspace = tmp_path / "repo"
    workspace.mkdir(parents=True)
    snapshot_path = workspace / ".ai-editor/index-snapshot.json"
    abs_engine = str(workspace / "src/engine.py")
    abs_state_machine = str(workspace / "src/state_machine.py")
    abs_unrelated = str(workspace / "src/unrelated.py")

    payload = _snapshot_payload(int(time.time() * 1000))
    payload["workspace_root"] = str(workspace)
    payload["graph"] = {
        "nodes": [
            {"id": "file:engine", "path": abs_engine, "name": "engine.py", "kind": "File"},
            {"id": "file:sm", "path": abs_state_machine, "name": "state_machine.py", "kind": "File"},
            {"id": "file:unrelated", "path": abs_unrelated, "name": "unrelated.py", "kind": "File"},
        ],
        "edges": [
            # The key file→file import that prior seed logic could not follow.
            {"from": "file:engine", "to": "file:sm", "kind": "Imports"},
        ],
    }
    payload["diagnostics"] = []
    _write_snapshot(snapshot_path, payload)

    # Stub semantic index: returns engine.py as the top semantic hit. The keyword
    # match in the goal ("orchestrate") does not touch engine.py at all — the
    # only path to seed engine.py is via the semantic union we just added.
    from agentd.retrieval.chunker import CodeChunk, ScoredChunk

    class _StubSemanticIndex:
        def is_ready(self) -> bool:
            return True

        def query(self, goal: str, *, top_k: int, exclude_tests: bool):
            chunk = CodeChunk(
                chunk_id="src/engine.py::L1",
                path="src/engine.py",
                language="python",
                line_start=1,
                line_end=10,
                line_count=10,
                name="orchestrate",
                kind="Function",
                signature="def orchestrate():",
                parent_name=None,
                parent_kind=None,
                module_path="engine",
                is_top_level=True,
                imports=[],
                calls=[],
                called_by=[],
                docstring=None,
                text="def orchestrate(): pass",
                text_with_lines="  1: def orchestrate(): pass",
                context_before="",
                context_after="",
                is_test=False,
                has_docstring=False,
                file_mtime=0.0,
                indexed_at_ms=0,
            )
            return [ScoredChunk(chunk=chunk, score=0.9)]

    client = RetrievalArtifactClient(semantic_index=_StubSemanticIndex())
    context, _ = client.load_context(str(workspace), "orchestrate something")

    # engine.py is the seed (via semantic). The edge engine.py → state_machine.py
    # should surface state_machine.py in graph_neighbor_files.
    assert "src/state_machine.py" in context.graph_neighbor_files, (
        f"expected state_machine.py in graph_neighbor_files; got: {context.graph_neighbor_files}"
    )
    # And the unrelated file (no edges) must NOT appear.
    assert "src/unrelated.py" not in context.graph_neighbor_files
    # Seed file itself must NOT be in its own neighbour list.
    assert "src/engine.py" not in context.graph_neighbor_files
