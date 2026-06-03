from __future__ import annotations

import json
from pathlib import Path

from agentd.retrieval.graph_walker import GraphWalker


def _write_snapshot(path: Path, nodes: list[dict], edges: list[dict]) -> None:
    payload = {
        "schema_version": 1,
        "workspace_root": str(path.parent),
        "generated_at_ms": 0,
        "graph": {"nodes": nodes, "edges": edges},
        "diagnostics": [],
        "stats": {"node_count": len(nodes), "edge_count": len(edges), "diagnostic_count": 0},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _fixture(workspace: Path) -> Path:
    snapshot = workspace / ".ai-editor" / "index-snapshot.json"
    workspace.mkdir(parents=True, exist_ok=True)
    eng = workspace / "src/engine.py"
    sm = workspace / "src/state_machine.py"
    base = workspace / "src/storage/base.py"
    sqlite = workspace / "src/storage/sqlite.py"
    memory = workspace / "src/storage/memory.py"
    nodes = [
        {"id": "fn:engine:run_task", "path": str(eng), "name": "run_task",
         "kind": "Function", "line": 1},
        {"id": "fn:engine:other", "path": str(eng), "name": "other",
         "kind": "Function", "line": 30},
        {"id": "file:sm", "path": str(sm), "name": "state_machine.py", "kind": "File", "line": 1},
        {"id": "fn:sm:transition", "path": str(sm), "name": "transition",
         "kind": "Function", "line": 5},
        {"id": "method:base:TaskStore.save", "path": str(base), "name": "TaskStore.save",
         "kind": "Method", "line": 10},
        {"id": "method:sqlite:save", "path": str(sqlite), "name": "SQLiteTaskStore.save",
         "kind": "Method", "line": 12},
        {"id": "method:memory:save", "path": str(memory), "name": "InMemoryTaskStore.save",
         "kind": "Method", "line": 8},
        {"id": "external:call:noise", "path": str(eng), "name": "noise",
         "kind": "Function", "line": 50},
    ]
    edges = [
        # run_task → transition  (Calls)
        {"from": "fn:engine:run_task", "to": "fn:sm:transition", "kind": "Calls"},
        # run_task → TaskStore.save  (Calls — Protocol)
        {"from": "fn:engine:run_task", "to": "method:base:TaskStore.save", "kind": "Calls"},
        # impls fan in
        {"from": "method:sqlite:save", "to": "method:base:TaskStore.save", "kind": "Implements"},
        {"from": "method:memory:save", "to": "method:base:TaskStore.save", "kind": "Implements"},
        # An external edge from run_task — walker must filter it.
        {"from": "fn:engine:run_task", "to": "external:call:noise", "kind": "Calls"},
    ]
    _write_snapshot(snapshot, nodes, edges)
    return snapshot


def test_query_by_symbol_returns_only_matching_root(tmp_path: Path) -> None:
    snapshot = _fixture(tmp_path)
    walker = GraphWalker(snapshot, tmp_path)

    result = walker.query("src/engine.py:run_task", depth=1, limit=20)

    assert len(result.matched_roots) == 1
    assert result.matched_roots[0].symbol == "run_task"
    assert result.matched_roots[0].file == "src/engine.py"


def test_query_by_file_returns_all_nodes_in_file(tmp_path: Path) -> None:
    snapshot = _fixture(tmp_path)
    walker = GraphWalker(snapshot, tmp_path)

    result = walker.query("src/engine.py", depth=1, limit=20)
    # engine.py has run_task + other + the external:call:noise node. External
    # markers should be skipped from matched_roots.
    names = {root.symbol for root in result.matched_roots}
    assert "run_task" in names
    assert "other" in names


def test_calls_neighbors_resolve_to_workspace_symbols(tmp_path: Path) -> None:
    snapshot = _fixture(tmp_path)
    walker = GraphWalker(snapshot, tmp_path)

    result = walker.query("src/engine.py:run_task", depth=1, limit=20, edge_kinds=["Calls"])

    # Two outbound Calls: transition + TaskStore.save. External edge dropped.
    outbound_calls = [n for n in result.neighbors if n.direction == "out" and n.edge_kind == "Calls"]
    assert len(outbound_calls) == 2
    names = {n.node.symbol for n in outbound_calls}
    assert names == {"transition", "TaskStore.save"}
    # External marker target must NOT be present.
    assert all(not n.node.raw_id.startswith("external:") for n in result.neighbors)


def test_implements_neighbors_are_inbound(tmp_path: Path) -> None:
    snapshot = _fixture(tmp_path)
    walker = GraphWalker(snapshot, tmp_path)

    result = walker.query(
        "src/storage/base.py:TaskStore.save", depth=1, limit=20, edge_kinds=["Implements"]
    )
    impls = [n for n in result.neighbors if n.edge_kind == "Implements" and n.direction == "in"]
    assert {n.node.symbol for n in impls} == {"SQLiteTaskStore.save", "InMemoryTaskStore.save"}


def test_depth_two_reaches_grandchildren_via_calls(tmp_path: Path) -> None:
    snapshot = _fixture(tmp_path)
    walker = GraphWalker(snapshot, tmp_path)

    # depth-2 from run_task should reach: transition (depth-1), TaskStore.save
    # (depth-1), and via TaskStore.save's inbound Implements edges →
    # SQLiteTaskStore.save + InMemoryTaskStore.save (depth-2).
    result = walker.query("src/engine.py:run_task", depth=2, limit=20)
    symbols_at_distance_2 = {n.node.symbol for n in result.neighbors if n.distance == 2}
    assert "SQLiteTaskStore.save" in symbols_at_distance_2
    assert "InMemoryTaskStore.save" in symbols_at_distance_2


def test_limit_caps_results_and_sets_truncated(tmp_path: Path) -> None:
    snapshot = _fixture(tmp_path)
    walker = GraphWalker(snapshot, tmp_path)

    result = walker.query("src/engine.py:run_task", depth=2, limit=1)
    assert len(result.neighbors) == 1
    assert result.truncated is True


def test_unknown_root_returns_empty(tmp_path: Path) -> None:
    snapshot = _fixture(tmp_path)
    walker = GraphWalker(snapshot, tmp_path)

    result = walker.query("src/does_not_exist.py:nope", depth=1, limit=20)
    assert result.matched_roots == []
    assert result.neighbors == []
    assert result.truncated is False


def test_edge_kind_filter_applied(tmp_path: Path) -> None:
    snapshot = _fixture(tmp_path)
    walker = GraphWalker(snapshot, tmp_path)

    # Asking only for Implements from run_task should return nothing (its
    # outbound edges are Calls, not Implements).
    result = walker.query("src/engine.py:run_task", depth=1, limit=20, edge_kinds=["Implements"])
    assert result.neighbors == []


def test_snapshot_reload_when_mtime_changes(tmp_path: Path) -> None:
    snapshot = _fixture(tmp_path)
    walker = GraphWalker(snapshot, tmp_path)

    first = walker.query("src/engine.py:run_task", depth=1, limit=20, edge_kinds=["Calls"])
    initial_count = len(first.neighbors)

    # Mutate the snapshot — drop the TaskStore.save edge — and bump its mtime.
    payload = json.loads(snapshot.read_text())
    payload["graph"]["edges"] = [
        e for e in payload["graph"]["edges"]
        if not (e["from"] == "fn:engine:run_task" and e["to"] == "method:base:TaskStore.save")
    ]
    snapshot.write_text(json.dumps(payload), encoding="utf-8")
    # Touch to ensure mtime changes (very fast tests can fall inside same ns)
    import os, time
    new_ts = time.time() + 1
    os.utime(snapshot, (new_ts, new_ts))

    second = walker.query("src/engine.py:run_task", depth=1, limit=20, edge_kinds=["Calls"])
    assert len(second.neighbors) < initial_count
