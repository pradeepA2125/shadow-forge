//! Bridge the parser's call-site placeholders to LSP-resolved workspace
//! symbols. Runs once at bootstrap (after `LspAdapter::open_workspace_files`)
//! and again per modified file inside the watch loop, after the LSP has been
//! told about the latest content.
//!
//! For each `PlaceholderEdge`:
//! 1. Ask `textDocument/definition` for the resolved declaration. When the
//!    location resolves to a workspace symbol, drop the `external:call:<name>`
//!    fallback edge and replace it with a `Calls` edge to that symbol.
//! 2. Ask `textDocument/implementation` for the dispatch space. For every
//!    impl that resolves to a workspace symbol, emit an `Implements` edge
//!    from the impl back to the declaration. This is what gives the planner
//!    visibility into Protocol/ABC/interface dispatch (engine.run_task calls
//!    TaskStore.save, which has SQLiteTaskStore.save and InMemoryTaskStore.save
//!    as its Implements neighbours).
//!
//! Failure modes are graceful: an unresolved definition leaves the placeholder's
//! external edge in place (same behaviour as today); an empty implementations
//! list emits just the Calls edge; an LSP error is logged and the placeholder
//! is dropped. Never panics. Never blocks the index from being written.

use std::path::Path;

use crate::graph::{EdgeKind, PlaceholderEdge, SymbolEdge, SymbolGraph};
use crate::lsp::{LspAdapter, LspLocation};

/// Thin oracle the resolver consults to turn a call site's `(file, line,
/// character)` into a resolved declaration plus its implementation fan-out.
/// `LspAdapter` is the production implementor; tests substitute a determ-
/// inistic fake so we don't have to spawn pyright/tsserver in the test suite.
pub trait CallSiteOracle {
    fn resolve_definition(
        &mut self,
        file_path: &Path,
        line: u32,
        character: u32,
    ) -> Option<LspLocation>;

    fn resolve_implementations(
        &mut self,
        file_path: &Path,
        line: u32,
        character: u32,
    ) -> Vec<LspLocation>;
}

impl CallSiteOracle for LspAdapter {
    fn resolve_definition(
        &mut self,
        file_path: &Path,
        line: u32,
        character: u32,
    ) -> Option<LspLocation> {
        self.resolve_definition(file_path, line, character)
    }

    fn resolve_implementations(
        &mut self,
        file_path: &Path,
        line: u32,
        character: u32,
    ) -> Vec<LspLocation> {
        self.resolve_implementations(file_path, line, character)
    }
}

/// Drain every placeholder on `graph` and resolve each via `lsp`.
pub fn resolve_placeholders(graph: &mut SymbolGraph, lsp: &mut LspAdapter) {
    let placeholders = graph.take_placeholders();
    resolve_inner(graph, lsp, placeholders);
}

/// Drain only placeholders whose call site is in `file_path` and resolve
/// each. Called from the watch loop on every modify event.
pub fn resolve_placeholders_for_file(
    graph: &mut SymbolGraph,
    lsp: &mut LspAdapter,
    file_path: &Path,
) {
    let placeholders = graph.take_placeholders_for_file(file_path);
    resolve_inner(graph, lsp, placeholders);
}

/// Generic implementation parameterised on the oracle. Production paths use
/// `LspAdapter`; tests use a stub.
pub fn resolve_inner<O: CallSiteOracle>(
    graph: &mut SymbolGraph,
    oracle: &mut O,
    placeholders: Vec<PlaceholderEdge>,
) {
    let total = placeholders.len();
    let mut resolved_count = 0usize;
    let mut external_target = 0usize;
    let mut none_returned = 0usize;
    let mut impls_emitted = 0usize;
    for placeholder in placeholders {
        let Some(definition_loc) = oracle.resolve_definition(
            &placeholder.file_path,
            placeholder.line,
            placeholder.character,
        ) else {
            none_returned += 1;
            // LSP couldn't resolve (stdlib, dynamic dispatch, server timeout
            // — all the same outcome from our point of view). The
            // external:call:<name> edge the parser already emitted stays in
            // place; nothing to do.
            continue;
        };

        // Map the definition location back to a workspace node. When the
        // resolver picks a node, we treat the call as workspace-resolved and
        // rewrite the edge. When no workspace node matches (the LSP pointed
        // at a stdlib or site-packages file), leave the external edge alone.
        let Some(declaration_id) = nearest_node_at(graph, &definition_loc) else {
            external_target += 1;
            continue;
        };

        // Drop the placeholder's external:call edge and replace with a Calls
        // edge to the declaration.
        let external_edge = SymbolEdge {
            from: placeholder.from_id.clone(),
            to: placeholder.external_to_id.clone(),
            kind: placeholder.edge_kind.clone(),
        };
        graph.remove_edge(&external_edge);
        graph.add_edge(SymbolEdge {
            from: placeholder.from_id.clone(),
            to: declaration_id.clone(),
            kind: placeholder.edge_kind.clone(),
        });
        resolved_count += 1;

        // Fan out implementations. LSP often includes the declaration in the
        // implementations response — filter it out so we don't emit
        // self-loops. Skip impls that don't resolve to a workspace node, same
        // policy as definition.
        let implementations = oracle.resolve_implementations(
            &placeholder.file_path,
            placeholder.line,
            placeholder.character,
        );
        for impl_loc in implementations {
            if locations_equal(&impl_loc, &definition_loc) {
                continue;
            }
            let Some(impl_id) = nearest_node_at(graph, &impl_loc) else {
                continue;
            };
            if impl_id == declaration_id {
                continue;
            }
            graph.add_edge(SymbolEdge {
                from: impl_id,
                to: declaration_id.clone(),
                kind: EdgeKind::Implements,
            });
            impls_emitted += 1;
        }
    }
    tracing::info!(
        total_placeholders = total,
        resolved = resolved_count,
        external_kept = none_returned + external_target,
        unresolved_none = none_returned,
        external_target = external_target,
        implements = impls_emitted,
        "resolver pass complete"
    );
}

/// Find the node in the graph that most likely corresponds to the symbol at
/// `loc`. Workspace nodes carry 1-indexed line numbers (parser convention);
/// LSP locations are 0-indexed. We add 1 to the LSP line and pick the node
/// in the same file whose `line` is the greatest value ≤ that target. This
/// is the standard "enclosing declaration" heuristic — the symbol whose
/// declaration appears at or just above the LSP-pointed line.
fn nearest_node_at(graph: &SymbolGraph, loc: &LspLocation) -> Option<String> {
    let path_str = loc.path.to_string_lossy().to_string();
    let target_line = loc.line + 1;

    let nodes = graph.nodes_in_file(&path_str);
    if nodes.is_empty() {
        return None;
    }

    let mut best: Option<(u32, String)> = None;
    for node in nodes {
        if node.line == 0 || node.line > target_line {
            continue;
        }
        let distance = target_line - node.line;
        if best.as_ref().is_none_or(|(d, _)| distance < *d) {
            best = Some((distance, node.id.clone()));
        }
    }
    best.map(|(_, id)| id)
}

fn locations_equal(a: &LspLocation, b: &LspLocation) -> bool {
    a.path == b.path && a.line == b.line && a.character == b.character
}
