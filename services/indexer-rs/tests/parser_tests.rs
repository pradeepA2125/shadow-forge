use ai_editor_indexer::graph::{EdgeKind, SymbolGraph, SymbolKind};
use ai_editor_indexer::parser::{LanguageParser, TreeSitterParser};
use std::path::Path;

#[test]
fn typescript_parser_emits_symbols_and_edges() {
    let parser = TreeSitterParser::new(std::path::PathBuf::from("."));
    let mut graph = SymbolGraph::default();
    let source = r#"
import { fetchUser } from "./api";
export class UserService extends BaseService {
  getUser() {
    return fetchUser();
  }
}
export function buildService() {
  return new UserService();
}
"#;

    parser
        .parse_file(Path::new("src/service.ts"), source, &mut graph)
        .expect("parse");

    let nodes = graph.all_nodes();
    let edges = graph.all_edges();
    assert!(nodes
        .iter()
        .any(|node| node.kind == SymbolKind::Class && node.name == "UserService"));
    assert!(nodes
        .iter()
        .any(|node| node.kind == SymbolKind::Function && node.name == "buildService"));
    assert!(edges.iter().any(|edge| edge.kind == EdgeKind::Imports));
    assert!(edges.iter().any(|edge| edge.kind == EdgeKind::Calls));
}

#[test]
fn python_parser_emits_symbols_and_edges() {
    use std::io::Write;
    // Create a real workspace so cross-file import resolution can be tested.
    let tmp = tempfile::tempdir().expect("tempdir");
    let ws = tmp.path();
    // Write the module that will be imported so resolve_python_module_to_file can find it.
    std::fs::create_dir_all(ws.join("app")).unwrap();
    let mut f = std::fs::File::create(ws.join("app/db.py")).unwrap();
    writeln!(f, "class Repo: pass").unwrap();

    let parser = TreeSitterParser::new(ws.to_path_buf());
    let mut graph = SymbolGraph::default();
    let source = r#"
from app.db import Repo

class AccountService:
    def get_user(self):
        return Repo.fetch()

def run():
    return AccountService()

async def async_run():
    pass
"#;

    parser
        .parse_file(&ws.join("app/service.py"), source, &mut graph)
        .expect("parse");

    let nodes = graph.all_nodes();
    let edges = graph.all_edges();

    // Class and methods must be extracted accurately (no docstring/assignment noise)
    assert!(nodes.iter().any(|n| n.kind == SymbolKind::Class && n.name == "AccountService"),
        "missing class AccountService");
    assert!(nodes.iter().any(|n| n.kind == SymbolKind::Method && n.name == "get_user"),
        "missing method get_user");
    assert!(nodes.iter().any(|n| n.kind == SymbolKind::Function && n.name == "run"),
        "missing function run");
    assert!(nodes.iter().any(|n| n.kind == SymbolKind::Function && n.name == "async_run"),
        "missing async function async_run");

    // File-to-file import edge: app/service.py → app/db.py (the key new capability)
    let target_id = format!("file:{}", ws.join("app/db.py").display());
    assert!(edges.iter().any(|e| e.kind == EdgeKind::Imports && e.to == target_id),
        "missing file-to-file import edge to app/db.py; edges: {edges:?}");

    // No garbage variable nodes from __slots__ or assignment tokenization
    assert!(!nodes.iter().any(|n| n.kind == SymbolKind::Variable && n.name == "self"),
        "spurious 'self' variable node");
    assert!(!nodes.iter().any(|n| n.kind == SymbolKind::Variable && n.name == "return"),
        "spurious 'return' variable node");
}

