from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentd.retrieval.graph_walker import GraphWalker, GraphWalkerSnapshotError


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


def test_file_seed_aggregates_to_distinct_files_grouped_by_direction(tmp_path: Path) -> None:
    """File-seeded query returns file_neighbors (file-level), not neighbors
    (symbol-level). engine.py calls into state_machine.py + storage/base.py
    (out), so both appear as outbound file neighbours."""
    snapshot = _fixture(tmp_path)
    walker = GraphWalker(snapshot, tmp_path)

    result = walker.query("src/engine.py", depth=1, limit=20)

    # Symbol-level list is empty; file-level list is populated.
    assert result.neighbors == []
    assert result.file_neighbors

    out_files = {fn.file for fn in result.file_neighbors if fn.direction == "out"}
    # run_task calls transition (state_machine.py) and save (base.py Protocol).
    assert "src/state_machine.py" in out_files
    assert "src/storage/base.py" in out_files
    # The seed file itself must not appear as its own neighbour.
    assert "src/engine.py" not in {fn.file for fn in result.file_neighbors}


def test_file_seed_collapses_multiple_edges_into_one_row_with_count(tmp_path: Path) -> None:
    """Two symbol edges from engine.py into state_machine.py collapse to a
    single file row whose edge_count reflects the aggregation."""
    snapshot = _fixture(tmp_path)
    # Add a second call from `other` into state_machine.py:transition.
    payload = json.loads(snapshot.read_text())
    payload["graph"]["edges"].append(
        {"from": "fn:engine:other", "to": "fn:sm:transition", "kind": "Calls"}
    )
    snapshot.write_text(json.dumps(payload), encoding="utf-8")
    import os, time
    ts = time.time() + 1
    os.utime(snapshot, (ts, ts))

    walker = GraphWalker(snapshot, tmp_path)
    result = walker.query("src/engine.py", depth=1, limit=20)

    sm_rows = [
        fn for fn in result.file_neighbors
        if fn.file == "src/state_machine.py" and fn.direction == "out"
    ]
    assert len(sm_rows) == 1, "should be a single aggregated row per (file, direction)"
    assert sm_rows[0].edge_count >= 2


def test_file_seed_truncates_on_distinct_files_not_edges(tmp_path: Path) -> None:
    snapshot = _fixture(tmp_path)
    walker = GraphWalker(snapshot, tmp_path)
    # limit=1 → at most one file row; truncated set because more files exist.
    result = walker.query("src/engine.py", depth=1, limit=1)
    assert len(result.file_neighbors) == 1
    assert result.truncated is True


def test_registry_blast_radius_unions_neighbour_files_excluding_seeds(tmp_path: Path) -> None:
    """PlanningToolRegistry.blast_radius — the cross-file ripple of a set of
    seed files, used by the chat classifier to judge true scope. Seed files
    are excluded; neighbours from all seeds are unioned."""
    from agentd.planning.registry import PlanningToolRegistry

    snapshot = _fixture(tmp_path)  # engine.py -> state_machine.py + storage/base.py
    # Override the env so the registry reads our fixture snapshot.
    import os
    os.environ["AI_EDITOR_RETRIEVAL_SNAPSHOT_PATH"] = str(snapshot)
    try:
        reg = PlanningToolRegistry(real_path=tmp_path)
        br = reg.blast_radius(["src/engine.py"])
    finally:
        del os.environ["AI_EDITOR_RETRIEVAL_SNAPSHOT_PATH"]

    # engine.py calls into state_machine.py and storage/base.py → both ripple.
    assert "src/state_machine.py" in br
    assert "src/storage/base.py" in br
    # The seed file itself is excluded.
    assert "src/engine.py" not in br


def test_symbol_seed_still_returns_symbol_level_neighbors(tmp_path: Path) -> None:
    """The aggregation is file-seed only; symbol seeds keep symbol detail."""
    snapshot = _fixture(tmp_path)
    walker = GraphWalker(snapshot, tmp_path)

    result = walker.query("src/engine.py:run_task", depth=1, limit=20, edge_kinds=["Calls"])
    assert result.file_neighbors == []
    assert result.neighbors
    assert {n.node.symbol for n in result.neighbors} == {"transition", "TaskStore.save"}


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


def test_missing_snapshot_first_load_raises_filenotfound(tmp_path: Path) -> None:
    """Caller has nothing to fall back to — propagate so the tool layer can
    translate to a clear 'run the indexer' message rather than silently
    returning an empty graph."""
    walker = GraphWalker(tmp_path / ".ai-editor" / "index-snapshot.json", tmp_path)
    with pytest.raises(FileNotFoundError):
        walker.query("anything", depth=1, limit=10)


def test_corrupt_snapshot_first_load_raises_typed_error(tmp_path: Path) -> None:
    """Garbled JSON on first load must surface as `GraphWalkerSnapshotError`,
    NOT a raw `json.JSONDecodeError` that the tool registry doesn't know how
    to handle. Without this, the planning loop would crash."""
    snapshot = tmp_path / ".ai-editor" / "index-snapshot.json"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text("{ this isn't json at all", encoding="utf-8")
    walker = GraphWalker(snapshot, tmp_path)
    with pytest.raises(GraphWalkerSnapshotError):
        walker.query("anything", depth=1, limit=10)


def test_corrupt_snapshot_after_successful_load_keeps_cached_state(tmp_path: Path) -> None:
    """The indexer overwrites the snapshot mid-rebuild on some paths. If the
    file is briefly malformed AFTER we've already loaded once, the walker
    must serve the cached state rather than crash the loop."""
    snapshot = _fixture(tmp_path)
    walker = GraphWalker(snapshot, tmp_path)

    # Prime the cache with a valid load.
    first = walker.query("src/engine.py:run_task", depth=1, limit=10, edge_kinds=["Calls"])
    assert first.neighbors  # sanity: cache is populated

    # Now scramble the file + bump mtime so `_ensure_loaded_locked` will try
    # to reload but hit a JSONDecodeError.
    snapshot.write_text("{ broken", encoding="utf-8")
    import os, time
    new_ts = time.time() + 1
    os.utime(snapshot, (new_ts, new_ts))

    # The next query MUST NOT raise; it serves cached state.
    second = walker.query("src/engine.py:run_task", depth=1, limit=10, edge_kinds=["Calls"])
    assert second.matched_roots == first.matched_roots
    assert {n.node.symbol for n in second.neighbors} == {n.node.symbol for n in first.neighbors}


def test_bad_arg_types_do_not_crash(tmp_path: Path) -> None:
    """If the LLM sends `depth="medium"` or `limit=None`, the walker clamps
    rather than raising. Without this, a single malformed tool call would
    crash the planning loop."""
    snapshot = _fixture(tmp_path)
    walker = GraphWalker(snapshot, tmp_path)

    # int/None/string mix — none should raise.
    result = walker.query(
        "src/engine.py:run_task",
        depth="medium",      # type: ignore[arg-type]
        limit=None,          # type: ignore[arg-type]
        edge_kinds=["Calls"],
    )
    assert isinstance(result.matched_roots, list)
    assert result.stats["depth"] >= 1
    assert result.stats["limit"] >= 1


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
