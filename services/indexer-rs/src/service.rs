use anyhow::Result;
use notify::{Event, EventKind, RecursiveMode, Watcher};
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::mpsc;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use crate::config::IndexerConfig;
use crate::graph::{SymbolEdge, SymbolGraph, SymbolNode};
use crate::lsp::{LspAdapter, LspDiagnostic};
use crate::parser::{LanguageParser, TreeSitterParser};

const WATCH_DEBOUNCE_MS: u128 = 150;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GraphSnapshot {
    pub nodes: Vec<SymbolNode>,
    pub edges: Vec<SymbolEdge>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SnapshotStats {
    pub node_count: usize,
    pub edge_count: usize,
    pub diagnostic_count: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IndexSnapshot {
    pub schema_version: u32,
    pub workspace_root: String,
    pub generated_at_ms: u128,
    pub graph: GraphSnapshot,
    pub diagnostics: Vec<LspDiagnostic>,
    pub stats: SnapshotStats,
}

pub struct IndexerService {
    config: IndexerConfig,
    graph: SymbolGraph,
    parser: Box<dyn LanguageParser>,
    lsp: LspAdapter,
    tracked_files: HashSet<PathBuf>,
    diagnostics_snapshot: Vec<LspDiagnostic>,
    index_warnings: Vec<LspDiagnostic>,
    last_watch_event: HashMap<PathBuf, Instant>,
}

impl IndexerService {
    pub fn new(config: IndexerConfig) -> Result<Self> {
        let lsp = LspAdapter::new(&config);
        Ok(Self {
            config,
            graph: SymbolGraph::default(),
            parser: Box::new(TreeSitterParser),
            lsp,
            tracked_files: HashSet::new(),
            diagnostics_snapshot: Vec::new(),
            index_warnings: Vec::new(),
            last_watch_event: HashMap::new(),
        })
    }

    pub async fn bootstrap(&mut self) -> Result<()> {
        self.tracked_files.clear();
        self.index_warnings.clear();

        let root = self.config.workspace_root.clone();
        self.index_path(&root).await?;

        if self.config.lsp_enabled {
            let files: Vec<PathBuf> = self.tracked_files.iter().cloned().collect();
            self.lsp.open_workspace_files(&files).await?;
            self.refresh_diagnostics().await?;
        }

        self.persist_snapshot().await?;
        Ok(())
    }

    pub async fn run(&mut self) -> Result<()> {
        if self.config.watch_enabled {
            tracing::info!(
                workspace = %self.config.workspace_root.display(),
                workers = self.config.max_parse_workers,
                parser = self.parser.language_name(),
                lsp_enabled = self.config.lsp_enabled,
                "indexer watch loop started"
            );
            self.watch_loop().await?;
        }

        self.refresh_diagnostics().await?;
        self.persist_snapshot().await?;

        tracing::info!(
            node_count = self.graph.node_count(),
            edge_count = self.graph.edge_count(),
            diagnostic_count = self.diagnostics_snapshot.len(),
            "index graph snapshot"
        );

        Ok(())
    }

    pub fn get_diagnostics_snapshot(&self) -> Vec<LspDiagnostic> {
        let mut diagnostics = self.diagnostics_snapshot.clone();
        diagnostics.extend(self.index_warnings.clone());
        diagnostics.sort_by(|a, b| {
            a.file
                .cmp(&b.file)
                .then(a.line.cmp(&b.line))
                .then(a.column.cmp(&b.column))
                .then(a.message.cmp(&b.message))
        });
        diagnostics
    }

    pub fn get_graph_snapshot(&self) -> GraphSnapshot {
        GraphSnapshot {
            nodes: self.graph.all_nodes(),
            edges: self.graph.all_edges(),
        }
    }

    pub fn get_index_snapshot(&self) -> IndexSnapshot {
        let graph = self.get_graph_snapshot();
        let diagnostics = self.get_diagnostics_snapshot();
        IndexSnapshot {
            schema_version: 1,
            workspace_root: self.config.workspace_root.display().to_string(),
            generated_at_ms: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|value| value.as_millis())
                .unwrap_or(0),
            graph: graph.clone(),
            diagnostics: diagnostics.clone(),
            stats: SnapshotStats {
                node_count: graph.nodes.len(),
                edge_count: graph.edges.len(),
                diagnostic_count: diagnostics.len(),
            },
        }
    }

    async fn watch_loop(&mut self) -> Result<()> {
        let (tx, rx) = mpsc::channel::<notify::Result<Event>>();
        let mut watcher =
            notify::recommended_watcher(move |res| if tx.send(res).is_err() {})?;
        watcher.watch(&self.config.workspace_root, RecursiveMode::Recursive)?;

        loop {
            let event_result = match rx.recv() {
                Ok(result) => result,
                Err(_) => break,
            };

            match event_result {
                Ok(event) => self.handle_watch_event(event).await?,
                Err(error) => {
                    tracing::warn!(error = %error, "filesystem watch event error");
                }
            }
        }

        Ok(())
    }

    async fn handle_watch_event(&mut self, event: Event) -> Result<()> {
        for path in event.paths {
            if !is_supported_source_path(&path) || is_ignored_path(&path) {
                continue;
            }

            let now = Instant::now();
            if let Some(previous) = self.last_watch_event.get(&path) {
                if now.duration_since(*previous).as_millis() < WATCH_DEBOUNCE_MS {
                    continue;
                }
            }
            self.last_watch_event.insert(path.clone(), now);

            match event.kind {
                EventKind::Remove(_) => {
                    self.tracked_files.remove(&path);
                    self.lsp.close_file(&path).await?;
                }
                EventKind::Modify(_) | EventKind::Create(_) | EventKind::Any => {
                    if !path.exists() || !path.is_file() {
                        continue;
                    }

                    match std::fs::read_to_string(&path) {
                        Ok(source) => {
                            if let Err(error) = self.parser.parse_file(&path, &source, &mut self.graph) {
                                self.push_index_warning(
                                    &path,
                                    format!("parser failed for changed file: {error}"),
                                );
                                continue;
                            }
                            self.tracked_files.insert(path.clone());
                            self.lsp.upsert_file(&path, source).await?;
                        }
                        Err(error) => {
                            self.push_index_warning(
                                &path,
                                format!("unable to process changed file: {error}"),
                            );
                            tracing::warn!(
                                path = %path.display(),
                                error = %error,
                                "unable to process changed file"
                            );
                        }
                    }
                }
                _ => {}
            }
        }

        self.refresh_diagnostics().await?;
        self.persist_snapshot().await?;
        Ok(())
    }

    async fn refresh_diagnostics(&mut self) -> Result<()> {
        self.diagnostics_snapshot = self
            .lsp
            .collect_diagnostics(&self.config.workspace_root)
            .await?;
        Ok(())
    }

    async fn persist_snapshot(&self) -> Result<()> {
        let snapshot = self.get_index_snapshot();
        if let Some(parent) = self.config.snapshot_output_path.parent() {
            tokio::fs::create_dir_all(parent).await?;
        }
        let payload = serde_json::to_vec_pretty(&snapshot)?;
        tokio::fs::write(&self.config.snapshot_output_path, payload).await?;
        Ok(())
    }

    async fn index_path(&mut self, path: &Path) -> Result<()> {
        self.index_path_inner(path)
    }

    fn index_path_inner(&mut self, path: &Path) -> Result<()> {
        if is_ignored_path(path) {
            return Ok(());
        }

        if path.is_file() {
            return self.index_file(path);
        }

        for entry in std::fs::read_dir(path)? {
            let entry = entry?;
            let child = entry.path();
            if child.is_dir() {
                self.index_path_inner(&child)?;
                continue;
            }
            self.index_file(&child)?;
        }

        Ok(())
    }

    fn index_file(&mut self, file_path: &Path) -> Result<()> {
        if !is_supported_source_path(file_path) {
            return Ok(());
        }

        match std::fs::read_to_string(file_path) {
            Ok(source) => {
                if let Err(error) = self.parser.parse_file(file_path, &source, &mut self.graph) {
                    self.push_index_warning(file_path, format!("parser failed while indexing: {error}"));
                    return Ok(());
                }
                self.tracked_files.insert(file_path.to_path_buf());
            }
            Err(error) => {
                self.push_index_warning(
                    file_path,
                    format!("unable to index source file (likely non-UTF8): {error}"),
                );
                tracing::warn!(
                    path = %file_path.display(),
                    error = %error,
                    "unable to index source file"
                );
            }
        }
        Ok(())
    }

    fn push_index_warning(&mut self, file_path: &Path, message: String) {
        let warning = LspDiagnostic {
            severity: "warning".to_string(),
            source: Some("indexer".to_string()),
            code: None,
            message,
            file: file_path.display().to_string(),
            line: 1,
            column: 1,
            language: language_for_path(file_path).to_string(),
        };

        if self
            .index_warnings
            .iter()
            .any(|existing| existing.file == warning.file && existing.message == warning.message)
        {
            return;
        }
        self.index_warnings.push(warning);
    }
}

fn is_supported_source_path(path: &Path) -> bool {
    matches!(
        path.extension().and_then(|ext| ext.to_str()),
        Some("ts" | "tsx" | "py" | "rs")
    )
}

fn is_ignored_path(path: &Path) -> bool {
    const IGNORED_DIRS: [&str; 7] = [
        ".git",
        "node_modules",
        ".venv",
        "target",
        "dist",
        "__pycache__",
        ".pytest_cache",
    ];
    path.components().any(|component| {
        let name = component.as_os_str().to_string_lossy();
        IGNORED_DIRS.iter().any(|candidate| *candidate == name)
    })
}

fn language_for_path(path: &Path) -> &'static str {
    match path.extension().and_then(|ext| ext.to_str()) {
        Some("ts" | "tsx") => "typescript",
        Some("py") => "python",
        Some("rs") => "rust",
        _ => "unknown",
    }
}
