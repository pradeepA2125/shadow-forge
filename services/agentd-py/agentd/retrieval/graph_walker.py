"""In-process BFS walker over the indexer snapshot's symbol graph.

Backs the `query_graph` planning/execution tool. The graph itself is already
loaded by the indexer pipeline into `index-snapshot.json` — this module just
provides a thin, LLM-friendly query surface over it:

    walker.query("services/agentd-py/agentd/orchestrator/engine.py:_run_task",
                 depth=1, limit=20, edge_kinds=["Calls", "Implements"])

Returns a `QueryResult` with each neighbour decoded into a human-readable
`(file, symbol, kind, line, edge_kind, direction, distance)` tuple — strips
the absolute paths + node-id syntax the raw snapshot uses, so the model sees
a workspace-relative path + symbol name pair it can directly hand to
`read_file`.

Loading the snapshot is cheap relative to the planner's other costs (~3 MB
JSON parse) and the walker caches it per workspace_root so repeated tool
calls from the same loop don't re-parse.
"""
from __future__ import annotations

import json
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# ── Public dataclasses ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GraphNode:
    """Snapshot node, decoded to workspace-relative path + symbol info."""

    file: str           # workspace-relative path, e.g. "services/agentd-py/agentd/foo.py"
    symbol: str         # the symbol name, e.g. "_run_task" or "AgentOrchestrator"
    kind: str           # node kind: "File", "Function", "Method", "Class", "Module", "Interface", "Variable"
    line: int           # 1-indexed line of the symbol declaration
    raw_id: str         # the raw snapshot node id — included so the model can re-query if needed


@dataclass(frozen=True)
class GraphNeighbor:
    """One step away from the seed root in the BFS."""

    node: GraphNode
    edge_kind: str      # "Calls" | "Imports" | "References" | "Inherits" | "Implements"
    direction: str      # "out" — seed → neighbour, "in" — neighbour → seed
    distance: int       # 1 = direct neighbour, 2 = neighbour-of-neighbour, etc.


@dataclass(frozen=True)
class QueryResult:
    matched_roots: list[GraphNode]
    neighbors: list[GraphNeighbor]
    truncated: bool
    stats: dict[str, int]


def _root_priority(node: dict[str, object]) -> tuple[int, str]:
    """Order roots so the File-kind node is processed first. File nodes carry
    cross-file `Imports` edges (workspace → external symbol → workspace) that
    no per-symbol node has, and surface the densest cross-file information
    per BFS step. Within the same kind, fall back to alphabetical for
    determinism."""
    kind = str(node.get("kind", ""))
    rank = 0 if kind == "File" else 1
    return rank, str(node.get("id", ""))


# ── Walker ────────────────────────────────────────────────────────────────────

_ALLOWED_EDGE_KINDS = {"Calls", "Imports", "References", "Inherits", "Implements"}
_DEFAULT_DEPTH = 1
_DEFAULT_LIMIT = 20
_MAX_DEPTH = 3
_MAX_LIMIT = 60


