from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from agentd.domain.models import (
    Diagnostic,
    PlanEvidenceFile,
    PlanEvidencePack,
    PlanEvidenceSymbol,
)
from agentd.retrieval.chunker import ScoredChunk
from agentd.runtime.adapters import EvidenceAdapter, GenericEvidenceAdapter


def _coerce_int(value: object, default: int) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return default


@dataclass(frozen=True)
class RetrievalContext:
    repository_structure: list[str] = field(default_factory=list)
    related_files: list[str] = field(default_factory=list)
    related_symbols: list[str] = field(default_factory=list)
    graph_neighbors: list[str] = field(default_factory=list)
    # Workspace-relative file paths reached from the matched seed by one Calls/Imports/References/Inherits
    # hop, dedupe by file, seed-files removed, cap 20. The Rust indexer's edges encode actual structural
    # connections (e.g. `engine.py:_run_task → state_machine.py:transition` via Calls + Imports), so this
    # surfaces neighboring files the semantic top-K may not have matched on. Read but never wrote before.
    graph_neighbor_files: list[str] = field(default_factory=list)
    file_outlines: dict[str, list[str]] = field(default_factory=dict)
    diagnostics_excerpt: list[str] = field(default_factory=list)
    snapshot_age_sec: float | None = None
    snapshot_stats: dict[str, int] = field(
        default_factory=lambda: {"node_count": 0, "edge_count": 0, "diagnostic_count": 0}
    )
    file_contents: dict[str, str] = field(default_factory=dict)
    planner_evidence: PlanEvidencePack = field(default_factory=PlanEvidencePack)
    # Semantic chunks from the vector index — used by engine for chunk-scoped patch context.
    # Not serialised into the LLM payload; evidence_files in planner_evidence carries them.
    semantic_chunks: list[ScoredChunk] = field(default_factory=list)

    @classmethod
    def empty(cls) -> "RetrievalContext":
        return cls(
            repository_structure=[],
            related_files=[],
            related_symbols=[],
            graph_neighbors=[],
            graph_neighbor_files=[],
            file_outlines={},
            file_contents={},
            planner_evidence=PlanEvidencePack(),
            diagnostics_excerpt=[],
            snapshot_age_sec=None,
            snapshot_stats={"node_count": 0, "edge_count": 0, "diagnostic_count": 0},
            semantic_chunks=[],
        )

    def as_prompt_payload(self) -> dict[str, object]:
        pe = self.planner_evidence
        # Serialize only the fields the LLM can act on; strip internal bookkeeping.
        if hasattr(pe, "evidence_files"):
            planner_evidence: dict[str, object] = {
                "evidence_files": [
                    f.model_dump(mode="json") if hasattr(f, "model_dump") else f
                    for f in pe.evidence_files
                ],
                "evidence_symbols": [
                    s.model_dump(mode="json") if hasattr(s, "model_dump") else s
                    for s in pe.evidence_symbols
                ],
                "confidence_notes": pe.confidence_notes,
            }
        else:
            # Already a plain dict (e.g. from test stubs)
            planner_evidence = dict(pe)  # type: ignore[arg-type]
        # Only surface diagnostics that are actionable errors — skip import-resolution
        # noise and other warnings that don't inform planning.
        error_diagnostics = [
            d for d in self.diagnostics_excerpt
            if ": error" in d.lower() or d.strip().startswith("error")
        ]
        return {
            "repository_structure": self.repository_structure,
            "file_outlines": self.file_outlines,
            "file_contents": self.file_contents,
            "planner_evidence": planner_evidence,
            "diagnostics_excerpt": error_diagnostics,
            "snapshot_stats": self.snapshot_stats,
            # Cross-file neighbours of the seed symbols/files reached via one
            # Calls/Imports/References/Inherits hop in the symbol graph. Use
            # to extend exploration beyond the semantic top-K — files here are
            # structurally connected to the goal's matched symbols even when
            # their text doesn't surface in semantic search.
            "graph_neighbor_files": self.graph_neighbor_files,
        }


