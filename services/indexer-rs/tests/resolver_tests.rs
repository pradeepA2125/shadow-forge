//! End-to-end behaviour of the LSP resolution stage, with a deterministic stub
//! oracle in place of the real `LspAdapter`. Production LSP wiring is covered
//! by the live snapshot probe (Phase 8 of the planning doc) — these tests pin
//! the resolver's contract independent of pyright/tsserver availability.
use std::collections::HashMap;
use std::path::{Path, PathBuf};

use ai_editor_indexer::graph::{
    EdgeKind, PlaceholderEdge, SymbolEdge, SymbolGraph, SymbolKind, SymbolNode,
};
use ai_editor_indexer::lsp::LspLocation;
use ai_editor_indexer::resolver::{resolve_inner, CallSiteOracle};

#[derive(Default)]
struct StubOracle {
    definitions: HashMap<(PathBuf, u32, u32), LspLocation>,
    implementations: HashMap<(PathBuf, u32, u32), Vec<LspLocation>>,
}

impl StubOracle {
    fn set_definition(&mut self, at: (PathBuf, u32, u32), to: LspLocation) {
        self.definitions.insert(at, to);
    }
    fn set_implementations(&mut self, at: (PathBuf, u32, u32), to: Vec<LspLocation>) {
        self.implementations.insert(at, to);
    }
}

impl CallSiteOracle for StubOracle {
    fn resolve_definition(
        &mut self,
        file_path: &Path,
        line: u32,
        character: u32,
    ) -> Option<LspLocation> {
        self.definitions
            .get(&(file_path.to_path_buf(), line, character))
            .cloned()
    }
    fn resolve_implementations(
        &mut self,
        file_path: &Path,
        line: u32,
        character: u32,
    ) -> Vec<LspLocation> {
        self.implementations
            .get(&(file_path.to_path_buf(), line, character))
            .cloned()
            .unwrap_or_default()
    }
}

fn fixture_graph() -> (SymbolGraph, PathBuf, PathBuf, PathBuf, PathBuf) {
    // Workspace shape:
    //   src/engine.py      — calls transition() and save()
    //   src/state_machine.py — defines transition (line 5)
    //   src/storage/base.py — declares TaskStore.save (Protocol, line 10)
    //   src/storage/sqlite.py — SQLiteTaskStore.save (line 12)
    //   src/storage/memory.py — InMemoryTaskStore.save (line 8)
    let mut graph = SymbolGraph::default();

    let engine_path = PathBuf::from("/ws/src/engine.py");
    let sm_path = PathBuf::from("/ws/src/state_machine.py");
    let sqlite_path = PathBuf::from("/ws/src/storage/sqlite.py");
    let memory_path = PathBuf::from("/ws/src/storage/memory.py");

    // engine.py:run_task function symbol.
    graph.upsert_node(SymbolNode {
        id: "fn:engine:run_task".to_string(),
        path: engine_path.to_string_lossy().to_string(),
        name: "run_task".to_string(),
        kind: SymbolKind::Function,
        line: 1,
    });

    // state_machine.py file node + transition function at line 5.
    graph.upsert_node(SymbolNode {
        id: "file:sm".to_string(),
        path: sm_path.to_string_lossy().to_string(),
        name: "state_machine.py".to_string(),
        kind: SymbolKind::File,
        line: 1,
    });
    graph.upsert_node(SymbolNode {
        id: "fn:sm:transition".to_string(),
        path: sm_path.to_string_lossy().to_string(),
        name: "transition".to_string(),
        kind: SymbolKind::Function,
        line: 5,
    });

    // base.py TaskStore.save declaration at line 10.
    let base_path = PathBuf::from("/ws/src/storage/base.py");
    graph.upsert_node(SymbolNode {
        id: "method:base:TaskStore.save".to_string(),
        path: base_path.to_string_lossy().to_string(),
        name: "TaskStore.save".to_string(),
        kind: SymbolKind::Method,
        line: 10,
    });

    // sqlite.py SQLiteTaskStore.save at line 12.
    graph.upsert_node(SymbolNode {
        id: "method:sqlite:SQLite.save".to_string(),
        path: sqlite_path.to_string_lossy().to_string(),
        name: "SQLiteTaskStore.save".to_string(),
        kind: SymbolKind::Method,
        line: 12,
    });

    // memory.py InMemoryTaskStore.save at line 8.
    graph.upsert_node(SymbolNode {
        id: "method:memory:InMemory.save".to_string(),
        path: memory_path.to_string_lossy().to_string(),
        name: "InMemoryTaskStore.save".to_string(),
        kind: SymbolKind::Method,
        line: 8,
    });

    (graph, engine_path, sm_path, sqlite_path, memory_path)
}

