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

#[derive(Debug, Default, Clone)]
pub struct SymbolGraph {
    nodes: HashMap<String, SymbolNode>,
    edges: HashSet<(String, String, EdgeKind)>,
    adjacency: HashMap<String, HashSet<(String, EdgeKind)>>,
    reverse_adjacency: HashMap<String, HashSet<(String, EdgeKind)>>,
    symbol_name_index: HashMap<String, HashSet<String>>,
    file_path_index: HashMap<String, HashSet<String>>,
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
