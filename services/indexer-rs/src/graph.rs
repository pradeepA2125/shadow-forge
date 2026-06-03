use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet, VecDeque};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Hash)]
pub enum SymbolKind {
    File,
    Module,
    Function,
    Class,
    Interface,
    Method,
    Variable,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SymbolNode {
    pub id: String,
    pub path: String,
    pub name: String,
    pub kind: SymbolKind,
    pub line: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Hash)]
pub enum EdgeKind {
    Calls,
    Imports,
    Inherits,
    References,
    /// Implementation of a declaration (Protocol method, ABC method, interface
    /// method, abstract base method). Emitted by the resolver stage after
    /// querying `textDocument/implementation` — points from the concrete impl
    /// to the declaration the call site dispatches through. Distinct from
    /// `Inherits` so consumers can distinguish class-level inheritance from
    /// method-level dispatch overrides.
    Implements,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SymbolEdge {
    pub from: String,
    pub to: String,
    pub kind: EdgeKind,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum GraphQueryMode {
    SymbolName,
    FilePath,
    NodeId,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct GraphQueryRequest {
    pub mode: GraphQueryMode,
    pub value: String,
    pub depth: usize,
    pub limit: usize,
    pub edge_kinds: Option<Vec<EdgeKind>>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct GraphQueryStats {
    pub root_count: usize,
    pub node_count: usize,
    pub edge_count: usize,
    pub requested_depth: usize,
    pub requested_limit: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct GraphQueryResponse {
    pub roots: Vec<String>,
    pub nodes: Vec<SymbolNode>,
    pub edges: Vec<SymbolEdge>,
    pub truncated: bool,
    pub stats: GraphQueryStats,
}

/// One unresolved `Calls`-style edge waiting on the LSP resolver. The parser
/// emits a `PlaceholderEdge` at each call site it can't statically resolve to a
/// workspace symbol, recording enough information for the resolver stage to
/// either (a) ask the LSP for the definition + implementations and rewrite the
/// edge in place, or (b) leave the placeholder's already-emitted
/// `external:call:<name>` edge in place when the LSP can't resolve it either.
#[derive(Debug, Clone)]
pub struct PlaceholderEdge {
    /// Source node id (the enclosing function/method/file that issued the call).
    pub from_id: String,
    /// Pre-existing `external:call:<name>` node id the parser emitted as a
    /// fallback target. The resolver replaces this with the resolved workspace
    /// node id when possible, or leaves it intact when the LSP can't help.
    pub external_to_id: String,
    /// Workspace-absolute path of the file containing the call site.
    pub file_path: std::path::PathBuf,
    /// 0-indexed line of the callable in the source (LSP position semantics).
    pub line: u32,
    /// 0-indexed character (UTF-16 code unit) of the callable in the source.
    pub character: u32,
    /// What kind of edge to emit on successful resolution. Today only `Calls`,
    /// but reserved so future passes can route References through the same
    /// machinery.
    pub edge_kind: EdgeKind,
}

#[derive(Debug, Default, Clone)]
pub struct SymbolGraph {
    nodes: HashMap<String, SymbolNode>,
    edges: HashSet<(String, String, EdgeKind)>,
    adjacency: HashMap<String, HashSet<(String, EdgeKind)>>,
    reverse_adjacency: HashMap<String, HashSet<(String, EdgeKind)>>,
    symbol_name_index: HashMap<String, HashSet<String>>,
    file_path_index: HashMap<String, HashSet<String>>,
    /// Call sites parked for the resolver stage. Not serialised into the
    /// snapshot — purely an indexer-internal queue between parse and resolve.
    placeholder_edges: Vec<PlaceholderEdge>,
}

impl SymbolGraph {
    pub fn from_snapshot(nodes: Vec<SymbolNode>, edges: Vec<SymbolEdge>) -> Self {
        let mut graph = Self::default();
        for node in nodes {
            graph.upsert_node(node);
        }
        for edge in edges {
            graph.add_edge(edge);
        }
        graph
    }

    pub fn upsert_node(&mut self, node: SymbolNode) {
        if let Some(previous) = self.nodes.insert(node.id.clone(), node.clone()) {
            let previous_name = previous.name.to_lowercase();
            remove_index_entry(&mut self.symbol_name_index, &previous_name, &previous.id);
            remove_index_entry(&mut self.file_path_index, &previous.path, &previous.id);
        }

        self.symbol_name_index
            .entry(node.name.to_lowercase())
            .or_default()
            .insert(node.id.clone());
        self.file_path_index
            .entry(node.path.clone())
            .or_default()
            .insert(node.id.clone());
    }

    pub fn add_edge(&mut self, edge: SymbolEdge) {
        if self
            .edges
            .insert((edge.from.clone(), edge.to.clone(), edge.kind.clone()))
        {
            self.adjacency
                .entry(edge.from.clone())
                .or_default()
                .insert((edge.to.clone(), edge.kind.clone()));
            self.reverse_adjacency
                .entry(edge.to)
                .or_default()
                .insert((edge.from, edge.kind));
        }
    }

    pub fn node_count(&self) -> usize {
        self.nodes.len()
    }

    pub fn edge_count(&self) -> usize {
        self.edges.len()
    }

    /// Borrow every node stored in `file_path`. Used by the resolver to map
    /// an LSP `Location` back to a workspace symbol node by picking the
    /// nearest enclosing-line node. Uses the same `file_path_index` already
    /// maintained by `upsert_node`, so the lookup is O(node-count-in-file).
    pub fn nodes_in_file(&self, file_path: &str) -> Vec<&SymbolNode> {
        let Some(ids) = self.file_path_index.get(file_path) else {
            return Vec::new();
        };
        ids.iter().filter_map(|id| self.nodes.get(id)).collect()
    }

    /// Park a placeholder for the resolver. Parsers call this at every call
    /// site they couldn't statically resolve to a workspace target. The edge
    /// itself (pointing at the `external_to_id` fallback) must be added via
    /// `add_edge` separately — `push_placeholder` only records resolution intent.
    pub fn push_placeholder(&mut self, placeholder: PlaceholderEdge) {
        self.placeholder_edges.push(placeholder);
    }

    /// Drain every placeholder. The resolver stage owns the returned vec and
    /// rewrites edges in-place via `add_edge` / `remove_edge` as it works
    /// through the LSP responses.
    pub fn take_placeholders(&mut self) -> Vec<PlaceholderEdge> {
        std::mem::take(&mut self.placeholder_edges)
    }

    /// Drain only placeholders whose call site is in `file_path`. Used by the
    /// watch loop, which re-parses a single file and shouldn't disturb other
    /// files' pending resolutions.
    pub fn take_placeholders_for_file(
        &mut self,
        file_path: &std::path::Path,
    ) -> Vec<PlaceholderEdge> {
        let mut keep: Vec<PlaceholderEdge> = Vec::new();
        let mut drained: Vec<PlaceholderEdge> = Vec::new();
        for placeholder in std::mem::take(&mut self.placeholder_edges) {
            if placeholder.file_path == file_path {
                drained.push(placeholder);
            } else {
                keep.push(placeholder);
            }
        }
        self.placeholder_edges = keep;
        drained
    }

    /// Remove a previously-added edge. The resolver uses this to drop a
    /// placeholder's `external:call:<name>` fallback once the LSP returns a
    /// resolved workspace target. Returns true when an edge was actually
    /// removed (i.e. it existed in the graph).
    pub fn remove_edge(&mut self, edge: &SymbolEdge) -> bool {
        let key = (edge.from.clone(), edge.to.clone(), edge.kind.clone());
        if !self.edges.remove(&key) {
            return false;
        }
        if let Some(set) = self.adjacency.get_mut(&edge.from) {
            set.remove(&(edge.to.clone(), edge.kind.clone()));
            if set.is_empty() {
                self.adjacency.remove(&edge.from);
            }
        }
        if let Some(set) = self.reverse_adjacency.get_mut(&edge.to) {
            set.remove(&(edge.from.clone(), edge.kind.clone()));
            if set.is_empty() {
                self.reverse_adjacency.remove(&edge.to);
            }
        }
        true
    }

    /// Drop every `Calls` and `Implements` edge whose source node lives in the
    /// given file. The watch loop calls this before re-parsing a file so the
    /// next resolver pass starts from a clean slate. Pure pruning — no
    /// node deletion, since nodes may still be referenced from other files.
    pub fn prune_resolved_calls_from_file(&mut self, file_path: &std::path::Path) {
        let target = file_path.to_string_lossy().to_string();
        let victims: Vec<SymbolEdge> = self
            .all_edges()
            .into_iter()
            .filter(|edge| {
                matches!(edge.kind, EdgeKind::Calls | EdgeKind::Implements)
                    && self
                        .nodes
                        .get(&edge.from)
                        .is_some_and(|node| node.path == target)
            })
            .collect();
        for edge in victims {
            self.remove_edge(&edge);
        }
    }

    pub fn all_nodes(&self) -> Vec<SymbolNode> {
        let mut nodes: Vec<SymbolNode> = self.nodes.values().cloned().collect();
        nodes.sort_by(|a, b| a.id.cmp(&b.id));
        nodes
    }

    pub fn all_edges(&self) -> Vec<SymbolEdge> {
        let mut edges: Vec<SymbolEdge> = self
            .edges
            .iter()
            .map(|(from, to, kind)| SymbolEdge {
                from: from.clone(),
                to: to.clone(),
                kind: kind.clone(),
            })
            .collect();
        edges.sort_by(|a, b| {
            a.from
                .cmp(&b.from)
                .then(a.to.cmp(&b.to))
                .then(format!("{:?}", a.kind).cmp(&format!("{:?}", b.kind)))
        });
        edges
    }

    pub fn query(&self, request: &GraphQueryRequest) -> GraphQueryResponse {
        let roots = self.resolve_roots(&request.mode, &request.value);
        if roots.is_empty() {
            return GraphQueryResponse {
                roots,
                nodes: Vec::new(),
                edges: Vec::new(),
                truncated: false,
                stats: GraphQueryStats {
                    root_count: 0,
                    node_count: 0,
                    edge_count: 0,
                    requested_depth: request.depth,
                    requested_limit: request.limit,
                },
            };
        }

        let allowed_kinds: Option<HashSet<EdgeKind>> = request
            .edge_kinds
            .as_ref()
            .map(|kinds| kinds.iter().cloned().collect());

        let mut visited: HashSet<String> = HashSet::new();
        let mut queue: VecDeque<(String, usize)> = roots
            .iter()
            .cloned()
            .map(|node_id| (node_id, 0))
            .collect();
        let mut truncated = false;

        while let Some((node_id, depth)) = queue.pop_front() {
            if visited.contains(&node_id) {
                continue;
            }
            if visited.len() == request.limit {
                truncated = true;
                break;
            }
            visited.insert(node_id.clone());

            if depth >= request.depth {
                continue;
            }

            for neighbor in self.neighbors(&node_id, allowed_kinds.as_ref()) {
                if !visited.contains(&neighbor) {
                    queue.push_back((neighbor, depth + 1));
                }
            }
        }

        let mut nodes: Vec<SymbolNode> = visited
            .iter()
            .filter_map(|node_id| self.nodes.get(node_id).cloned())
            .collect();
        nodes.sort_by(|a, b| a.id.cmp(&b.id));

        let mut edges: Vec<SymbolEdge> = self
            .all_edges()
            .into_iter()
            .filter(|edge| {
                visited.contains(&edge.from)
                    && visited.contains(&edge.to)
                    && allowed_kinds
                        .as_ref()
                        .is_none_or(|kinds| kinds.contains(&edge.kind))
            })
            .collect();
        edges.sort_by(|a, b| {
            a.from
                .cmp(&b.from)
                .then(a.to.cmp(&b.to))
                .then(format!("{:?}", a.kind).cmp(&format!("{:?}", b.kind)))
        });

        GraphQueryResponse {
            roots: roots.clone(),
            truncated,
            stats: GraphQueryStats {
                root_count: roots.len(),
                node_count: nodes.len(),
                edge_count: edges.len(),
                requested_depth: request.depth,
                requested_limit: request.limit,
            },
            nodes,
            edges,
        }
    }

    fn resolve_roots(&self, mode: &GraphQueryMode, value: &str) -> Vec<String> {
        let normalized = value.trim();
        if normalized.is_empty() {
            return Vec::new();
        }

        let mut roots: Vec<String> = match mode {
            GraphQueryMode::NodeId => self
                .nodes
                .contains_key(normalized)
                .then_some(normalized.to_string())
                .into_iter()
                .collect(),
            GraphQueryMode::FilePath => {
                let candidates: Vec<String> = self
                    .file_path_index
                    .get(normalized)
                    .map(|entries| entries.iter().cloned().collect())
                    .unwrap_or_default();

                let mut file_roots: Vec<String> = candidates
                    .iter()
                    .filter_map(|node_id| {
                        self.nodes
                            .get(node_id)
                            .filter(|node| node.kind == SymbolKind::File)
                            .map(|_| node_id.clone())
                    })
                    .collect();
                file_roots.sort();
                file_roots.dedup();

                if file_roots.is_empty() {
                    let mut fallback = candidates;
                    fallback.sort();
                    fallback.dedup();
                    fallback
                } else {
                    file_roots
                }
            }
            GraphQueryMode::SymbolName => {
                let lowered = normalized.to_lowercase();
                self.symbol_name_index
                    .get(&lowered)
                    .map(|entries| entries.iter().cloned().collect())
                    .unwrap_or_default()
            }
        };

        roots.sort();
        roots.dedup();
        roots
    }

    fn neighbors(
        &self,
        node_id: &str,
        allowed_kinds: Option<&HashSet<EdgeKind>>,
    ) -> Vec<String> {
        let mut neighbors: HashSet<String> = HashSet::new();

        if let Some(outgoing) = self.adjacency.get(node_id) {
            for (to, kind) in outgoing {
                if allowed_kinds
                    .as_ref()
                    .is_some_and(|kinds| !kinds.contains(kind))
                {
                    continue;
                }
                neighbors.insert(to.clone());
            }
        }

        if let Some(incoming) = self.reverse_adjacency.get(node_id) {
            for (from, kind) in incoming {
                if allowed_kinds
                    .as_ref()
                    .is_some_and(|kinds| !kinds.contains(kind))
                {
                    continue;
                }
                neighbors.insert(from.clone());
            }
        }

        let mut ordered: Vec<String> = neighbors.into_iter().collect();
        ordered.sort();
        ordered
    }
}

fn remove_index_entry(index: &mut HashMap<String, HashSet<String>>, key: &str, value: &str) {
    if let Some(entries) = index.get_mut(key) {
        entries.remove(value);
        if entries.is_empty() {
            index.remove(key);
        }
    }
}
