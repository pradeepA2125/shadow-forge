use ai_editor_indexer::graph::{EdgeKind, SymbolGraph, SymbolKind};
use ai_editor_indexer::parser::{LanguageParser, TreeSitterParser};
use std::path::Path;

#[test]
fn typescript_parser_emits_symbols_and_edges() {
    let parser = TreeSitterParser;
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
    let parser = TreeSitterParser;
    let mut graph = SymbolGraph::default();
    let source = r#"
from app.db import Repo

class AccountService:
    def get_user(self):
        return Repo.fetch()

def run():
    return AccountService()
"#;

    parser
        .parse_file(Path::new("app/service.py"), source, &mut graph)
        .expect("parse");

    let nodes = graph.all_nodes();
    let edges = graph.all_edges();
    assert!(nodes
        .iter()
        .any(|node| node.kind == SymbolKind::Class && node.name == "AccountService"));
    assert!(nodes
        .iter()
        .any(|node| node.kind == SymbolKind::Method && node.name == "get_user"));
    assert!(edges.iter().any(|edge| edge.kind == EdgeKind::Imports));
    assert!(edges.iter().any(|edge| edge.kind == EdgeKind::Calls));
}

#[test]
fn rust_parser_emits_symbols_and_edges() {
    let parser = TreeSitterParser;
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