class GraphWalker:
    """BFS walker over a single snapshot. Lazy-loaded; thread-safe."""

    def __init__(self, snapshot_path: Path, workspace_root: Path) -> None:
        self._snapshot_path = snapshot_path
        self._workspace_root = workspace_root.resolve()
        self._lock = threading.Lock()
        self._loaded_at_mtime_ns: int | None = None
        self._nodes_by_id: dict[str, dict[str, Any]] = {}
        # Edge indexes: outbound[from_id] → list[(to_id, kind)], inbound[to_id] → list[(from_id, kind)].
        self._outbound: dict[str, list[tuple[str, str]]] = {}
        self._inbound: dict[str, list[tuple[str, str]]] = {}
        # File-path → list[node_id] index (built from nodes_by_id paths). Cached
        # to keep `resolve_root` fast even on repeated calls.
        self._nodes_by_file: dict[str, list[str]] = {}

    # ── Public API ────────────────────────────────────────────────────────

    def query(
        self,
        node: str,
        depth: int = _DEFAULT_DEPTH,
        limit: int = _DEFAULT_LIMIT,
        edge_kinds: list[str] | None = None,
    ) -> QueryResult:
        """BFS expand from `node` and return decoded neighbours.

        `node` accepts two forms:
          - "path/to/file.py"          — root every node in that file.
                                          Intra-file neighbours are SKIPPED:
                                          the caller already has the file, so
                                          the cap is reserved for cross-file
                                          edges they actually need to follow.
          - "path/to/file.py:Symbol"   — root only the matching symbol(s).
                                          Intra-file neighbours INCLUDED so
                                          methods/fields of a class are
                                          visible.
        """
        depth = max(1, min(int(depth), _MAX_DEPTH))
        limit = max(1, min(int(limit), _MAX_LIMIT))
        kinds_filter = self._normalize_edge_kinds(edge_kinds)

        self._ensure_loaded()

        symbol_seed = ":" in node
        roots = self._resolve_roots(node)
        if not roots:
            return QueryResult(matched_roots=[], neighbors=[], truncated=False, stats={
                "root_count": 0, "neighbor_count": 0, "depth": depth, "limit": limit,
            })

        # File-seeded queries: every node in the seed file is a root; skip
        # neighbours that share a path with the roots so the limit is spent
        # on actual cross-file edges. Also reorder roots: the File-kind node
        # holds the inbound `Imports` edges from every caller — by far the
        # densest cross-file signal — so it must run first or its edges get
        # squeezed out by per-symbol Calls noise filling the limit.
        intra_file_paths: set[str] = set()
        if not symbol_seed:
            for root in roots:
                path = root.get("path")
                if isinstance(path, str):
                    intra_file_paths.add(path)
            roots = sorted(roots, key=_root_priority)

        seen_node_ids: set[str] = set(n["id"] for n in roots)
        ordered: list[GraphNeighbor] = []
        truncated = False

        # BFS: each queue item is (node_id, distance, last_edge_kind, last_direction).
        # For depth>1 the last_edge_kind/direction refer to the first hop the path took.
        queue: deque[tuple[str, int]] = deque((n["id"], 0) for n in roots)

        while queue:
            current_id, distance = queue.popleft()
            if distance >= depth:
                continue

            next_distance = distance + 1
            # Outbound edges → "out" direction.
            for neighbor_id, kind in self._outbound.get(current_id, ()):
                if kind not in kinds_filter:
                    continue
                if not self._emit_neighbor(
                    neighbor_id, kind, "out", next_distance,
                    seen_node_ids, ordered, limit, intra_file_paths,
                ):
                    truncated = True
                    queue.clear()
                    break
                if next_distance < depth:
                    queue.append((neighbor_id, next_distance))
            else:
                # Inbound edges → "in" direction. Done in the same loop body so
                # we don't double-cap. `else` runs when the `for` completed
                # without `break`.
                for neighbor_id, kind in self._inbound.get(current_id, ()):
                    if kind not in kinds_filter:
                        continue
                    if not self._emit_neighbor(
                        neighbor_id, kind, "in", next_distance,
                        seen_node_ids, ordered, limit, intra_file_paths,
                    ):
                        truncated = True
                        queue.clear()
                        break
                    if next_distance < depth:
                        queue.append((neighbor_id, next_distance))

        # Stable sort: by edge kind, then file path, then symbol name. Distance
        # already increases monotonically thanks to BFS order; ties go by kind.
        ordered.sort(key=lambda n: (n.edge_kind, n.node.file, n.node.symbol))

        matched_roots: list[GraphNode] = [
            decoded for decoded in (self._decode_node(n) for n in roots) if decoded is not None
        ]
        return QueryResult(
            matched_roots=matched_roots,
            neighbors=ordered,
            truncated=truncated,
            stats={
                "root_count": len(matched_roots),
                "neighbor_count": len(ordered),
                "depth": depth,
                "limit": limit,
            },
        )

    # ── Internals ─────────────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        """Load the snapshot if it has changed since last load. Cheap NOOP
        when nothing's moved."""
        with self._lock:
            try:
                mtime_ns = self._snapshot_path.stat().st_mtime_ns
            except (FileNotFoundError, NotADirectoryError):
                if self._loaded_at_mtime_ns is None:
                    raise
                # Snapshot disappeared after first load — keep what we have.
                return
            if self._loaded_at_mtime_ns == mtime_ns and self._nodes_by_id:
                return

            with self._snapshot_path.open(encoding="utf-8") as fh:
                payload = json.load(fh)

            graph = payload.get("graph", {})
            nodes = graph.get("nodes", []) or []
            edges = graph.get("edges", []) or []

            self._nodes_by_id = {
                node["id"]: node for node in nodes if isinstance(node, dict) and "id" in node
            }

            outbound: dict[str, list[tuple[str, str]]] = {}
            inbound: dict[str, list[tuple[str, str]]] = {}
            for edge in edges:
                if not isinstance(edge, dict):
                    continue
                src = edge.get("from")
                dst = edge.get("to")
                kind = edge.get("kind")
                if not (isinstance(src, str) and isinstance(dst, str) and isinstance(kind, str)):
                    continue
                outbound.setdefault(src, []).append((dst, kind))
                inbound.setdefault(dst, []).append((src, kind))
            self._outbound = outbound
            self._inbound = inbound

            nodes_by_file: dict[str, list[str]] = {}
            for node_id, node in self._nodes_by_id.items():
                rel = self._workspace_relative(node.get("path"))
                if rel is None:
                    continue
                nodes_by_file.setdefault(rel, []).append(node_id)
            self._nodes_by_file = nodes_by_file

            self._loaded_at_mtime_ns = mtime_ns

    def _resolve_roots(self, node: str) -> list[dict[str, Any]]:
        """Parse the `node` argument and return matching snapshot node dicts."""
        if ":" in node:
            file_part, symbol_part = node.rsplit(":", 1)
        else:
            file_part, symbol_part = node, None
        file_part = file_part.strip().lstrip("./")
        symbol_part = symbol_part.strip() if symbol_part else None

        candidate_ids = self._nodes_by_file.get(file_part, [])
        if not candidate_ids:
            return []

        if symbol_part is None:
            # Every node in the file.
            return [self._nodes_by_id[nid] for nid in candidate_ids]

        # Filter by symbol name (case-sensitive — matches the snapshot's
        # `name` field directly).
        matches: list[dict[str, Any]] = []
        for nid in candidate_ids:
            n = self._nodes_by_id[nid]
            if n.get("name") == symbol_part:
                matches.append(n)
        return matches

    def _emit_neighbor(
        self,
        neighbor_id: str,
        kind: str,
        direction: str,
        distance: int,
        seen_ids: set[str],
        out: list[GraphNeighbor],
        limit: int,
        intra_file_paths: set[str],
    ) -> bool:
        """Append a neighbour if we haven't seen it. Returns False when the
        limit is hit (signals BFS to stop). Skips neighbours whose host file
        is one of the seed files in `intra_file_paths` — keeps the cap
        reserved for cross-file edges that actually navigate to new files."""
        if neighbor_id in seen_ids:
            return True
        node = self._nodes_by_id.get(neighbor_id)
        if node is None:
            return True
        # Skip external markers (`external:call:foo`, `external:module:bar`).
        # These don't have a `path` we can route the model to.
        if neighbor_id.startswith("external:") or not isinstance(node.get("path"), str):
            seen_ids.add(neighbor_id)
            return True
        path = node.get("path")
        if isinstance(path, str) and path in intra_file_paths:
            # Same host file as a seed — skip silently. The seed file is
            # already in matched_roots; the caller can read_file for its
            # internal symbols. We don't even mark seen, in case the same
            # node is also reached via a cross-file path further along.
            return True
        decoded = self._decode_node(node)
        if decoded is None:
            seen_ids.add(neighbor_id)
            return True
        out.append(GraphNeighbor(node=decoded, edge_kind=kind, direction=direction, distance=distance))
        seen_ids.add(neighbor_id)
        return len(out) < limit

    def _decode_node(self, node: dict[str, Any]) -> GraphNode | None:
        path = self._workspace_relative(node.get("path"))
        if path is None:
            return None
        return GraphNode(
            file=path,
            symbol=str(node.get("name", "")),
            kind=str(node.get("kind", "")),
            line=int(node.get("line", 1)) if isinstance(node.get("line"), int) else 1,
            raw_id=str(node.get("id", "")),
        )

    def _workspace_relative(self, raw: object) -> str | None:
        if not isinstance(raw, str) or not raw:
            return None
        try:
            return str(Path(raw).resolve().relative_to(self._workspace_root))
        except (ValueError, OSError):
            return None

    @staticmethod
    def _normalize_edge_kinds(kinds: list[str] | None) -> set[str]:
        if not kinds:
            return set(_ALLOWED_EDGE_KINDS)
        normalized: set[str] = set()
        for raw in kinds:
            if not isinstance(raw, str):
                continue
            cap = raw.strip().capitalize()
            if cap in _ALLOWED_EDGE_KINDS:
                normalized.add(cap)
        return normalized or set(_ALLOWED_EDGE_KINDS)