fn placeholder(
    from_id: &str,
    external_name: &str,
    file_path: &Path,
    line: u32,
    character: u32,
) -> (PlaceholderEdge, SymbolEdge, String) {
    let external_id = format!("external:call:{external_name}");
    let placeholder = PlaceholderEdge {
        from_id: from_id.to_string(),
        external_to_id: external_id.clone(),
        file_path: file_path.to_path_buf(),
        line,
        character,
        edge_kind: EdgeKind::Calls,
    };
    let edge = SymbolEdge {
        from: from_id.to_string(),
        to: external_id.clone(),
        kind: EdgeKind::Calls,
    };
    (placeholder, edge, external_id)
}

#[test]
fn resolves_simple_cross_file_call() {
    let (mut graph, engine_path, sm_path, _, _) = fixture_graph();

    // Add the parser's pre-emitted external Calls edge.
    let (p, external_edge, external_id) =
        placeholder("fn:engine:run_task", "transition", &engine_path, 4, 4);
    // Need the external node itself to live in the graph.
    graph.upsert_node(SymbolNode {
        id: external_id.clone(),
        path: engine_path.to_string_lossy().to_string(),
        name: "transition".to_string(),
        kind: SymbolKind::Function,
        line: 5,
    });
    graph.add_edge(external_edge.clone());

    // Stub LSP: definition at sm.py line 4 (0-indexed → graph line 5).
    let mut oracle = StubOracle::default();
    oracle.set_definition(
        (engine_path.clone(), 4, 4),
        LspLocation {
            path: sm_path.clone(),
            line: 4,
            character: 0,
        },
    );

    resolve_inner(&mut graph, &mut oracle, vec![p]);

    // External edge gone; resolved Calls edge present.
    let edges = graph.all_edges();
    assert!(
        !edges
            .iter()
            .any(|e| e.kind == EdgeKind::Calls && e.to == external_id),
        "external Calls edge should have been removed"
    );
    assert!(
        edges.iter().any(|e| e.kind == EdgeKind::Calls
            && e.from == "fn:engine:run_task"
            && e.to == "fn:sm:transition"),
        "resolved Calls edge to sm.py:transition missing; edges: {:#?}",
        edges
    );
}

#[test]
fn protocol_call_fans_out_implements_edges() {
    let (mut graph, engine_path, _, sqlite_path, memory_path) = fixture_graph();
    let base_path = PathBuf::from("/ws/src/storage/base.py");

    let (p, ext_edge, ext_id) =
        placeholder("fn:engine:run_task", "save", &engine_path, 8, 16);
    graph.upsert_node(SymbolNode {
        id: ext_id.clone(),
        path: engine_path.to_string_lossy().to_string(),
        name: "save".to_string(),
        kind: SymbolKind::Function,
        line: 9,
    });
    graph.add_edge(ext_edge);

    let mut oracle = StubOracle::default();
    // Definition → TaskStore.save Protocol method at base.py line 9 (0-idx).
    oracle.set_definition(
        (engine_path.clone(), 8, 16),
        LspLocation {
            path: base_path.clone(),
            line: 9,
            character: 8,
        },
    );
    // Implementations → SQLite + InMemory variants.
    oracle.set_implementations(
        (engine_path.clone(), 8, 16),
        vec![
            LspLocation {
                path: sqlite_path.clone(),
                line: 11, // 0-idx → graph line 12
                character: 8,
            },
            LspLocation {
                path: memory_path.clone(),
                line: 7, // 0-idx → graph line 8
                character: 8,
            },
        ],
    );

    resolve_inner(&mut graph, &mut oracle, vec![p]);

    let edges = graph.all_edges();
    let implements: Vec<&SymbolEdge> = edges
        .iter()
        .filter(|e| e.kind == EdgeKind::Implements)
        .collect();

    assert_eq!(
        implements.len(),
        2,
        "expected 2 Implements edges (SQLite + InMemory); got {:#?}",
        implements
    );
    assert!(
        implements
            .iter()
            .any(|e| e.from == "method:sqlite:SQLite.save"
                && e.to == "method:base:TaskStore.save"),
        "missing SQLite → TaskStore.save Implements edge"
    );
    assert!(
        implements
            .iter()
            .any(|e| e.from == "method:memory:InMemory.save"
                && e.to == "method:base:TaskStore.save"),
        "missing InMemory → TaskStore.save Implements edge"
    );
}