#[test]
fn python_parser_resolves_imports_through_monorepo_source_roots() {
    use std::io::Write;
    // Monorepo layout: a Python package sits one level below the workspace root.
    // Before source-root discovery, `from agentd.domain.x import T` only tried
    // workspace_root/agentd/domain/x.py and silently fell back to an external edge.
    let tmp = tempfile::tempdir().expect("tempdir");
    let ws = tmp.path();
    let pkg_root = ws.join("services/agentd-py");
    let pkg_dir = pkg_root.join("agentd");
    let domain_dir = pkg_dir.join("domain");
    std::fs::create_dir_all(&domain_dir).expect("mkdir domain");

    // `agentd` is a package; `services/agentd-py/` is therefore a source root.
    std::fs::File::create(pkg_dir.join("__init__.py")).expect("agentd init");
    std::fs::File::create(domain_dir.join("__init__.py")).expect("domain init");

    // The target module the importer references.
    let target = domain_dir.join("state_machine.py");
    {
        let mut f = std::fs::File::create(&target).expect("create state_machine");
        writeln!(f, "def transition(): pass").unwrap();
    }

    // The importing file, in a sibling package directory.
    let importer = pkg_dir.join("orchestrator").join("engine.py");
    std::fs::create_dir_all(importer.parent().unwrap()).unwrap();
    std::fs::File::create(pkg_dir.join("orchestrator/__init__.py")).expect("orch init");
    let importer_source = r#"
from agentd.domain.state_machine import transition

def run_task():
    transition()
"#;

    let parser = TreeSitterParser::new(ws.to_path_buf());
    let mut graph = SymbolGraph::default();
    parser
        .parse_file(&importer, importer_source, &mut graph)
        .expect("parse importer");

    let edges = graph.all_edges();
    let expected_target_id = format!("file:{}", target.display());

    // The whole point: a workspace→workspace Imports edge, not an external module edge.
    assert!(
        edges.iter().any(|e| e.kind == EdgeKind::Imports && e.to == expected_target_id),
        "expected Imports edge to {}; got: {:#?}",
        expected_target_id,
        edges
    );

    // And the no-longer-needed external fallback should NOT have been emitted for this module.
    assert!(
        !edges.iter().any(|e|
            e.kind == EdgeKind::Imports && e.to.contains("external:module:agentd.domain.state_machine")
        ),
        "external fallback emitted despite resolvable workspace import"
    );
}

#[test]
fn python_parser_emits_calls_placeholders_for_function_body() {
    use std::io::Write;
    let tmp = tempfile::tempdir().expect("tempdir");
    let ws = tmp.path();
    std::fs::create_dir_all(ws.join("app")).unwrap();
    let mut f = std::fs::File::create(ws.join("app/state_machine.py")).unwrap();
    writeln!(f, "def transition(): pass").unwrap();

    // engine.py has one top-level function whose body calls `transition`. The
    // parser must:
    //   (a) emit a Calls edge from `run_task` to `external:call:transition`
    //   (b) push a PlaceholderEdge so the resolver can later rewrite the edge
    //       to point at app/state_machine.py:transition.
    let parser = TreeSitterParser::new(ws.to_path_buf());
    let mut graph = SymbolGraph::default();
    let source = r#"
from app.state_machine import transition

def run_task():
    transition()
    return 1
"#;

    parser
        .parse_file(&ws.join("app/engine.py"), source, &mut graph)
        .expect("parse");

    let nodes = graph.all_nodes();
    let edges = graph.all_edges();

    // run_task is captured as a workspace function.
    let run_task_id = nodes
        .iter()
        .find(|n| n.kind == SymbolKind::Function && n.name == "run_task")
        .map(|n| n.id.clone())
        .expect("missing run_task node");

    // A Calls edge from run_task to the external:call:transition placeholder.
    let calls_edge = edges
        .iter()
        .find(|e| {
            e.kind == EdgeKind::Calls
                && e.from == run_task_id
                && e.to.contains("external:call:transition")
        })
        .expect("missing Calls edge run_task → external:call:transition");
    assert_eq!(calls_edge.kind, EdgeKind::Calls);

    // The PlaceholderEdge list carries the same call site so the resolver
    // stage can later query the LSP and rewrite.
    let placeholders = graph.take_placeholders();
    let transition_placeholder = placeholders
        .iter()
        .find(|p| {
            p.from_id == run_task_id
                && p.external_to_id.contains("external:call:transition")
        })
        .expect("missing PlaceholderEdge for transition()");
    assert_eq!(transition_placeholder.edge_kind, EdgeKind::Calls);
    assert!(transition_placeholder.line >= 1); // 0-indexed LSP line for `transition` call
}

#[test]
fn rust_parser_emits_symbols_and_edges() {
    let parser = TreeSitterParser::new(std::path::PathBuf::from("."));
    let mut graph = SymbolGraph::default();
    let source = r#"
use crate::storage::Store;

struct App;
trait Runner { fn run(&self); }

impl App {
    fn build(&self) {
        Store::new();
    }
}

fn main() {
    App::build(&App);
}
"#;

    parser
        .parse_file(Path::new("src/main.rs"), source, &mut graph)
        .expect("parse");

    let nodes = graph.all_nodes();
    let edges = graph.all_edges();
    assert!(nodes
        .iter()
        .any(|node| node.kind == SymbolKind::Class && node.name == "App"));
    assert!(nodes
        .iter()
        .any(|node| node.kind == SymbolKind::Interface && node.name == "Runner"));
    assert!(nodes
        .iter()
        .any(|node| node.kind == SymbolKind::Function && node.name == "main"));
    assert!(edges.iter().any(|edge| edge.kind == EdgeKind::Imports));
}
