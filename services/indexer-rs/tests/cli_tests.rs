use ai_editor_indexer::graph::GraphQueryResponse;
use ai_editor_indexer::service::IndexSnapshot;
use serde_json::json;
use std::path::PathBuf;
use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

fn temp_workspace(prefix: &str) -> PathBuf {
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| value.as_nanos())
        .unwrap_or(0);
    let path = std::env::temp_dir().join(format!(
        "ai-editor-indexer-{prefix}-{}-{unique}",
        std::process::id()
    ));
    std::fs::create_dir_all(&path).expect("create temp workspace");
    path
}

#[test]
fn index_command_writes_full_snapshot_artifact() {
    let workspace = temp_workspace("index");
    let source_dir = workspace.join("src");
    std::fs::create_dir_all(&source_dir).expect("create source dir");
    std::fs::write(
        source_dir.join("main.ts"),
        "import { readFile } from 'node:fs';\nfunction build() { readFile('x'); }\n",
    )
    .expect("write source file");

    let snapshot_path = workspace.join(".ai-editor/index-snapshot.json");
    let status = Command::new(env!("CARGO_BIN_EXE_ai-editor-indexer"))
        .arg("index")
        .arg("--workspace")
        .arg(&workspace)
        .arg("--snapshot-path")
        .arg(&snapshot_path)
        .arg("--watch")
        .arg("0")
        .env("AI_EDITOR_LSP_ENABLED", "0")
        .status()
        .expect("execute index command");

    assert!(status.success(), "index command should exit successfully");
    assert!(snapshot_path.exists(), "index command should produce snapshot");

    let payload = std::fs::read_to_string(&snapshot_path).expect("read snapshot");
    let snapshot: IndexSnapshot = serde_json::from_str(&payload).expect("decode snapshot");

    assert_eq!(snapshot.schema_version, 1);
    assert_eq!(snapshot.stats.node_count, snapshot.graph.nodes.len());
    assert_eq!(snapshot.stats.edge_count, snapshot.graph.edges.len());
    assert!(!snapshot.graph.nodes.is_empty(), "expected indexed nodes");
    assert!(
        snapshot.workspace_root.ends_with(workspace.to_string_lossy().as_ref()),
        "workspace root should match command input"
    );

    let _ = std::fs::remove_dir_all(&workspace);
}

#[test]
fn query_command_returns_deterministic_graph_payload() {
    let workspace = temp_workspace("query");
    let snapshot_path = workspace.join(".ai-editor/index-snapshot.json");
    if let Some(parent) = snapshot_path.parent() {
        std::fs::create_dir_all(parent).expect("create snapshot dir");
    }

    let snapshot = json!({
        "schema_version": 1,
        "workspace_root": workspace.to_string_lossy(),
        "generated_at_ms": 123_u64,
        "graph": {
            "nodes": [
                {"id":"file:1","path":"src/a.ts","name":"a.ts","kind":"File","line":1},
                {"id":"function:file:1:build","path":"src/a.ts","name":"build","kind":"Function","line":2},
                {"id":"call:fetch","path":"src/a.ts","name":"fetch","kind":"Function","line":3}
            ],
            "edges": [
                {"from":"file:1","to":"function:file:1:build","kind":"References"},
                {"from":"function:file:1:build","to":"call:fetch","kind":"Calls"}
            ]
        },
        "diagnostics": [],
        "stats": {"node_count":3,"edge_count":2,"diagnostic_count":0}
    });
    std::fs::write(
        &snapshot_path,
        serde_json::to_vec_pretty(&snapshot).expect("encode snapshot"),
    )
    .expect("write snapshot");

    let output = Command::new(env!("CARGO_BIN_EXE_ai-editor-indexer"))
        .arg("query")
        .arg("--snapshot-path")
        .arg(&snapshot_path)
        .arg("--mode")
        .arg("node_id")
        .arg("--value")
        .arg("file:1")
        .arg("--depth")
        .arg("2")
        .arg("--limit")
        .arg("200")
        .output()
        .expect("execute query command");

    assert!(
        output.status.success(),
        "query command should exit successfully"
    );
    let response: GraphQueryResponse =
        serde_json::from_slice(&output.stdout).expect("decode query response");

    assert_eq!(response.roots, vec!["file:1".to_string()]);
    assert_eq!(
        response
            .nodes
            .iter()
            .map(|node| node.id.clone())
            .collect::<Vec<String>>(),
        vec![
            "call:fetch".to_string(),
            "file:1".to_string(),
            "function:file:1:build".to_string()
        ]
    );
    assert_eq!(response.edges.len(), 2);
    assert!(!response.truncated);

    let _ = std::fs::remove_dir_all(&workspace);
}