class RetrievalArtifactClient:
    _IGNORED_CONTEXT_DIRS = {
        ".git",
        "node_modules",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        "target",
        "dist",
        ".agentd",
        ".ai-editor",
        ".tmp",
    }

    def __init__(
        self,
        *,
        snapshot_path_template: str | None = None,
        max_age_sec: int = 900,
        index_command_template: str | None = None,
        index_timeout_sec: int = 120,
        evidence_adapter: EvidenceAdapter | None = None,
        semantic_index: object = None,  # SemanticIndex | None — typed as object to avoid hard dep
    ) -> None:
        self._snapshot_path_template = snapshot_path_template
        self._max_age_sec = max_age_sec
        self._index_command_template = index_command_template
        self._index_timeout_sec = index_timeout_sec
        self._evidence_adapter = evidence_adapter or GenericEvidenceAdapter()
        self._semantic_index = semantic_index  # None when semantic retrieval is disabled
        self._last_indexed_snapshot_ms: int = 0  # snapshot generation ms at last index build
        self._building: bool = False
        # Serializes re-embeds: the watch loop can fire several /v1/index/build calls in quick
        # succession (multiple FS events per edit). Coalesce them — skip if a build is already
        # running; the in-flight build reads the snapshot fresh, and any newer change re-triggers.
        self._build_lock = threading.Lock()

    @classmethod
    def from_env(
        cls,
        *,
        evidence_adapter: EvidenceAdapter | None = None,
        semantic_index: object = None,
    ) -> "RetrievalArtifactClient":
        return cls(
            snapshot_path_template=os.getenv("AI_EDITOR_RETRIEVAL_SNAPSHOT_PATH"),
            max_age_sec=int(os.getenv("AI_EDITOR_RETRIEVAL_MAX_AGE_SEC", "900")),
            index_command_template=os.getenv("AI_EDITOR_INDEXER_INDEX_CMD"),
            index_timeout_sec=int(os.getenv("AI_EDITOR_INDEXER_INDEX_TIMEOUT_SEC", "120")),
            evidence_adapter=evidence_adapter,
            semantic_index=semantic_index,
        )

    def semantic_enabled(self) -> bool:
        return self._semantic_index is not None

    def trigger_index_build(self, workspace_path: str) -> object | None:
        """Synchronously build/update the semantic index from the current snapshot.

        Returns the IndexStats from build_or_update, or None if semantic retrieval
        is disabled or the snapshot doesn't exist. Updating _last_indexed_snapshot_ms
        here means load_context() will skip a redundant rebuild on the first task.
        """
        if self._semantic_index is None:
            return None
        snapshot_path = self._resolve_snapshot_path(workspace_path)
        if not snapshot_path.exists():
            return None
        if not self._build_lock.acquire(blocking=False):
            # A build is already running — coalesce. Skipping is safe: the in-flight build
            # reads the snapshot fresh, and any change after it re-triggers a build.
            return None
        try:
            self._building = True
            payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
            stats = self._semantic_index.build_or_update(workspace_path, payload)  # type: ignore[union-attr]
            snapshot_ms = int(payload.get("generated_at_ms", 0) or 0)
            if snapshot_ms > self._last_indexed_snapshot_ms:
                self._last_indexed_snapshot_ms = snapshot_ms
            return stats
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("trigger_index_build failed: %s", exc)
            return None
        finally:
            self._building = False
            self._build_lock.release()

    def index_status(self) -> dict[str, object]:
        return {
            "semantic_enabled": self.semantic_enabled(),
            "building": self._building,
            "last_indexed_snapshot_ms": self._last_indexed_snapshot_ms,
        }

    def load_context(
        self,
        workspace_path: str,
        goal: str,
    ) -> tuple[RetrievalContext, list[Diagnostic]]:
        diagnostics: list[Diagnostic] = []
        snapshot_path = self._resolve_snapshot_path(workspace_path)

        if not snapshot_path.exists():
            diagnostics.extend(self._attempt_build_snapshot(workspace_path, snapshot_path))

        if not snapshot_path.exists():
            diagnostics.append(
                Diagnostic(
                    source="retrieval",
                    message=(
                        "Retrieval snapshot is unavailable; continuing without retrieval context "
                        f"({snapshot_path})"
                    ),
                    level="warning",
                )
            )
            return RetrievalContext.empty(), diagnostics

        try:
            payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            diagnostics.append(
                Diagnostic(
                    source="retrieval",
                    message=(
                        "Retrieval snapshot could not be parsed; continuing without retrieval context "
                        f"({exc})"
                    ),
                    level="warning",
                )
            )
            return RetrievalContext.empty(), diagnostics

        age_sec = self._compute_age_sec(payload)
        if age_sec is not None and age_sec > self._max_age_sec:
            diagnostics.append(
                Diagnostic(
                    source="retrieval",
                    message=(
                        "Retrieval snapshot is stale "
                        f"({age_sec:.1f}s old > {self._max_age_sec}s); continuing with stale context"
                    ),
                    level="warning",
                )
            )

        # Trigger semantic index rebuild when the snapshot has been regenerated.
        # Delta logic inside build_or_update() ensures only changed files are re-embedded.
        if self._semantic_index is not None:
            snapshot_ms = int(payload.get("generated_at_ms", 0) or 0)
            if snapshot_ms > self._last_indexed_snapshot_ms:
                try:
                    self._semantic_index.build_or_update(workspace_path, payload)  # type: ignore[union-attr]
                    self._last_indexed_snapshot_ms = snapshot_ms
                except Exception as exc:
                    diagnostics.append(
                        Diagnostic(
                            source="retrieval",
                            message=f"Semantic index build failed; using graph-only retrieval ({exc})",
                            level="warning",
                        )
                    )

        context = self._build_context(payload, goal, age_sec, workspace_path)

        return context, diagnostics

    def _resolve_snapshot_path(self, workspace_path: str) -> Path:
        workspace = Path(workspace_path).resolve()
        if self._snapshot_path_template:
            rendered = self._snapshot_path_template.format(
                workspace=str(workspace),
                snapshot_path=str(workspace / ".ai-editor/index-snapshot.json"),
            )
            return Path(rendered).expanduser().resolve()
        return (workspace / ".ai-editor/index-snapshot.json").resolve()

    def _attempt_build_snapshot(self, workspace_path: str, snapshot_path: Path) -> list[Diagnostic]:
        command = self._render_index_command(workspace_path, snapshot_path)
        if not command:
            return [
                Diagnostic(
                    source="retrieval",
                    message=(
                        "Retrieval snapshot missing and no index command is configured or auto-detected; "
                        "skipping auto-index"
                    ),
                    level="warning",
                )
            ]

        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self._index_timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return [
                Diagnostic(
                    source="retrieval",
                    message=(
                        "Auto-index command timed out after "
                        f"{self._index_timeout_sec}s: {command}"
                    ),
                    level="warning",
                )
            ]
        except OSError as exc:
            return [
                Diagnostic(
                    source="retrieval",
                    message=f"Auto-index command could not be launched: {exc}",
                    level="warning",
                )
            ]

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            return [
                Diagnostic(
                    source="retrieval",
                    message=(
                        f"Auto-index command failed with exit code {result.returncode}: {command}"
                        + (f" | stderr: {stderr}" if stderr else "")
                    ),
                    level="warning",
                )
            ]

        return []

    def _render_index_command(self, workspace_path: str, snapshot_path: Path) -> str | None:
        workspace = str(Path(workspace_path).resolve())
        if self._index_command_template:
            return self._index_command_template.format(
                workspace=workspace,
                snapshot_path=str(snapshot_path),
            )

        auto_indexer = shutil.which("ai-editor-indexer")
        if not auto_indexer:
            return None

        return (
            f"{shlex.quote(auto_indexer)} index "
            f"--workspace {shlex.quote(workspace)} "
            f"--snapshot-path {shlex.quote(str(snapshot_path))} "
            "--watch 0"
        )

    def _compute_age_sec(self, payload: dict[str, object]) -> float | None:
        generated_raw = payload.get("generated_at_ms")
        generated_ms = None
        if isinstance(generated_raw, int):
            generated_ms = generated_raw
        elif isinstance(generated_raw, float):
            generated_ms = int(generated_raw)
        elif isinstance(generated_raw, str) and generated_raw.isdigit():
            generated_ms = int(generated_raw)

        if generated_ms is None:
            return None
        now_ms = int(time.time() * 1000)
        if generated_ms > now_ms:
            return 0.0
        return (now_ms - generated_ms) / 1000.0

    def _build_context(
        self,
        payload: dict[str, object],
        goal: str,
        age_sec: float | None,
        workspace_path: str,
    ) -> RetrievalContext:
        workspace_root = Path(workspace_path).resolve()
        snapshot_workspace_root = workspace_root
        workspace_root_raw = payload.get("workspace_root")
        if isinstance(workspace_root_raw, str) and workspace_root_raw.strip():
            snapshot_workspace_root = Path(workspace_root_raw).expanduser().resolve()

        graph = payload.get("graph", {})
        nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
        edges = graph.get("edges", []) if isinstance(graph, dict) else []
        diagnostics: list[object] = payload.get("diagnostics") or []  # type: ignore[assignment]
        stats = payload.get("stats", {})

        raw_node_items = [node for node in nodes if isinstance(node, dict)]
        node_items: list[dict[str, object]] = []
        for node in raw_node_items:
            normalized_path = self._normalize_snapshot_path(
                raw_path=node.get("path"),
                workspace_root=workspace_root,
                snapshot_workspace_root=snapshot_workspace_root,
            )
            if normalized_path is None:
                continue
            normalized_node = dict(node)
            normalized_node["path"] = normalized_path
            node_items.append(normalized_node)

        edge_items = [edge for edge in edges if isinstance(edge, dict)]
        diagnostic_items = [item for item in diagnostics if isinstance(item, dict)]

        terms = {
            token.lower()
            for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", goal)
            if len(token) >= 3
        }

        goal_lower = goal.lower()

        # ── Semantic retrieval (if index is ready) ────────────────────────────
        semantic_results: list[ScoredChunk] = []
        semantic_file_scores: dict[str, float] = {}
        if self._semantic_index is not None:
            try:
                sem_index = self._semantic_index
                if sem_index.is_ready():  # type: ignore[union-attr]
                    semantic_results = sem_index.query(  # type: ignore[union-attr]
                        goal,
                        top_k=40,
                        exclude_tests="test" not in goal_lower,
                    )
                    for sr in semantic_results:
                        p = sr.chunk.path
                        if p not in semantic_file_scores or sr.score > semantic_file_scores[p]:
                            semantic_file_scores[p] = sr.score
            except Exception:
                pass  # Graceful fallback to graph-only scoring

        # ── Hybrid scoring: graph token overlap + semantic similarity ─────────
        # Graph score is integer hit count; semantic score is float [0, 1].
        # We normalize graph score to [0, 1] before combining.
        # Weight: 0.35 graph + 0.65 semantic (semantic captures intent better for code goals).
        _GRAPH_WEIGHT = 0.35
        _SEM_WEIGHT = 0.65
        _MAX_GRAPH_SCORE = 10.0  # normalisation ceiling

        scored_nodes: list[tuple[float, dict[str, object]]] = []
        for node in node_items:
            node_name = str(node.get("name", "")).lower()
            node_path = str(node.get("path", "")).lower()
            canonical_path = str(node.get("path", ""))

            # Graph score: symbol-name substring + path-aware matching.
            #
            # Directory segments use weighted segment scoring:
            #   - Exact match or underscore/dot-separated prefix → 1.0 pt
            #   - Hyphen-prefixed extension (e.g. "pydantic" in "pydantic-core") → 0.5 pt
            # This means "pydantic" scores "pydantic/" at 1.0 and "pydantic-core/" at 0.5,
            # giving the exact-name directory an edge over hyphenated siblings — without
            # zeroing out legitimate hyphenated dirs like "agentd-py/" or "react-dom/".
            #
            # The filename segment uses stem-word prefix matching: the file extension
            # is stripped, the stem is split on word characters, and each word is matched
            # if it equals the term or starts with the term (handles plurals like
            # "validators" matching term "validator", but NOT "validation" since
            # "validation" does not start with "validator").
            path_segs = node_path.split("/")
            dir_segs = path_segs[:-1]
            file_seg = path_segs[-1] if path_segs else ""
            file_stem = re.sub(r'\.[^.]+$', '', file_seg).lstrip("_").lstrip("-")
            file_words = re.findall(r'[a-z][a-z0-9]*', file_stem) if file_stem else []

            def _dir_seg_score(seg: str, term: str) -> float:
                s = seg.lstrip("_")
                if s == term or s.startswith(term + "_") or s.startswith(term + "."):
                    return 1.0
                if s.startswith(term + "-"):
                    return 0.5  # partial credit for hyphenated extensions (agentd-py, react-dom, etc.)
                return 0.0

            graph_raw: float = sum(1.0 for term in terms if term in node_name)
            graph_raw += sum(
                max((_dir_seg_score(seg, term) for seg in dir_segs), default=0.0)
                for term in terms
            )
            graph_raw += sum(
                1.0 for term in terms
                if any(w == term or w.startswith(term) for w in file_words)
            )
            graph_raw += self._evidence_adapter.path_relevance_score(
                goal=goal,
                normalized_path=node_path,
            )
            sem_score = semantic_file_scores.get(canonical_path, 0.0)

            # Skip nodes with no signal from either source
            if graph_raw == 0 and sem_score == 0.0:
                continue

            graph_norm = min(graph_raw / _MAX_GRAPH_SCORE, 1.0)
            combined = _GRAPH_WEIGHT * graph_norm + _SEM_WEIGHT * sem_score

            if "test_" in node_name and "test" not in goal_lower:
                combined -= 0.5

            scored_nodes.append((combined, node))

        # Also surface nodes from files the semantic index found but token matching missed
        graph_paths = {str(n.get("path", "")) for _, n in scored_nodes}
        for sem_path, sem_score in semantic_file_scores.items():
            if sem_path in graph_paths:
                continue
            for node in node_items:
                if str(node.get("path", "")) == sem_path:
                    scored_nodes.append((_SEM_WEIGHT * sem_score, node))

        scored_nodes.sort(key=lambda item: (-item[0], str(item[1].get("path", ""))))
        matched_nodes = [node for _, node in scored_nodes[:500]]

        if not matched_nodes:
            matched_nodes = node_items[:8]

        matched_ids = {
            str(node.get("id"))
            for node in matched_nodes
            if isinstance(node.get("id"), str)
        }

        file_scores: dict[str, float] = {}
        for score, node in scored_nodes:
            path = str(node.get("path", ""))
            if path and path not in file_scores:
                file_scores[path] = score

        related_files: list[str] = []
        seen_files: set[str] = set()
        for node in matched_nodes:
            node_path = node.get("path")
            if not isinstance(node_path, str):
                continue
            if node_path in seen_files:
                continue
            related_files.append(node_path)
            seen_files.add(node_path)
            if len(related_files) >= 20:
                break

        related_symbols: list[str] = []
        seen_symbols: set[str] = set()
        for node in matched_nodes:
            node_name = node.get("name")
            if not isinstance(node_name, str):
                continue
            if str(node.get("kind")) == "File":
                continue
            if node_name in seen_symbols:
                continue
            related_symbols.append(node_name)
            seen_symbols.add(node_name)
            if len(related_symbols) >= 40:
                break

        graph_neighbors: set[str] = set()
        for edge in edge_items:
            source = edge.get("from")
            target = edge.get("to")
            if isinstance(source, str) and source in matched_ids and isinstance(target, str):
                graph_neighbors.add(target)
            if isinstance(target, str) and target in matched_ids and isinstance(source, str):
                graph_neighbors.add(source)

        # Filter out neighbors whose IDs reference ignored directories
        filtered_neighbors = [
            n for n in sorted(graph_neighbors)
            if not any(f"/{ignored}/" in n for ignored in self._IGNORED_CONTEXT_DIRS)
        ]
        neighbors = filtered_neighbors[:50]

        # Flatten neighbor node-ids to workspace-relative FILE paths the planner
        # can actually read. We re-walk edge_items rather than reusing `neighbors`
        # because that list mixes intra-file method nodes (engine.py's 79 symbols
        # generate many intra-file edges) and cross-file structural edges in a
        # single alphabetically-sorted 50-cap — intra-file ones squeeze out the
        # cross-file targets we actually need. Here we drop neighbors whose host
        # file is already a seed file, and rank by distinct edge count so the
        # most-connected new files come first.
        nodes_by_id = {
            str(n.get("id")): n for n in nodes
            if isinstance(n.get("id"), str)
        }
        # Seed files = files the planner is actually likely to see as evidence.
        # Union three sources: matched_nodes (keyword + score), the semantic top-K
        # chunk paths (planner_evidence.evidence_files draws from these — they
        # are the files the planner sees most directly), and the resolved paths
        # of any matched file-level node. Without the semantic union, files like
        # engine.py — surfaced only by the embedding model — never become seeds,
        # and their structural neighbours (state_machine.py via Imports, etc.)
        # are missed.
        seed_files: set[str] = {
            str(n.get("path")) for n in matched_nodes
            if isinstance(n.get("path"), str)
        }
        for sr in semantic_results:
            sem_path = sr.chunk.path
            if not sem_path:
                continue
            try:
                abs_sem = (workspace_root / sem_path).resolve()
            except (ValueError, OSError):
                continue
            seed_files.add(str(abs_sem))
        ws_resolved = workspace_root.resolve()
        neighbor_file_hits: dict[str, int] = {}
        for edge in edge_items:
            src = edge.get("from")
            tgt = edge.get("to")
            src_node = nodes_by_id.get(src) if isinstance(src, str) else None
            tgt_node = nodes_by_id.get(tgt) if isinstance(tgt, str) else None
            src_path = src_node.get("path") if isinstance(src_node, dict) else None
            tgt_path = tgt_node.get("path") if isinstance(tgt_node, dict) else None
            # A node is a "seed" if its id is in matched_ids (symbol-level match)
            # OR its host file is a seed file (file-level reach). The file-level
            # check is what unlocks Python file→file Imports edges, whose endpoints
            # are file nodes — never themselves keyword/semantic-matched.
            src_is_seed = (
                (isinstance(src, str) and src in matched_ids)
                or (isinstance(src_path, str) and src_path in seed_files)
            )
            tgt_is_seed = (
                (isinstance(tgt, str) and tgt in matched_ids)
                or (isinstance(tgt_path, str) and tgt_path in seed_files)
            )
            if src_is_seed == tgt_is_seed:
                # Both seeds or neither seed → no new file to surface.
                continue
            neighbor_node = tgt_node if src_is_seed else src_node
            if not isinstance(neighbor_node, dict):
                continue
            path_str = neighbor_node.get("path")
            if not isinstance(path_str, str) or path_str in seed_files:
                continue
            if any(f"/{ig}/" in path_str for ig in self._IGNORED_CONTEXT_DIRS):
                continue
            try:
                rel = Path(path_str).resolve().relative_to(ws_resolved)
            except (ValueError, OSError):
                continue
            rel_str = str(rel)
            neighbor_file_hits[rel_str] = neighbor_file_hits.get(rel_str, 0) + 1

        # Sort by hit count (descending), then path (ascending) for deterministic
        # ties; cap at 20.
        graph_neighbor_files: list[str] = [
            path for path, _ in sorted(
                neighbor_file_hits.items(),
                key=lambda item: (-item[1], item[0]),
            )[:20]
        ]

        # Extract structural outlines for top files.
        # When semantic search is active, only include files above threshold to suppress noise.
        # Without semantic, graph scores are much smaller (integer hits / 10), so no threshold.
        _OUTLINE_SCORE_THRESHOLD = 0.45
        file_outlines: dict[str, list[str]] = {}
        candidates = related_files[:8]
        if semantic_results:
            candidates = [f for f in candidates if file_scores.get(f, 0.0) >= _OUTLINE_SCORE_THRESHOLD]
            if not candidates:
                candidates = related_files[:8]  # fallback if all filtered
        top_files = candidates

        _SYMBOL_KINDS = {"Class", "Function", "Method", "Interface", "Protocol"}
        _GAP_THRESHOLD = 30      # lines between symbols before inserting a gap marker
        _CHUNK_BUFFER = 10       # lines around each chunk when limiting large files
        _FULL_LISTING_MAX = 100  # files with ≤ this many symbols get a full listing

        # Per-file lookup of matched chunk line ranges (used for large-file fallback).
        file_chunk_ranges: dict[str, list[tuple[int, int]]] = {}
        for sr in semantic_results:
            p = sr.chunk.path
            if p not in file_chunk_ranges:
                file_chunk_ranges[p] = []
            file_chunk_ranges[p].append((sr.chunk.line_start, sr.chunk.line_end))

        def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
            merged: list[list[int]] = []
            for s, e in sorted(ranges):
                s = max(1, s - _CHUNK_BUFFER)
                e = e + _CHUNK_BUFFER
                if merged and s <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], e)
                else:
                    merged.append([s, e])
            return [(s, e) for s, e in merged]

        def _build_outline(target_file: str) -> list[str]:
            file_nodes = sorted(
                [n for n in node_items
                 if n.get("path") == target_file and str(n.get("kind", "")) in _SYMBOL_KINDS],
                key=lambda x: _coerce_int(x.get("line"), 0),
            )
            if not file_nodes:
                return []

            def _fmt(n: dict[str, object]) -> str:
                line = n.get("line")
                return f"{n.get('kind')}: {n.get('name')}" + (f" (line {line})" if line else "")

            if len(file_nodes) <= _FULL_LISTING_MAX:
                # Small enough: show every symbol in line order; gap markers for large code gaps.
                result: list[str] = []
                prev_line = 0
                for n in file_nodes:
                    line = _coerce_int(n.get("line"), 0)
                    if prev_line and line - prev_line > _GAP_THRESHOLD:
                        result.append(f"... (lines {prev_line + 1}–{line - 1} omitted)")
                    result.append(_fmt(n))
                    prev_line = line
                return result

            # Large file: show only symbols near semantic chunk hits.
            chunk_ranges = file_chunk_ranges.get(target_file)
            if not chunk_ranges:
                result = [_fmt(n) for n in file_nodes[:20]]
                if len(file_nodes) > 20:
                    result.append(f"... ({len(file_nodes) - 20} more symbols omitted)")
                return result

            merged = _merge_ranges(chunk_ranges)
            result = []
            prev_end = 0
            for rs, re in merged:
                if prev_end > 0 and rs > prev_end + 1:
                    result.append(f"... (lines {prev_end + 1}–{rs - 1} omitted)")
                for n in file_nodes:
                    line = _coerce_int(n.get("line"), 0)
                    if rs <= line <= re:
                        result.append(_fmt(n))
                prev_end = re
            last_line = _coerce_int(file_nodes[-1].get("line"), 0)
            trailing = [n for n in file_nodes if _coerce_int(n.get("line"), 0) > prev_end]
            if trailing:
                result.append(f"... (lines {prev_end + 1}–{last_line} omitted, {len(trailing)} symbols)")
            return result

        for target_file in top_files:
            outlines = _build_outline(target_file)
            if outlines:
                file_outlines[target_file] = outlines

        # Same-directory sibling expansion: for each top file, add outlines for
        # sibling files in the same directory that have semantic signal.
        # Sorted by semantic score so the most relevant siblings are added first.
        # This surfaces co-located files (e.g. errors.py next to validators.py)
        # that the graph doesn't link via cross-file import edges for Python.
        _MAX_OUTLINE_FILES = 12
        indexed_paths = {str(n.get("path", "")) for n in node_items if n.get("kind") == "File"}
        seen_outline_files = set(file_outlines.keys())
        top_dirs = {str(Path(f).parent) for f in top_files}

        sibling_candidates: list[tuple[float, str]] = []
        for sibling in indexed_paths:
            if sibling in seen_outline_files:
                continue
            if str(Path(sibling).parent) not in top_dirs:
                continue
            # Include siblings with semantic signal; fall back to any sibling if no semantic index
            sem_score = semantic_file_scores.get(sibling, 0.0)
            if semantic_results and sem_score == 0.0:
                continue
            sibling_candidates.append((sem_score, sibling))

        sibling_candidates.sort(key=lambda x: -x[0])
        for _, sibling in sibling_candidates:
            if len(file_outlines) >= _MAX_OUTLINE_FILES:
                break
            outlines = _build_outline(sibling)
            if outlines:
                file_outlines[sibling] = outlines
                seen_outline_files.add(sibling)

        relevant_files = set(top_files)
        diagnostics_excerpt: list[str] = []
        for item in diagnostic_items:
            excerpt = self._format_diagnostic_excerpt(
                item,
                workspace_root=workspace_root,
                snapshot_workspace_root=snapshot_workspace_root,
            )
            if excerpt is None:
                continue
            normalized_file = excerpt.split(":", 1)[0]
            if relevant_files and normalized_file not in relevant_files:
                continue
            diagnostics_excerpt.append(excerpt)
            if len(diagnostics_excerpt) >= 12:
                break

        node_count = _coerce_int(
            stats.get("node_count") if isinstance(stats, dict) else None,
            len(node_items),
        )
        edge_count = _coerce_int(
            stats.get("edge_count") if isinstance(stats, dict) else None,
            len(edge_items),
        )
        diagnostic_count = _coerce_int(
            stats.get("diagnostic_count") if isinstance(stats, dict) else None,
            len(diagnostic_items),
        )

        repository_structure: list[str] = []
        for root, dirs, files in os.walk(workspace_root):
            rel_root = Path(root).relative_to(workspace_root)
            if self._is_ignored_relative_path(rel_root):
                dirs.clear()
                continue

            level = len(rel_root.parts)
            if level > 5:
                dirs.clear()
                continue

            indent = "  " * level
            display_name = "." if str(rel_root) == "." else rel_root.name

            valid_dirs = [d for d in dirs if not self._is_ignored_relative_path(rel_root / d)]
            valid_files = [f for f in files if self._is_supported_source_path(Path(f))]
            if valid_dirs or valid_files:
                summary = f"{indent}{display_name}/ ({len(valid_dirs)} dirs, {len(valid_files)} source files)"
                repository_structure.append(summary)

        planner_evidence = self._build_planner_evidence(
            workspace_root=workspace_root,
            goal_terms=terms,
            matched_nodes=matched_nodes,
            top_files=top_files,
            snapshot_age_sec=age_sec,
            semantic_results=semantic_results,
        )

        return RetrievalContext(
            repository_structure=repository_structure,
            related_files=related_files,
            related_symbols=related_symbols,
            graph_neighbors=neighbors,
            graph_neighbor_files=graph_neighbor_files,
            file_outlines=file_outlines,
            diagnostics_excerpt=diagnostics_excerpt,
            snapshot_age_sec=age_sec,
            snapshot_stats={
                "node_count": node_count,
                "edge_count": edge_count,
                "diagnostic_count": diagnostic_count,
            },
            planner_evidence=planner_evidence,
            semantic_chunks=semantic_results,
        )

    def _normalize_snapshot_path(
        self,
        *,
        raw_path: object,
        workspace_root: Path,
        snapshot_workspace_root: Path,
    ) -> str | None:
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None

        path_str = raw_path.strip()
        candidate = Path(path_str).expanduser()
        
        # If the path is absolute and doesn't match workspace_root,
        # it might be from a different workspace/shadow environment.
        # We try to extract the project-relative part by finding the common suffix.
        if candidate.is_absolute() and not self._is_within(candidate, workspace_root):
            # STRATEGY: Find the project-relative path by looking for the last 
            # occurrence of a project sub-directory that exists in the current workspace.
            # This is more robust than hardcoding markers.
            parts = candidate.parts
            for i in range(len(parts)):
                # Take the suffix from index i to end
                suffix = Path(*parts[i:])
                if (workspace_root / suffix).exists():
                    return suffix.as_posix()

        # Standard resolution for relative paths or paths already within workspace_root
        resolved = candidate.resolve() if candidate.is_absolute() else (snapshot_workspace_root / candidate).resolve()
        if not self._is_within(resolved, workspace_root):
            # Final fallback: if it's a relative path that doesn't resolve within snapshot_workspace_root,
            # try resolving it relative to the current workspace_root.
            if not candidate.is_absolute():
                fallback = (workspace_root / candidate).resolve()
                if self._is_within(fallback, workspace_root):
                    return candidate.as_posix()
            return None

        relative = resolved.relative_to(workspace_root)
        if self._is_ignored_relative_path(relative):
            return None
        return relative.as_posix()

    def _is_within(self, path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _is_ignored_relative_path(self, relative_path: Path) -> bool:
        return any(part in self._IGNORED_CONTEXT_DIRS for part in relative_path.parts)

    def _is_supported_source_path(self, path: Path) -> bool:
        # Same extensions as indexer-rs
        ext = path.suffix.lower()
        return ext in {".ts", ".tsx", ".py", ".rs"}

    def _build_workspace_files_index(self, workspace_root: Path) -> list[str]:
        indexed: list[str] = []
        for root, dirs, files in os.walk(workspace_root):
            rel_root = Path(root).relative_to(workspace_root)
            if self._is_ignored_relative_path(rel_root):
                dirs.clear()
                continue
            dirs[:] = sorted(d for d in dirs if not self._is_ignored_relative_path(rel_root / d))
            for file_name in sorted(files):
                rel_path = (Path(root) / file_name).relative_to(workspace_root)
                if self._is_ignored_relative_path(rel_path):
                    continue
                indexed.append(rel_path.as_posix())
                if len(indexed) >= 15000:
                    return indexed
        return indexed

    def _build_planner_evidence(
        self,
        *,
        workspace_root: Path,
        goal_terms: set[str],
        matched_nodes: list[dict[str, object]],
        top_files: list[str],
        snapshot_age_sec: float | None,
        semantic_results: list[ScoredChunk] | None = None,
    ) -> PlanEvidencePack:
        evidence_files: list[PlanEvidenceFile] = []
        evidence_symbols: list[PlanEvidenceSymbol] = []

        # Prefer semantic chunks as evidence files — they are semantically grounded to the
        # goal rather than using a keyword-anchor heuristic within the file.
        if semantic_results:
            seen_paths: set[str] = set()
            for sr in semantic_results[:12]:
                chunk = sr.chunk
                if chunk.path in seen_paths:
                    continue
                seen_paths.add(chunk.path)
                abs_path = workspace_root / chunk.path
                if not abs_path.exists():
                    continue
                evidence_files.append(
                    PlanEvidenceFile(
                        path=chunk.path,
                        excerpt=chunk.text_with_lines,
                        rationale=(
                            f"semantic match (score={sr.score:.2f}): "
                            f"{chunk.kind} {chunk.name}"
                            + (f" in {chunk.parent_name}" if chunk.parent_name else "")
                        ),
                        line_start=chunk.line_start,
                        line_end=chunk.line_end,
                    )
                )
                if len(evidence_files) >= 8:
                    break
        else:
            # Fallback: keyword-anchored excerpts (original behaviour)
            for target_file in top_files[:8]:
                excerpt_info = self._extract_file_evidence(
                    workspace_root=workspace_root,
                    file_path=target_file,
                    matched_nodes=matched_nodes,
                    goal_terms=goal_terms,
                )
                if excerpt_info is None:
                    continue
                evidence_files.append(
                    PlanEvidenceFile(
                        path=target_file,
                        excerpt=excerpt_info["excerpt"],
                        rationale=excerpt_info["rationale"],
                        line_start=excerpt_info["line_start"],
                        line_end=excerpt_info["line_end"],
                    )
                )

        seen_symbol_keys: set[tuple[str, str]] = set()
        for node in matched_nodes:
            node_name = str(node.get("name", "")).strip()
            node_kind = str(node.get("kind", "")).strip()
            node_file = node.get("path")
            if not node_name or not isinstance(node_file, str) or node_kind == "File":
                continue
            symbol_key = (node_file, node_name)
            if symbol_key in seen_symbol_keys:
                continue
            seen_symbol_keys.add(symbol_key)
            snippet = self._extract_symbol_snippet(
                workspace_root=workspace_root,
                file_path=node_file,
                line=_coerce_int(node.get("line"), 0) or None,
                symbol=node_name,
            )
            evidence_symbols.append(
                PlanEvidenceSymbol(
                    name=node_name,
                    kind=node_kind,
                    file=node_file,
                    line=_coerce_int(node.get("line"), 0) or None,
                    snippet=snippet,
                )
            )
            if len(evidence_symbols) >= 16:
                break

        confidence_notes: list[str] = []
        if snapshot_age_sec is not None and snapshot_age_sec > self._max_age_sec:
            confidence_notes.append(
                f"Snapshot is stale ({snapshot_age_sec:.1f}s old); prefer evidence excerpts over graph freshness."
            )
        if not evidence_files:
            confidence_notes.append("No grounded file excerpts were extracted from the current workspace.")
        if not evidence_symbols:
            confidence_notes.append("No grounded symbol evidence was extracted for this goal.")

        return PlanEvidencePack(
            evidence_files=evidence_files,
            evidence_symbols=evidence_symbols,
            confidence_notes=confidence_notes,
        )

    def _extract_file_evidence(
        self,
        *,
        workspace_root: Path,
        file_path: str,
        matched_nodes: list[dict[str, object]],
        goal_terms: set[str],
    ) -> dict[str, object] | None:
        absolute_path = workspace_root / file_path
        if not absolute_path.exists() or not absolute_path.is_file():
            return None
        try:
            lines = absolute_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None

        file_nodes = [node for node in matched_nodes if node.get("path") == file_path]
        anchor_line: int | None = None
        rationale = "keyword-grounded excerpt"
        if file_nodes:
            line_values = [
                _coerce_int(node.get("line"), 0)
                for node in file_nodes
                if _coerce_int(node.get("line"), 0) > 0
            ]
            if line_values:
                anchor_line = min(line_values)
                rationale = "symbol-grounded excerpt"

        if anchor_line is None and goal_terms:
            for index, line in enumerate(lines, start=1):
                lowered = line.lower()
                if any(term in lowered for term in goal_terms):
                    anchor_line = index
                    break

        if anchor_line is None:
            anchor_line = 1

        start = max(1, anchor_line - 3)
        end = min(len(lines), start + 11)
        excerpt = "\n".join(lines[start - 1 : end])
        return {
            "excerpt": excerpt,
            "rationale": rationale,
            "line_start": start,
            "line_end": end,
        }

    def _extract_symbol_snippet(
        self,
        *,
        workspace_root: Path,
        file_path: str,
        line: int | None,
        symbol: str,
    ) -> str | None:
        absolute_path = workspace_root / file_path
        if not absolute_path.exists() or not absolute_path.is_file():
            return None
        try:
            lines = absolute_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None

        if line is None or line <= 0:
            for index, content in enumerate(lines, start=1):
                if symbol in content:
                    line = index
                    break
        if line is None or line <= 0:
            return None

        start = max(1, line - 2)
        end = min(len(lines), line + 3)
        return "\n".join(lines[start - 1 : end])

    def _format_diagnostic_excerpt(
        self,
        item: dict[str, object],
        *,
        workspace_root: Path,
        snapshot_workspace_root: Path,
    ) -> str | None:
        normalized_file = self._normalize_snapshot_path(
            raw_path=item.get("file"),
            workspace_root=workspace_root,
            snapshot_workspace_root=snapshot_workspace_root,
        )
        if normalized_file is None:
            return None

        line = item.get("line", "?")
        message = item.get("message", "")
        return f"{normalized_file}:{line}: {message}"
