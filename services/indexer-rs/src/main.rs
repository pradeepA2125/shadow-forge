use anyhow::{anyhow, Result};
use ai_editor_indexer::config::IndexerConfig;
use ai_editor_indexer::graph::{EdgeKind, GraphQueryMode, GraphQueryRequest, SymbolGraph};
use ai_editor_indexer::service::{IndexerService, IndexSnapshot};
use std::path::PathBuf;

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .init();

    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("index") => run_index(&args[1..]).await,
        Some("query") => run_query(&args[1..]),
        _ => run_default().await,
    }
}

async fn run_default() -> Result<()> {
    let config = IndexerConfig::from_env();
    let mut service = IndexerService::new(config)?;
    service.bootstrap().await?;
    service.run().await
}

async fn run_index(args: &[String]) -> Result<()> {
    let mut config = IndexerConfig::from_env();
    config.watch_enabled = false;
    if let Some(workspace) = find_option(args, "--workspace") {
        config.workspace_root = PathBuf::from(workspace);
    }
    if let Some(snapshot_path) = find_option(args, "--snapshot-path") {
        config.snapshot_output_path = PathBuf::from(snapshot_path);
    }
    if let Some(watch_raw) = find_option(args, "--watch") {
        config.watch_enabled = parse_bool(watch_raw)?;
    }

    let mut service = IndexerService::new(config)?;
    service.bootstrap().await?;
    service.run().await
}

fn run_query(args: &[String]) -> Result<()> {
    let snapshot_path = find_option(args, "--snapshot-path")
        .ok_or_else(|| anyhow!("--snapshot-path is required for query command"))?;
    let mode_raw =
        find_option(args, "--mode").ok_or_else(|| anyhow!("--mode is required for query command"))?;
    let value =
        find_option(args, "--value").ok_or_else(|| anyhow!("--value is required for query command"))?;

    let depth = find_option(args, "--depth")
        .and_then(|raw| raw.parse::<usize>().ok())
        .unwrap_or(2);
    let limit = find_option(args, "--limit")
        .and_then(|raw| raw.parse::<usize>().ok())
        .unwrap_or(200);

    let edge_kinds = find_option(args, "--edge-kinds").map(parse_edge_kinds).transpose()?;
    let mode = parse_query_mode(mode_raw)?;

    let payload = std::fs::read_to_string(snapshot_path)?;
    let snapshot: IndexSnapshot = serde_json::from_str(&payload)?;
    let graph = SymbolGraph::from_snapshot(snapshot.graph.nodes, snapshot.graph.edges);
    let request = GraphQueryRequest {
        mode,
        value: value.to_string(),
        depth,
        limit,
        edge_kinds,
    };
    let response = graph.query(&request);
    println!("{}", serde_json::to_string_pretty(&response)?);
    Ok(())
}

fn find_option<'a>(args: &'a [String], name: &str) -> Option<&'a str> {
    args.windows(2)
        .find(|window| window.first().is_some_and(|item| item == name))
        .and_then(|window| window.get(1))
        .map(String::as_str)
}

fn parse_bool(raw: &str) -> Result<bool> {
    match raw {
        "1" | "true" | "TRUE" | "True" => Ok(true),
        "0" | "false" | "FALSE" | "False" => Ok(false),
        _ => Err(anyhow!("invalid boolean value: {raw}")),
    }
}

fn parse_query_mode(raw: &str) -> Result<GraphQueryMode> {
    match raw {
        "symbol_name" => Ok(GraphQueryMode::SymbolName),
        "file_path" => Ok(GraphQueryMode::FilePath),
        "node_id" => Ok(GraphQueryMode::NodeId),
        _ => Err(anyhow!(
            "invalid query mode '{raw}', expected symbol_name|file_path|node_id"
        )),
    }
}

fn parse_edge_kinds(raw: &str) -> Result<Vec<EdgeKind>> {
    let mut kinds: Vec<EdgeKind> = Vec::new();
    for part in raw.split(',').map(|item| item.trim()).filter(|item| !item.is_empty()) {
        let kind = match part {
            "calls" => EdgeKind::Calls,
            "imports" => EdgeKind::Imports,
            "inherits" => EdgeKind::Inherits,
            "references" => EdgeKind::References,
            "implements" => EdgeKind::Implements,
            _ => return Err(anyhow!("invalid edge kind '{part}'")),
        };
        kinds.push(kind);
    }

    kinds.sort_by(|a, b| format!("{a:?}").cmp(&format!("{b:?}")));
    kinds.dedup();
    Ok(kinds)
}
