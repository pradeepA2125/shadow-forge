use ai_editor_indexer::graph::{EdgeKind, SymbolEdge, SymbolKind, SymbolNode};
use ai_editor_indexer::lsp::LspDiagnostic;
use ai_editor_indexer::service::{GraphSnapshot, IndexSnapshot, SnapshotStats};

#[test]
fn snapshot_roundtrip_preserves_full_graph_payload() {
    let snapshot = IndexSnapshot {
        schema_version: 1,
        workspace_root: "/tmp/ws".to_string(),
        generated_at_ms: 123,
        graph: GraphSnapshot {
            nodes: vec![SymbolNode {
                id: "file:src/main.rs".to_string(),
                path: "src/main.rs".to_string(),
                name: "main.rs".to_string(),
                kind: SymbolKind::File,
                line: 1,
            }],
            edges: vec![SymbolEdge {
                from: "file:src/main.rs".to_string(),
                to: "call:main".to_string(),
                kind: EdgeKind::References,
            }],
        },
        diagnostics: vec![LspDiagnostic {
            severity: "warning".to_string(),
            source: Some("lsp".to_string()),
            code: Some("W1".to_string()),
            message: "warn".to_string(),
            file: "src/main.rs".to_string(),
            line: 1,
            column: 1,
            language: "rust".to_string(),
        }],
        stats: SnapshotStats {
            node_count: 1,
            edge_count: 1,
            diagnostic_count: 1,
        },
    };

    let payload = serde_json::to_string(&snapshot).expect("serialize");
    let parsed: IndexSnapshot = serde_json::from_str(&payload).expect("deserialize");
    assert_eq!(parsed.schema_version, 1);
    assert_eq!(parsed.graph.nodes.len(), 1);
    assert_eq!(parsed.graph.edges.len(), 1);
    assert_eq!(parsed.diagnostics.len(), 1);
    assert_eq!(parsed.stats.node_count, 1);
}
