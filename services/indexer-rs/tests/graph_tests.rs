use ai_editor_indexer::graph::{
    EdgeKind, GraphQueryMode, GraphQueryRequest, SymbolEdge, SymbolGraph, SymbolKind, SymbolNode,
};

#[test]
fn graph_upsert_and_edge_insert() {
    let mut graph = SymbolGraph::default();
    graph.upsert_node(SymbolNode {
        id: "f:1".to_string(),
        path: "src/main.rs".to_string(),
        name: "main.rs".to_string(),
        kind: SymbolKind::File,
        line: 1,
    });

    graph.add_edge(SymbolEdge {
        from: "f:1".to_string(),
        to: "f:1".to_string(),
        kind: EdgeKind::References,
    });

    assert_eq!(graph.node_count(), 1);
    assert_eq!(graph.edge_count(), 1);
}

#[test]
fn graph_query_expands_neighbors_by_depth() {
    let mut graph = SymbolGraph::default();
    graph.upsert_node(SymbolNode {
        id: "file:1".to_string(),
        path: "src/a.ts".to_string(),
        name: "a.ts".to_string(),
        kind: SymbolKind::File,
        line: 1,
    });
    graph.upsert_node(SymbolNode {
        id: "function:file:1:build".to_string(),
        path: "src/a.ts".to_string(),
        name: "build".to_string(),
        kind: SymbolKind::Function,
        line: 2,
    });
    graph.upsert_node(SymbolNode {
        id: "call:fetch".to_string(),
        path: "src/a.ts".to_string(),
        name: "fetch".to_string(),
        kind: SymbolKind::Function,
        line: 3,
    });
    graph.add_edge(SymbolEdge {
        from: "file:1".to_string(),
        to: "function:file:1:build".to_string(),
        kind: EdgeKind::References,
    });
    graph.add_edge(SymbolEdge {
        from: "function:file:1:build".to_string(),
        to: "call:fetch".to_string(),
        kind: EdgeKind::Calls,
    });

    let result = graph.query(&GraphQueryRequest {
        mode: GraphQueryMode::FilePath,
        value: "src/a.ts".to_string(),
        depth: 2,
        limit: 20,
        edge_kinds: None,
    });

    assert_eq!(result.roots, vec!["file:1".to_string()]);
    assert_eq!(result.nodes.len(), 3);
    assert_eq!(result.edges.len(), 2);
    assert!(!result.truncated);
}

#[test]
fn graph_query_honors_edge_filters() {
    let mut graph = SymbolGraph::default();
    graph.upsert_node(SymbolNode {
        id: "file:1".to_string(),
        path: "src/a.ts".to_string(),
        name: "a.ts".to_string(),
        kind: SymbolKind::File,
        line: 1,
    });
    graph.upsert_node(SymbolNode {
        id: "module:net/http".to_string(),
        path: "src/a.ts".to_string(),
        name: "net/http".to_string(),
        kind: SymbolKind::Module,
        line: 1,
    });
    graph.upsert_node(SymbolNode {
        id: "call:fetch".to_string(),
        path: "src/a.ts".to_string(),
        name: "fetch".to_string(),
        kind: SymbolKind::Function,
        line: 2,
    });

    graph.add_edge(SymbolEdge {
        from: "file:1".to_string(),
        to: "module:net/http".to_string(),
        kind: EdgeKind::Imports,
    });
    graph.add_edge(SymbolEdge {
        from: "file:1".to_string(),
        to: "call:fetch".to_string(),
        kind: EdgeKind::Calls,
    });

    let result = graph.query(&GraphQueryRequest {
        mode: GraphQueryMode::NodeId,
        value: "file:1".to_string(),
        depth: 1,
        limit: 20,
        edge_kinds: Some(vec![EdgeKind::Imports]),
    });

    assert_eq!(result.nodes.len(), 2);
    assert_eq!(result.edges.len(), 1);
    assert_eq!(result.edges[0].kind, EdgeKind::Imports);
}