#[test]
fn unresolved_definition_keeps_external_edge() {
    let (mut graph, engine_path, _, _, _) = fixture_graph();
    let (p, ext_edge, ext_id) =
        placeholder("fn:engine:run_task", "dynamic", &engine_path, 1, 1);
    graph.upsert_node(SymbolNode {
        id: ext_id.clone(),
        path: engine_path.to_string_lossy().to_string(),
        name: "dynamic".to_string(),
        kind: SymbolKind::Function,
        line: 2,
    });
    graph.add_edge(ext_edge);

    // Oracle returns None for definition.
    let mut oracle = StubOracle::default();
    resolve_inner(&mut graph, &mut oracle, vec![p]);

    // External edge survives untouched.
    assert!(
        graph
            .all_edges()
            .iter()
            .any(|e| e.kind == EdgeKind::Calls && e.to == ext_id),
        "external Calls edge should have been kept when LSP could not resolve"
    );
    // No Implements edges introduced.
    assert!(graph
        .all_edges()
        .iter()
        .all(|e| e.kind != EdgeKind::Implements));
}

#[test]
fn definition_to_external_file_keeps_external_edge() {
    // LSP can return a location outside the workspace (stdlib, site-packages,
    // node_modules). Those don't have a workspace node so `nearest_node_at`
    // returns None — we must leave the external edge in place.
    let (mut graph, engine_path, _, _, _) = fixture_graph();
    let (p, ext_edge, ext_id) =
        placeholder("fn:engine:run_task", "json_loads", &engine_path, 2, 2);
    graph.upsert_node(SymbolNode {
        id: ext_id.clone(),
        path: engine_path.to_string_lossy().to_string(),
        name: "json_loads".to_string(),
        kind: SymbolKind::Function,
        line: 3,
    });
    graph.add_edge(ext_edge);

    let mut oracle = StubOracle::default();
    oracle.set_definition(
        (engine_path.clone(), 2, 2),
        LspLocation {
            path: PathBuf::from("/usr/lib/python3.13/json/__init__.py"),
            line: 100,
            character: 0,
        },
    );

    resolve_inner(&mut graph, &mut oracle, vec![p]);

    assert!(
        graph
            .all_edges()
            .iter()
            .any(|e| e.kind == EdgeKind::Calls && e.to == ext_id),
        "external Calls edge should have been kept (LSP target outside workspace)"
    );
}

#[test]
fn prune_then_re_resolve_does_not_leave_orphans() {
    // The watch-loop sequence: parse → prune Calls/Implements originating in
    // file → re-parse → resolve. Verify the prune correctly removes ONLY
    // those edges, leaving References/Inherits/Imports from the same file
    // alone.
    let (mut graph, engine_path, _, _, _) = fixture_graph();

    // Engine.py has both a Calls edge (to be pruned) and a References edge
    // (must survive).
    graph.add_edge(SymbolEdge {
        from: "fn:engine:run_task".to_string(),
        to: "external:call:doomed".to_string(),
        kind: EdgeKind::Calls,
    });
    graph.add_edge(SymbolEdge {
        from: "fn:engine:run_task".to_string(),
        to: "fn:engine:run_task".to_string(),
        kind: EdgeKind::References,
    });

    graph.prune_resolved_calls_from_file(&engine_path);

    let edges = graph.all_edges();
    assert!(
        !edges.iter().any(|e| e.kind == EdgeKind::Calls
            && e.from == "fn:engine:run_task"),
        "Calls edges originating in engine.py should have been pruned"
    );
    assert!(
        edges.iter().any(|e| e.kind == EdgeKind::References
            && e.from == "fn:engine:run_task"),
        "References edge from engine.py should have survived prune"
    );
}

#[test]
fn take_placeholders_for_file_filters_correctly() {
    let mut graph = SymbolGraph::default();
    let a = PathBuf::from("/ws/a.py");
    let b = PathBuf::from("/ws/b.py");

    graph.push_placeholder(PlaceholderEdge {
        from_id: "x".to_string(),
        external_to_id: "external:call:f".to_string(),
        file_path: a.clone(),
        line: 1,
        character: 0,
        edge_kind: EdgeKind::Calls,
    });
    graph.push_placeholder(PlaceholderEdge {
        from_id: "y".to_string(),
        external_to_id: "external:call:g".to_string(),
        file_path: b.clone(),
        line: 1,
        character: 0,
        edge_kind: EdgeKind::Calls,
    });

    let drained = graph.take_placeholders_for_file(&a);
    assert_eq!(drained.len(), 1);
    assert_eq!(drained[0].file_path, a);

    let remaining = graph.take_placeholders();
    assert_eq!(remaining.len(), 1);
    assert_eq!(remaining[0].file_path, b);
}
