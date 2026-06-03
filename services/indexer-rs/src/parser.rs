use anyhow::{anyhow, Context, Result};
use rustpython_parser::{ast, ast::Ranged, Parse};
use std::path::{Path, PathBuf};
use tree_sitter::{Node, Parser};

use crate::graph::{EdgeKind, PlaceholderEdge, SymbolEdge, SymbolGraph, SymbolKind, SymbolNode};

pub trait LanguageParser: Send + Sync {
    fn language_name(&self) -> &'static str;
    fn parse_file(&self, file_path: &Path, source: &str, graph: &mut SymbolGraph) -> Result<()>;
}

pub struct TreeSitterParser {
    pub workspace_root: PathBuf,
    /// Directories that act as Python sys.path entries: not packages themselves
    /// (no __init__.py) but containing at least one immediate package child.
    /// Resolves `from agentd.X import Y` in a monorepo where `agentd/` sits at
    /// `services/agentd-py/agentd/` rather than at the workspace root.
    /// Computed once at construction so the cost (one directory walk) is paid
    /// per indexing run, not per file.
    python_source_roots: Vec<PathBuf>,
}

impl TreeSitterParser {
    pub fn new(workspace_root: PathBuf) -> Self {
        let python_source_roots = discover_python_source_roots(&workspace_root);
        Self {
            workspace_root,
            python_source_roots,
        }
    }
}

#[derive(Clone, Copy)]
enum ParserLanguage {
    TypeScript,
    Tsx,
    Rust,
}

#[derive(Clone)]
struct RustImplContext {
    owner_id: String,
}

impl ParserLanguage {
    fn from_path(path: &Path) -> Option<Self> {
        match path
            .extension()
            .and_then(|ext| ext.to_str())
            .unwrap_or_default()
            .to_lowercase()
            .as_str()
        {
            "ts" => Some(Self::TypeScript),
            "tsx" => Some(Self::Tsx),
            "rs" => Some(Self::Rust),
            _ => None,
        }
    }

    fn configure_parser(self, parser: &mut Parser) -> Result<()> {
        match self {
            Self::TypeScript => parser
                .set_language(&tree_sitter_typescript::LANGUAGE_TYPESCRIPT.into())
                .context("failed to set TypeScript grammar"),
            Self::Tsx => parser
                .set_language(&tree_sitter_typescript::LANGUAGE_TSX.into())
                .context("failed to set TSX grammar"),
            Self::Rust => parser
                .set_language(&tree_sitter_rust::LANGUAGE.into())
                .context("failed to set Rust grammar"),
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            Self::TypeScript => "typescript",
            Self::Tsx => "tsx",
            Self::Rust => "rust",
        }
    }
}

impl LanguageParser for TreeSitterParser {
    fn language_name(&self) -> &'static str {
        "tree-sitter-registry"
    }

    fn parse_file(&self, file_path: &Path, source: &str, graph: &mut SymbolGraph) -> Result<()> {
        let file_id = upsert_file_node(graph, file_path);

        if file_path.extension().and_then(|e| e.to_str()) == Some("py") {
            return extract_python_ruff(
                file_path,
                source,
                graph,
                &file_id,
                &self.workspace_root,
                &self.python_source_roots,
            );
        }

        let Some(language) = ParserLanguage::from_path(file_path) else {
            return Ok(());
        };

        let mut parser = Parser::new();
        language.configure_parser(&mut parser)?;

        let tree = parser.parse(source, None).ok_or_else(|| {
            anyhow!(
                "tree-sitter returned no parse tree for {}",
                file_path.display()
            )
        })?;

        let root = tree.root_node();
        match language {
            ParserLanguage::TypeScript | ParserLanguage::Tsx => {
                extract_typescript(file_path, source, graph, &file_id, root, None)
            }
            ParserLanguage::Rust => extract_rust(file_path, source, graph, &file_id, root, None),
        }

        if root.has_error() {
            return Err(anyhow!(
                "tree-sitter parse completed with syntax errors ({})",
                language.as_str()
            ));
        }

        Ok(())
    }
}

fn extract_typescript(
    file_path: &Path,
    source: &str,
    graph: &mut SymbolGraph,
    file_id: &str,
    node: Node<'_>,
    current_class: Option<String>,
) {
    match node.kind() {
        "import_statement" => {
            if let Some(module_name) = extract_quoted_fragment(&node_text(node, source).unwrap_or_default()) {
                let module_id = upsert_external_symbol(
                    graph,
                    file_path,
                    "module",
                    &module_name,
                    SymbolKind::Module,
                    node_line(node),
                );
                graph.add_edge(SymbolEdge {
                    from: file_id.to_string(),
                    to: module_id,
                    kind: EdgeKind::Imports,
                });
            }
        }
        "class_declaration" => {
            if let Some(name) = field_identifier(node, "name", source) {
                let class_id = upsert_named_symbol(
                    graph,
                    file_path,
                    file_id,
                    "class",
                    &name,
                    SymbolKind::Class,
                    node_line(node),
                );
                graph.add_edge(SymbolEdge {
                    from: file_id.to_string(),
                    to: class_id.clone(),
                    kind: EdgeKind::References,
                });

                for parent_name in extract_extends_targets(&node_text(node, source).unwrap_or_default()) {
                    let parent_id = upsert_external_symbol(
                        graph,
                        file_path,
                        "class",
                        &parent_name,
                        SymbolKind::Class,
                        node_line(node),
                    );
                    graph.add_edge(SymbolEdge {
                        from: class_id.clone(),
                        to: parent_id,
                        kind: EdgeKind::Inherits,
                    });
                }

                recurse_typescript(file_path, source, graph, file_id, node, Some(class_id));
                return;
            }
        }
        "interface_declaration" => {
            if let Some(name) = field_identifier(node, "name", source) {
                let interface_id = upsert_named_symbol(
                    graph,
                    file_path,
                    file_id,
                    "interface",
                    &name,
                    SymbolKind::Interface,
                    node_line(node),
                );
                graph.add_edge(SymbolEdge {
                    from: file_id.to_string(),
                    to: interface_id.clone(),
                    kind: EdgeKind::References,
                });
                for parent_name in extract_extends_targets(&node_text(node, source).unwrap_or_default()) {
                    let parent_id = upsert_external_symbol(
                        graph,
                        file_path,
                        "interface",
                        &parent_name,
                        SymbolKind::Interface,
                        node_line(node),
                    );
                    graph.add_edge(SymbolEdge {
                        from: interface_id.clone(),
                        to: parent_id,
                        kind: EdgeKind::Inherits,
                    });
                }
            }
        }
        "function_declaration" => {
            if let Some(name) = field_identifier(node, "name", source) {
                let function_id = upsert_scoped_symbol(
                    graph,
                    file_path,
                    file_id,
                    "function",
                    &name,
                    SymbolKind::Function,
                    node_line(node),
                );
                graph.add_edge(SymbolEdge {
                    from: file_id.to_string(),
                    to: function_id,
                    kind: EdgeKind::References,
                });
            }
        }
        "method_definition" => {
            if let Some(class_id) = current_class.as_ref() {
                if let Some(name) = field_identifier(node, "name", source) {
                    let method_id = upsert_member_symbol(
                        graph,
                        file_path,
                        file_id,
                        "method",
                        class_id,
                        &name,
                        SymbolKind::Method,
                        node_line(node),
                    );
                    graph.add_edge(SymbolEdge {
                        from: class_id.clone(),
                        to: method_id,
                        kind: EdgeKind::References,
                    });
                }
            }
        }
        "lexical_declaration" | "variable_declaration" => {
            let variables = extract_variable_declarators(node, source);
            for name in variables {
                let variable_id = upsert_scoped_symbol(
                    graph,
                    file_path,
                    file_id,
                    "variable",
                    &name,
                    SymbolKind::Variable,
                    node_line(node),
                );
                graph.add_edge(SymbolEdge {
                    from: file_id.to_string(),
                    to: variable_id,
                    kind: EdgeKind::References,
                });
            }
        }
        "call_expression" | "new_expression" => {
            if let Some(target_name) = extract_call_target(node, source) {
                let target_id = upsert_external_symbol(
                    graph,
                    file_path,
                    "call",
                    &target_name,
                    SymbolKind::Function,
                    node_line(node),
                );
                let from = current_class.clone().unwrap_or_else(|| file_id.to_string());
                graph.add_edge(SymbolEdge {
                    from: from.clone(),
                    to: target_id.clone(),
                    kind: EdgeKind::Calls,
                });
                // PlaceholderEdge so the resolver can rewrite this to a
                // workspace symbol once tsserver answers. Position points at
                // the callable identifier — for `obj.method()` that's the
                // property's start, not the receiver's, so tsserver resolves
                // the method itself rather than the receiver variable.
                if let Some(point) = typescript_callable_position(node) {
                    graph.push_placeholder(PlaceholderEdge {
                        from_id: from,
                        external_to_id: target_id,
                        file_path: file_path.to_path_buf(),
                        line: point.row as u32,
                        character: point.column as u32,
                        edge_kind: EdgeKind::Calls,
                    });
                }
            }
        }
        _ => {}
    }

    recurse_typescript(file_path, source, graph, file_id, node, current_class);
}

fn recurse_typescript(
    file_path: &Path,
    source: &str,
    graph: &mut SymbolGraph,
    file_id: &str,
    node: Node<'_>,
    current_class: Option<String>,
) {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        extract_typescript(
            file_path,
            source,
            graph,
            file_id,
            child,
            current_class.clone(),
        );
    }
}

fn extract_python_ruff(
    file_path: &Path,
    source: &str,
    graph: &mut SymbolGraph,
    file_id: &str,
    workspace_root: &Path,
    python_source_roots: &[PathBuf],
) -> Result<()> {
    let stmts = ast::Suite::parse(source, file_path.to_str().unwrap_or("<file>"))
        .map_err(|e| anyhow!("Python parse error in {}: {e}", file_path.display()))?;
    // current_enclosing at module scope is the file node itself — any Calls
    // emitted from top-level statements (rare but real: module-level setup
    // code) are attributed to the file.
    walk_python_body(
        file_path,
        source,
        graph,
        file_id,
        workspace_root,
        python_source_roots,
        &stmts,
        None,
        file_id,
    );
    Ok(())
}

fn walk_python_body(
    file_path: &Path,
    source: &str,
    graph: &mut SymbolGraph,
    file_id: &str,
    workspace_root: &Path,
    python_source_roots: &[PathBuf],
    stmts: &[ast::Stmt],
    current_class: Option<&str>,
    current_enclosing: &str,
) {
    for stmt in stmts {
        match stmt {
            ast::Stmt::FunctionDef(f) => {
                let name = f.name.as_str();
                let line = py_offset_to_line(source, usize::from(f.range.start()));
                let fn_id =
                    emit_python_fn(file_path, graph, file_id, name, line, current_class);
                // Recurse into the body so nested defs are captured AND so
                // each Call inside attributes to this function/method.
                walk_python_body(
                    file_path,
                    source,
                    graph,
                    file_id,
                    workspace_root,
                    python_source_roots,
                    &f.body,
                    None,
                    fn_id.as_str(),
                );
            }
            ast::Stmt::AsyncFunctionDef(f) => {
                let name = f.name.as_str();
                let line = py_offset_to_line(source, usize::from(f.range.start()));
                let fn_id =
                    emit_python_fn(file_path, graph, file_id, name, line, current_class);
                walk_python_body(
                    file_path,
                    source,
                    graph,
                    file_id,
                    workspace_root,
                    python_source_roots,
                    &f.body,
                    None,
                    fn_id.as_str(),
                );
            }
            ast::Stmt::ClassDef(c) => {
                let name = c.name.as_str();
                let line = py_offset_to_line(source, usize::from(c.range.start()));
                let class_id = upsert_named_symbol(
                    graph, file_path, file_id, "class", name, SymbolKind::Class, line,
                );
                graph.add_edge(SymbolEdge {
                    from: file_id.to_string(),
                    to: class_id.clone(),
                    kind: EdgeKind::References,
                });
                for base in &c.bases {
                    if let Some(base_name) = py_expr_base_name(base) {
                        let parent_id = upsert_external_symbol(
                            graph, file_path, "class", &base_name, SymbolKind::Class, line,
                        );
                        graph.add_edge(SymbolEdge {
                            from: class_id.clone(),
                            to: parent_id,
                            kind: EdgeKind::Inherits,
                        });
                    }
                }
                // Inside the class body, methods are defined; their bodies'
                // call sites attribute to the method id, not the class id.
                // current_enclosing for the class scope itself is left as the
                // outer enclosing (file or outer function) so module-level
                // calls — class-level assignments using calls (e.g. `field =
                // Field(...)` inside a Pydantic class) — attribute correctly.
                walk_python_body(
                    file_path,
                    source,
                    graph,
                    file_id,
                    workspace_root,
                    python_source_roots,
                    &c.body,
                    Some(class_id.as_str()),
                    current_enclosing,
                );
            }
            ast::Stmt::Import(i) => {
                for alias in &i.names {
                    let module_name = alias.name.as_str();
                    if let Some(target) = resolve_python_module_to_file(
                        module_name,
                        0,
                        file_path,
                        workspace_root,
                        python_source_roots,
                    ) {
                        let target_id = format!("file:{}", target.display());
                        graph.upsert_node(SymbolNode {
                            id: target_id.clone(),
                            path: target.display().to_string(),
                            name: target.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default(),
                            kind: SymbolKind::File,
                            line: 1,
                        });
                        graph.add_edge(SymbolEdge {
                            from: file_id.to_string(),
                            to: target_id,
                            kind: EdgeKind::Imports,
                        });
                    } else {
                        let module_id = upsert_external_symbol(
                            graph, file_path, "module", module_name, SymbolKind::Module, 0,
                        );
                        graph.add_edge(SymbolEdge {
                            from: file_id.to_string(),
                            to: module_id,
                            kind: EdgeKind::Imports,
                        });
                    }
                }
            }
            ast::Stmt::ImportFrom(i) => {
                // ast::Int wraps u32 and exposes .to_u32()
                let level: u32 = i.level.as_ref().map(|n| n.to_u32()).unwrap_or(0);
                if let Some(module_id_val) = &i.module {
                    let module_name = module_id_val.as_str();
                    if let Some(target) = resolve_python_module_to_file(
                        module_name,
                        level,
                        file_path,
                        workspace_root,
                        python_source_roots,
                    ) {
                        let target_id = format!("file:{}", target.display());
                        graph.upsert_node(SymbolNode {
                            id: target_id.clone(),
                            path: target.display().to_string(),
                            name: target.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default(),
                            kind: SymbolKind::File,
                            line: 1,
                        });
                        graph.add_edge(SymbolEdge {
                            from: file_id.to_string(),
                            to: target_id,
                            kind: EdgeKind::Imports,
                        });
                    } else {
                        let module_id = upsert_external_symbol(
                            graph, file_path, "module", module_name, SymbolKind::Module, 0,
                        );
                        graph.add_edge(SymbolEdge {
                            from: file_id.to_string(),
                            to: module_id,
                            kind: EdgeKind::Imports,
                        });
                    }
                }
            }
            other => {
                scan_python_stmt_for_calls(
                    file_path,
                    source,
                    graph,
                    file_id,
                    workspace_root,
                    python_source_roots,
                    other,
                    current_class,
                    current_enclosing,
                );
            }
        }
    }
}

/// Find every `Expr::Call` reachable from a non-def statement and emit a
/// placeholder `Calls` edge for each. Recurses through nested control-flow
/// bodies via `walk_python_body` so the enclosing-scope attribution stays
/// correct. The list of statement variants is exhaustive for what shows up
/// in our codebases; anything missed falls through silently (no panic) and is
/// recovered by grep on the model side, same as today.
#[allow(clippy::too_many_arguments)]
fn scan_python_stmt_for_calls(
    file_path: &Path,
    source: &str,
    graph: &mut SymbolGraph,
    file_id: &str,
    workspace_root: &Path,
    python_source_roots: &[PathBuf],
    stmt: &ast::Stmt,
    current_class: Option<&str>,
    current_enclosing: &str,
) {
    // Inline closure to scan a single expression for nested calls.
    let scan = |expr: &ast::Expr, g: &mut SymbolGraph| {
        scan_python_expr_for_calls(file_path, source, g, expr, current_enclosing);
    };

    match stmt {
        ast::Stmt::Expr(s) => scan(&s.value, graph),
        ast::Stmt::Assign(s) => {
            scan(&s.value, graph);
            for target in &s.targets {
                scan(target, graph);
            }
        }
        ast::Stmt::AnnAssign(s) => {
            if let Some(value) = &s.value {
                scan(value, graph);
            }
            scan(&s.target, graph);
            scan(&s.annotation, graph);
        }
        ast::Stmt::AugAssign(s) => {
            scan(&s.target, graph);
            scan(&s.value, graph);
        }
        ast::Stmt::Return(s) => {
            if let Some(value) = &s.value {
                scan(value, graph);
            }
        }
        ast::Stmt::If(s) => {
            scan(&s.test, graph);
            recurse_body(
                file_path,
                source,
                graph,
                file_id,
                workspace_root,
                python_source_roots,
                &s.body,
                current_class,
                current_enclosing,
            );
            recurse_body(
                file_path,
                source,
                graph,
                file_id,
                workspace_root,
                python_source_roots,
                &s.orelse,
                current_class,
                current_enclosing,
            );
        }
        ast::Stmt::While(s) => {
            scan(&s.test, graph);
            recurse_body(
                file_path,
                source,
                graph,
                file_id,
                workspace_root,
                python_source_roots,
                &s.body,
                current_class,
                current_enclosing,
            );
            recurse_body(
                file_path,
                source,
                graph,
                file_id,
                workspace_root,
                python_source_roots,
                &s.orelse,
                current_class,
                current_enclosing,
            );
        }
        ast::Stmt::For(s) => {
            scan(&s.iter, graph);
            scan(&s.target, graph);
            recurse_body(
                file_path,
                source,
                graph,
                file_id,
                workspace_root,
                python_source_roots,
                &s.body,
                current_class,
                current_enclosing,
            );
            recurse_body(
                file_path,
                source,
                graph,
                file_id,
                workspace_root,
                python_source_roots,
                &s.orelse,
                current_class,
                current_enclosing,
            );
        }
        ast::Stmt::AsyncFor(s) => {
            scan(&s.iter, graph);
            scan(&s.target, graph);
            recurse_body(
                file_path,
                source,
                graph,
                file_id,
                workspace_root,
                python_source_roots,
                &s.body,
                current_class,
                current_enclosing,
            );
            recurse_body(
                file_path,
                source,
                graph,
                file_id,
                workspace_root,
                python_source_roots,
                &s.orelse,
                current_class,
                current_enclosing,
            );
        }
        ast::Stmt::With(s) => {
            for item in &s.items {
                scan(&item.context_expr, graph);
                if let Some(vars) = &item.optional_vars {
                    scan(vars, graph);
                }
            }
            recurse_body(
                file_path,
                source,
                graph,
                file_id,
                workspace_root,
                python_source_roots,
                &s.body,
                current_class,
                current_enclosing,
            );
        }
        ast::Stmt::AsyncWith(s) => {
            for item in &s.items {
                scan(&item.context_expr, graph);
                if let Some(vars) = &item.optional_vars {
                    scan(vars, graph);
                }
            }
            recurse_body(
                file_path,
                source,
                graph,
                file_id,
                workspace_root,
                python_source_roots,
                &s.body,
                current_class,
                current_enclosing,
            );
        }
        ast::Stmt::Try(s) => {
            recurse_body(
                file_path,
                source,
                graph,
                file_id,
                workspace_root,
                python_source_roots,
                &s.body,
                current_class,
                current_enclosing,
            );
            for handler in &s.handlers {
                let ast::ExceptHandler::ExceptHandler(eh) = handler;
                if let Some(typ) = &eh.type_ {
                    scan(typ, graph);
                }
                recurse_body(
                    file_path,
                    source,
                    graph,
                    file_id,
                    workspace_root,
                    python_source_roots,
                    &eh.body,
                    current_class,
                    current_enclosing,
                );
            }
            recurse_body(
                file_path,
                source,
                graph,
                file_id,
                workspace_root,
                python_source_roots,
                &s.orelse,
                current_class,
                current_enclosing,
            );
            recurse_body(
                file_path,
                source,
                graph,
                file_id,
                workspace_root,
                python_source_roots,
                &s.finalbody,
                current_class,
                current_enclosing,
            );
        }
        ast::Stmt::TryStar(s) => {
            recurse_body(
                file_path,
                source,
                graph,
                file_id,
                workspace_root,
                python_source_roots,
                &s.body,
                current_class,
                current_enclosing,
            );
            for handler in &s.handlers {
                let ast::ExceptHandler::ExceptHandler(eh) = handler;
                if let Some(typ) = &eh.type_ {
                    scan(typ, graph);
                }
                recurse_body(
                    file_path,
                    source,
                    graph,
                    file_id,
                    workspace_root,
                    python_source_roots,
                    &eh.body,
                    current_class,
                    current_enclosing,
                );
            }
            recurse_body(
                file_path,
                source,
                graph,
                file_id,
                workspace_root,
                python_source_roots,
                &s.orelse,
                current_class,
                current_enclosing,
            );
            recurse_body(
                file_path,
                source,
                graph,
                file_id,
                workspace_root,
                python_source_roots,
                &s.finalbody,
                current_class,
                current_enclosing,
            );
        }
        ast::Stmt::Raise(s) => {
            if let Some(exc) = &s.exc {
                scan(exc, graph);
            }
            if let Some(cause) = &s.cause {
                scan(cause, graph);
            }
        }
        ast::Stmt::Assert(s) => {
            scan(&s.test, graph);
            if let Some(msg) = &s.msg {
                scan(msg, graph);
            }
        }
        ast::Stmt::Delete(s) => {
            for target in &s.targets {
                scan(target, graph);
            }
        }
        // Match would require iterating cases/patterns; very rare in our code,
        // skip for now. Pass/Break/Continue/Global/Nonlocal carry no exprs.
        _ => {}
    }
}

/// Local convenience for recursing into a nested body, preserving the
/// enclosing-scope attribution that the parent statement was operating under.
#[allow(clippy::too_many_arguments)]
fn recurse_body(
    file_path: &Path,
    source: &str,
    graph: &mut SymbolGraph,
    file_id: &str,
    workspace_root: &Path,
    python_source_roots: &[PathBuf],
    stmts: &[ast::Stmt],
    current_class: Option<&str>,
    current_enclosing: &str,
) {
    walk_python_body(
        file_path,
        source,
        graph,
        file_id,
        workspace_root,
        python_source_roots,
        stmts,
        current_class,
        current_enclosing,
    );
}

/// Walk an expression subtree, emitting a `Calls` placeholder for every
/// `Expr::Call` encountered. Recurses through compound expressions so calls
/// nested in lambdas, comprehensions, ternaries, etc. are captured.
fn scan_python_expr_for_calls(
    file_path: &Path,
    source: &str,
    graph: &mut SymbolGraph,
    expr: &ast::Expr,
    current_enclosing: &str,
) {
    match expr {
        ast::Expr::Call(c) => {
            if let Some(name) = py_expr_base_name(&c.func) {
                // For Attribute callees (`receiver.method(...)`) pyright needs
                // the position INSIDE the attribute name, not at the start of
                // the receiver — querying at the receiver resolves the
                // variable instead, and pyright returns None for the method.
                // For Name callees the start of the name is correct.
                let offset = python_callable_offset(source, &c.func);
                let line_1 = py_offset_to_line(source, offset);
                let (lsp_line, lsp_char) = py_offset_to_lsp_position(source, offset);
                let external_id = upsert_external_symbol(
                    graph,
                    file_path,
                    "call",
                    &name,
                    SymbolKind::Function,
                    line_1,
                );
                graph.add_edge(SymbolEdge {
                    from: current_enclosing.to_string(),
                    to: external_id.clone(),
                    kind: EdgeKind::Calls,
                });
                graph.push_placeholder(PlaceholderEdge {
                    from_id: current_enclosing.to_string(),
                    external_to_id: external_id,
                    file_path: file_path.to_path_buf(),
                    line: lsp_line,
                    character: lsp_char,
                    edge_kind: EdgeKind::Calls,
                });
            }
            scan_python_expr_for_calls(file_path, source, graph, &c.func, current_enclosing);
            for arg in &c.args {
                scan_python_expr_for_calls(file_path, source, graph, arg, current_enclosing);
            }
            for kw in &c.keywords {
                scan_python_expr_for_calls(file_path, source, graph, &kw.value, current_enclosing);
            }
        }
        ast::Expr::Attribute(a) => {
            scan_python_expr_for_calls(file_path, source, graph, &a.value, current_enclosing);
        }
        ast::Expr::Subscript(s) => {
            scan_python_expr_for_calls(file_path, source, graph, &s.value, current_enclosing);
            scan_python_expr_for_calls(file_path, source, graph, &s.slice, current_enclosing);
        }
        ast::Expr::Slice(s) => {
            if let Some(l) = &s.lower {
                scan_python_expr_for_calls(file_path, source, graph, l, current_enclosing);
            }
            if let Some(u) = &s.upper {
                scan_python_expr_for_calls(file_path, source, graph, u, current_enclosing);
            }
            if let Some(st) = &s.step {
                scan_python_expr_for_calls(file_path, source, graph, st, current_enclosing);
            }
        }
        ast::Expr::BoolOp(o) => {
            for v in &o.values {
                scan_python_expr_for_calls(file_path, source, graph, v, current_enclosing);
            }
        }
        ast::Expr::BinOp(o) => {
            scan_python_expr_for_calls(file_path, source, graph, &o.left, current_enclosing);
            scan_python_expr_for_calls(file_path, source, graph, &o.right, current_enclosing);
        }
        ast::Expr::UnaryOp(o) => {
            scan_python_expr_for_calls(file_path, source, graph, &o.operand, current_enclosing);
        }
        ast::Expr::Compare(c) => {
            scan_python_expr_for_calls(file_path, source, graph, &c.left, current_enclosing);
            for v in &c.comparators {
                scan_python_expr_for_calls(file_path, source, graph, v, current_enclosing);
            }
        }
        ast::Expr::IfExp(e) => {
            scan_python_expr_for_calls(file_path, source, graph, &e.test, current_enclosing);
            scan_python_expr_for_calls(file_path, source, graph, &e.body, current_enclosing);
            scan_python_expr_for_calls(file_path, source, graph, &e.orelse, current_enclosing);
        }
        ast::Expr::Lambda(l) => {
            scan_python_expr_for_calls(file_path, source, graph, &l.body, current_enclosing);
        }
        ast::Expr::NamedExpr(n) => {
            scan_python_expr_for_calls(file_path, source, graph, &n.value, current_enclosing);
            scan_python_expr_for_calls(file_path, source, graph, &n.target, current_enclosing);
        }
        ast::Expr::Await(a) => {
            scan_python_expr_for_calls(file_path, source, graph, &a.value, current_enclosing);
        }
        ast::Expr::Yield(y) => {
            if let Some(v) = &y.value {
                scan_python_expr_for_calls(file_path, source, graph, v, current_enclosing);
            }
        }
        ast::Expr::YieldFrom(y) => {
            scan_python_expr_for_calls(file_path, source, graph, &y.value, current_enclosing);
        }
        ast::Expr::FormattedValue(f) => {
            scan_python_expr_for_calls(file_path, source, graph, &f.value, current_enclosing);
        }
        ast::Expr::JoinedStr(j) => {
            for part in &j.values {
                scan_python_expr_for_calls(file_path, source, graph, part, current_enclosing);
            }
        }
        ast::Expr::List(l) => {
            for e in &l.elts {
                scan_python_expr_for_calls(file_path, source, graph, e, current_enclosing);
            }
        }
        ast::Expr::Tuple(t) => {
            for e in &t.elts {
                scan_python_expr_for_calls(file_path, source, graph, e, current_enclosing);
            }
        }
        ast::Expr::Set(s) => {
            for e in &s.elts {
                scan_python_expr_for_calls(file_path, source, graph, e, current_enclosing);
            }
        }
        ast::Expr::Dict(d) => {
            for k in d.keys.iter().flatten() {
                scan_python_expr_for_calls(file_path, source, graph, k, current_enclosing);
            }
            for v in &d.values {
                scan_python_expr_for_calls(file_path, source, graph, v, current_enclosing);
            }
        }
        ast::Expr::ListComp(c) => {
            scan_python_expr_for_calls(file_path, source, graph, &c.elt, current_enclosing);
            for gen in &c.generators {
                scan_python_expr_for_calls(file_path, source, graph, &gen.iter, current_enclosing);
                for cond in &gen.ifs {
                    scan_python_expr_for_calls(file_path, source, graph, cond, current_enclosing);
                }
            }
        }
        ast::Expr::SetComp(c) => {
            scan_python_expr_for_calls(file_path, source, graph, &c.elt, current_enclosing);
            for gen in &c.generators {
                scan_python_expr_for_calls(file_path, source, graph, &gen.iter, current_enclosing);
                for cond in &gen.ifs {
                    scan_python_expr_for_calls(file_path, source, graph, cond, current_enclosing);
                }
            }
        }
        ast::Expr::DictComp(c) => {
            scan_python_expr_for_calls(file_path, source, graph, &c.key, current_enclosing);
            scan_python_expr_for_calls(file_path, source, graph, &c.value, current_enclosing);
            for gen in &c.generators {
                scan_python_expr_for_calls(file_path, source, graph, &gen.iter, current_enclosing);
                for cond in &gen.ifs {
                    scan_python_expr_for_calls(file_path, source, graph, cond, current_enclosing);
                }
            }
        }
        ast::Expr::GeneratorExp(c) => {
            scan_python_expr_for_calls(file_path, source, graph, &c.elt, current_enclosing);
            for gen in &c.generators {
                scan_python_expr_for_calls(file_path, source, graph, &gen.iter, current_enclosing);
                for cond in &gen.ifs {
                    scan_python_expr_for_calls(file_path, source, graph, cond, current_enclosing);
                }
            }
        }
        ast::Expr::Starred(s) => {
            scan_python_expr_for_calls(file_path, source, graph, &s.value, current_enclosing);
        }
        // Leaf nodes (Name, Constant) and rarely-call-bearing expressions
        // (Subscript handled above; Subscript itself only contains value+slice).
        _ => {}
    }
}

fn emit_python_fn(
    file_path: &Path,
    graph: &mut SymbolGraph,
    file_id: &str,
    name: &str,
    line: u32,
    current_class: Option<&str>,
) -> String {
    if let Some(class_id) = current_class {
        let method_id = upsert_member_symbol(
            graph, file_path, file_id, "method", class_id, name, SymbolKind::Method, line,
        );
        graph.add_edge(SymbolEdge {
            from: class_id.to_string(),
            to: method_id.clone(),
            kind: EdgeKind::References,
        });
        method_id
    } else {
        let function_id = upsert_scoped_symbol(
            graph, file_path, file_id, "function", name, SymbolKind::Function, line,
        );
        graph.add_edge(SymbolEdge {
            from: file_id.to_string(),
            to: function_id.clone(),
            kind: EdgeKind::References,
        });
        function_id
    }
}

fn py_expr_base_name(expr: &ast::Expr) -> Option<String> {
    match expr {
        ast::Expr::Name(n) => Some(n.id.as_str().to_string()),
        ast::Expr::Attribute(a) => Some(a.attr.as_str().to_string()),
        _ => None,
    }
}

fn resolve_python_module_to_file(
    module: &str,
    level: u32,
    file_path: &Path,
    workspace_root: &Path,
    python_source_roots: &[PathBuf],
) -> Option<PathBuf> {
    let rel_path = module.replace('.', "/");

    // Relative imports (`from . import x`, `from ..a.b import x`) are anchored to the
    // importing file's package directory; source roots don't apply.
    if level > 0 {
        let mut dir = file_path.parent().unwrap_or(workspace_root);
        for _ in 1..level {
            dir = dir.parent().unwrap_or(workspace_root);
        }
        let module_file = dir.join(format!("{rel_path}.py"));
        if module_file.exists() {
            return Some(module_file);
        }
        let pkg_init = dir.join(&rel_path).join("__init__.py");
        if pkg_init.exists() {
            return Some(pkg_init);
        }
        return None;
    }

    // Absolute imports: try each discovered Python source root in turn. Falling back
    // to workspace_root preserves the legacy single-root behaviour for flat layouts
    // (the test fixture uses ws_root directly, with no nested source root).
    let candidates = python_source_roots
        .iter()
        .map(|p| p.as_path())
        .chain(std::iter::once(workspace_root));
    for root in candidates {
        let module_file = root.join(format!("{rel_path}.py"));
        if module_file.exists() {
            return Some(module_file);
        }
        let pkg_init = root.join(&rel_path).join("__init__.py");
        if pkg_init.exists() {
            return Some(pkg_init);
        }
    }
    None
}

/// Discover directories that act as Python sys.path entries: a directory D is a
/// source root iff it is NOT itself a package (no D/__init__.py) but at least
/// one immediate child directory IS a package (child/__init__.py exists). Walk
/// the workspace, descending only through non-packages so we find every nested
/// source root in a monorepo while avoiding redundant inner-package hits.
///
/// Example: in this repo, `services/agentd-py/` qualifies because `agentd/` has
/// `__init__.py` and `services/agentd-py/` itself does not. So `from agentd.X
/// import Y` resolves to `services/agentd-py/agentd/X.py` once this root is
/// known — the prior code only tried the workspace root and silently fell back
/// to an `external:module:agentd.X` edge.
fn discover_python_source_roots(workspace_root: &Path) -> Vec<PathBuf> {
    const IGNORED_DIR_NAMES: &[&str] = &[
        "node_modules", ".venv", "venv", ".git", "target",
        "__pycache__", "dist", "build", ".next",
        ".mypy_cache", ".pytest_cache", ".ruff_cache",
        ".agentd", ".ai-editor", ".tmp", ".worktrees",
    ];

    let mut roots: Vec<PathBuf> = Vec::new();
    let mut stack: Vec<PathBuf> = vec![workspace_root.to_path_buf()];

    while let Some(dir) = stack.pop() {
        let read_dir = match std::fs::read_dir(&dir) {
            Ok(rd) => rd,
            Err(_) => continue,
        };

        let mut has_package_child = false;
        let mut nonpackage_subdirs: Vec<PathBuf> = Vec::new();

        for entry in read_dir.flatten() {
            let path = entry.path();
            if !path.is_dir() {
                continue;
            }
            if let Some(name) = path.file_name().and_then(|n| n.to_str()) {
                if name.starts_with('.') || IGNORED_DIR_NAMES.contains(&name) {
                    continue;
                }
            }
            let is_package = path.join("__init__.py").exists();
            if is_package {
                has_package_child = true;
            } else {
                nonpackage_subdirs.push(path);
            }
        }

        let dir_is_package = dir.join("__init__.py").exists();
        if has_package_child && !dir_is_package {
            roots.push(dir.clone());
        }

        // Continue only through non-package subdirs; package interiors are
        // owned by `dir` (or a deeper non-package ancestor we've already seen).
        stack.extend(nonpackage_subdirs);
    }

    roots
}

fn py_offset_to_line(source: &str, offset: usize) -> u32 {
    let safe = offset.min(source.len());
    1 + source[..safe].bytes().filter(|&b| b == b'\n').count() as u32
}

/// Compute the byte offset of the callable identifier for an LSP position
/// query. Bare callees (`foo()`) want the start of the name; member-access
/// callees (`obj.method()`) want the start of `method`, not the receiver —
/// pyright resolves at the receiver's position to the receiver variable, not
/// the method. rustpython-parser doesn't expose the attr's range directly, so
/// we walk forward from the value's end through the `.` and any whitespace.
fn python_callable_offset(source: &str, func: &ast::Expr) -> usize {
    if let ast::Expr::Attribute(a) = func {
        let bytes = source.as_bytes();
        let mut offset = usize::from(a.value.range().end());
        // Skip the dot and any whitespace before the attribute identifier.
        while offset < bytes.len() {
            let b = bytes[offset];
            if b == b'.' || b == b' ' || b == b'\t' || b == b'\n' || b == b'\r' {
                offset += 1;
            } else {
                break;
            }
        }
        return offset;
    }
    usize::from(func.range().start())
}

/// Convert a Python source byte offset into LSP's 0-indexed `(line, character)`
/// pair. LSP technically specifies UTF-16 code-unit offsets for `character`,
/// but our source is overwhelmingly ASCII — the gap from byte to UTF-16 is
/// negligible in practice. Falls within the same "known limits" bucket as
/// dynamic dispatch: noted, accepted, not blocking.
fn py_offset_to_lsp_position(source: &str, offset: usize) -> (u32, u32) {
    let safe = offset.min(source.len());
    let preceding = &source[..safe];
    let line = preceding.bytes().filter(|&b| b == b'\n').count() as u32;
    let character = match preceding.rfind('\n') {
        Some(idx) => safe.saturating_sub(idx + 1),
        None => safe,
    };
    (line, character as u32)
}

fn extract_rust(
    file_path: &Path,
    source: &str,
    graph: &mut SymbolGraph,
    file_id: &str,
    node: Node<'_>,
    impl_context: Option<RustImplContext>,
) {
    match node.kind() {
        "use_declaration" => {
            if let Some(module_name) = extract_rust_use_module(&node_text(node, source).unwrap_or_default()) {
                let module_id = upsert_external_symbol(
                    graph,
                    file_path,
                    "module",
                    &module_name,
                    SymbolKind::Module,
                    node_line(node),
                );
                graph.add_edge(SymbolEdge {
                    from: file_id.to_string(),
                    to: module_id,
                    kind: EdgeKind::Imports,
                });
            }
        }
        "struct_item" | "enum_item" => {
            if let Some(name) = field_identifier(node, "name", source) {
                let class_id = upsert_named_symbol(
                    graph,
                    file_path,
                    file_id,
                    "class",
                    &name,
                    SymbolKind::Class,
                    node_line(node),
                );
                graph.add_edge(SymbolEdge {
                    from: file_id.to_string(),
                    to: class_id,
                    kind: EdgeKind::References,
                });
            }
        }
        "trait_item" => {
            if let Some(name) = field_identifier(node, "name", source) {
                let trait_id = upsert_named_symbol(
                    graph,
                    file_path,
                    file_id,
                    "interface",
                    &name,
                    SymbolKind::Interface,
                    node_line(node),
                );
                graph.add_edge(SymbolEdge {
                    from: file_id.to_string(),
                    to: trait_id,
                    kind: EdgeKind::References,
                });
            }
        }
        "impl_item" => {
            if let Some((owner_name, trait_name)) = parse_rust_impl_signature(&node_text(node, source).unwrap_or_default()) {
                let owner_id = upsert_named_symbol(
                    graph,
                    file_path,
                    file_id,
                    "class",
                    &owner_name,
                    SymbolKind::Class,
                    node_line(node),
                );
                graph.add_edge(SymbolEdge {
                    from: file_id.to_string(),
                    to: owner_id.clone(),
                    kind: EdgeKind::References,
                });

                if let Some(trait_name) = trait_name {
                    let trait_id = upsert_external_symbol(
                        graph,
                        file_path,
                        "interface",
                        &trait_name,
                        SymbolKind::Interface,
                        node_line(node),
                    );
                    graph.add_edge(SymbolEdge {
                        from: owner_id.clone(),
                        to: trait_id,
                        kind: EdgeKind::Inherits,
                    });
                }

                recurse_rust(
                    file_path,
                    source,
                    graph,
                    file_id,
                    node,
                    Some(RustImplContext { owner_id }),
                );
                return;
            }
        }
        "function_item" => {
            if let Some(name) = field_identifier(node, "name", source) {
                if let Some(context) = impl_context.as_ref() {
                    let method_id = upsert_member_symbol(
                        graph,
                        file_path,
                        file_id,
                        "method",
                        &context.owner_id,
                        &name,
                        SymbolKind::Method,
                        node_line(node),
                    );
                    graph.add_edge(SymbolEdge {
                        from: context.owner_id.clone(),
                        to: method_id,
                        kind: EdgeKind::References,
                    });
                } else {
                    let function_id = upsert_scoped_symbol(
                        graph,
                        file_path,
                        file_id,
                        "function",
                        &name,
                        SymbolKind::Function,
                        node_line(node),
                    );
                    graph.add_edge(SymbolEdge {
                        from: file_id.to_string(),
                        to: function_id,
                        kind: EdgeKind::References,
                    });
                }
            }
        }
        "let_declaration" => {
            if let Some(pattern) = node.child_by_field_name("pattern") {
                for name in extract_identifiers(&node_text(pattern, source).unwrap_or_default()) {
                    let variable_id = upsert_scoped_symbol(
                        graph,
                        file_path,
                        file_id,
                        "variable",
                        &name,
                        SymbolKind::Variable,
                        node_line(node),
                    );
                    graph.add_edge(SymbolEdge {
                        from: file_id.to_string(),
                        to: variable_id,
                        kind: EdgeKind::References,
                    });
                }
            }
        }
        "call_expression" => {
            if let Some(target_name) = extract_call_target(node, source) {
                let target_id = upsert_external_symbol(
                    graph,
                    file_path,
                    "call",
                    &target_name,
                    SymbolKind::Function,
                    node_line(node),
                );
                let from = impl_context
                    .as_ref()
                    .map(|value| value.owner_id.clone())
                    .unwrap_or_else(|| file_id.to_string());
                graph.add_edge(SymbolEdge {
                    from: from.clone(),
                    to: target_id.clone(),
                    kind: EdgeKind::Calls,
                });
                // Same shape as the TS handler: park a placeholder so the
                // resolver can rewrite to a workspace symbol once
                // rust-analyzer answers. `typescript_callable_position` works
                // for Rust call_expression too — tree-sitter-rust uses the
                // same `function` field for the callable.
                if let Some(point) = typescript_callable_position(node) {
                    graph.push_placeholder(PlaceholderEdge {
                        from_id: from,
                        external_to_id: target_id,
                        file_path: file_path.to_path_buf(),
                        line: point.row as u32,
                        character: point.column as u32,
                        edge_kind: EdgeKind::Calls,
                    });
                }
            }
        }
        _ => {}
    }

    recurse_rust(file_path, source, graph, file_id, node, impl_context);
}

fn recurse_rust(
    file_path: &Path,
    source: &str,
    graph: &mut SymbolGraph,
    file_id: &str,
    node: Node<'_>,
    impl_context: Option<RustImplContext>,
) {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        extract_rust(
            file_path,
            source,
            graph,
            file_id,
            child,
            impl_context.clone(),
        );
    }
}

fn upsert_file_node(graph: &mut SymbolGraph, file_path: &Path) -> String {
    let file_id = format!("file:{}", file_path.display());
    graph.upsert_node(SymbolNode {
        id: file_id.clone(),
        path: file_path.display().to_string(),
        name: file_path
            .file_name()
            .map(|v| v.to_string_lossy().to_string())
            .unwrap_or_else(|| "unknown".to_string()),
        kind: SymbolKind::File,
        line: 1,
    });
    file_id
}

fn upsert_named_symbol(
    graph: &mut SymbolGraph,
    file_path: &Path,
    file_id: &str,
    scope: &str,
    name: &str,
    kind: SymbolKind,
    line: u32,
) -> String {
    upsert_symbol(
        graph,
        file_path,
        &format!("{scope}:{file_id}:{name}"),
        name,
        kind,
        line,
    )
}

fn upsert_scoped_symbol(
    graph: &mut SymbolGraph,
    file_path: &Path,
    file_id: &str,
    scope: &str,
    name: &str,
    kind: SymbolKind,
    line: u32,
) -> String {
    upsert_symbol(
        graph,
        file_path,
        &format!("{scope}:{file_id}:{name}:{line}"),
        name,
        kind,
        line,
    )
}

fn upsert_member_symbol(
    graph: &mut SymbolGraph,
    file_path: &Path,
    file_id: &str,
    scope: &str,
    owner_id: &str,
    name: &str,
    kind: SymbolKind,
    line: u32,
) -> String {
    upsert_symbol(
        graph,
        file_path,
        &format!("{scope}:{file_id}:{owner_id}:{name}:{line}"),
        name,
        kind,
        line,
    )
}

fn upsert_external_symbol(
    graph: &mut SymbolGraph,
    file_path: &Path,
    scope: &str,
    name: &str,
    kind: SymbolKind,
    line: u32,
) -> String {
    upsert_symbol(
        graph,
        file_path,
        &format!("external:{scope}:{name}"),
        name,
        kind,
        line,
    )
}

fn upsert_symbol(
    graph: &mut SymbolGraph,
    file_path: &Path,
    symbol_id: &str,
    name: &str,
    kind: SymbolKind,
    line: u32,
) -> String {
    graph.upsert_node(SymbolNode {
        id: symbol_id.to_string(),
        path: file_path.display().to_string(),
        name: name.to_string(),
        kind,
        line,
    });
    symbol_id.to_string()
}

fn node_line(node: Node<'_>) -> u32 {
    (node.start_position().row + 1) as u32
}

fn node_text(node: Node<'_>, source: &str) -> Option<String> {
    node.utf8_text(source.as_bytes())
        .ok()
        .map(str::trim)
        .filter(|text| !text.is_empty())
        .map(ToOwned::to_owned)
}

fn field_identifier(node: Node<'_>, field: &str, source: &str) -> Option<String> {
    node.child_by_field_name(field)
        .and_then(|value| identifier_from_node(value, source))
}

fn identifier_from_node(node: Node<'_>, source: &str) -> Option<String> {
    let text = node_text(node, source)?;
    if let Some(identifier) = last_identifier(&text) {
        return Some(identifier);
    }
    first_identifier(&text)
}

fn extract_call_target(node: Node<'_>, source: &str) -> Option<String> {
    let function_node = node
        .child_by_field_name("function")
        .or_else(|| node.child_by_field_name("constructor"))
        .or_else(|| node.child(0))?;
    let text = node_text(function_node, source)?;
    last_identifier(&text)
}

/// Return the 0-indexed `(row, column)` of the callable identifier inside a
/// `call_expression` / `new_expression` node. For a member-access callable
/// (`obj.method(...)`) the position points at the property's start so
/// tsserver / pyright / rust-analyzer resolve the method itself; for a bare
/// identifier (`fn(...)`) it points at the identifier's start. Returns `None`
/// when neither shape applies (e.g., a computed-call surface we don't model).
fn typescript_callable_position(call_node: Node<'_>) -> Option<tree_sitter::Point> {
    let function_node = call_node
        .child_by_field_name("function")
        .or_else(|| call_node.child_by_field_name("constructor"))
        .or_else(|| call_node.child(0))?;
    if let Some(property) = function_node.child_by_field_name("property") {
        return Some(property.start_position());
    }
    Some(function_node.start_position())
}

fn extract_variable_declarators(node: Node<'_>, source: &str) -> Vec<String> {
    let mut names: Vec<String> = Vec::new();
    let mut queue: Vec<Node<'_>> = vec![node];

    while let Some(current) = queue.pop() {
        if current.kind() == "variable_declarator" {
            if let Some(name_node) = current.child_by_field_name("name") {
                names.extend(extract_identifiers(&node_text(name_node, source).unwrap_or_default()));
            }
        }

        let mut cursor = current.walk();
        for child in current.children(&mut cursor) {
            queue.push(child);
        }
    }

    names.sort();
    names.dedup();
    names
}

fn extract_quoted_fragment(text: &str) -> Option<String> {
    for quote in ['\'', '"'] {
        if let Some((_, tail)) = text.split_once(quote) {
            if let Some((value, _)) = tail.split_once(quote) {
                let trimmed = value.trim();
                if !trimmed.is_empty() {
                    return Some(trimmed.to_string());
                }
            }
        }
    }
    None
}

fn extract_extends_targets(text: &str) -> Vec<String> {
    let compact = text.replace('\n', " ");
    let Some(index) = compact.find("extends") else {
        return Vec::new();
    };
    let mut tail = compact[index + "extends".len()..].trim().to_string();
    for marker in ["implements", "{", "("] {
        if let Some(position) = tail.find(marker) {
            tail = tail[..position].trim().to_string();
        }
    }

    let mut targets = Vec::new();
    for part in tail.split(',') {
        if let Some(identifier) = first_identifier(part.trim()) {
            targets.push(identifier);
        }
    }
    targets.sort();
    targets.dedup();
    targets
}


fn extract_rust_use_module(text: &str) -> Option<String> {
    let compact = text.replace('\n', " ");
    let rest = compact.trim().strip_prefix("use ")?;
    let module = rest.trim_end_matches(';').trim();
    if module.is_empty() {
        return None;
    }
    Some(module.to_string())
}

fn parse_rust_impl_signature(text: &str) -> Option<(String, Option<String>)> {
    let compact = text.replace('\n', " ");
    let rest = compact.trim().strip_prefix("impl ")?;
    let body = rest.split('{').next().unwrap_or(rest).trim();

    if let Some((trait_part, type_part)) = body.split_once(" for ") {
        let owner_name = last_identifier(type_part.trim())?;
        let trait_name = last_identifier(trait_part.trim());
        return Some((owner_name, trait_name));
    }

    let owner_name = last_identifier(body)?;
    Some((owner_name, None))
}

fn extract_identifiers(text: &str) -> Vec<String> {
    let mut identifiers: Vec<String> = Vec::new();
    let mut token = String::new();

    for ch in text.chars() {
        if ch.is_ascii_alphanumeric() || ch == '_' {
            token.push(ch);
            continue;
        }

        if !token.is_empty() {
            if is_valid_identifier_token(&token) {
                identifiers.push(token.clone());
            }
            token.clear();
        }
    }

    if !token.is_empty() && is_valid_identifier_token(&token) {
        identifiers.push(token);
    }

    identifiers.sort();
    identifiers.dedup();
    identifiers
}

fn first_identifier(text: &str) -> Option<String> {
    extract_identifiers(text).into_iter().next()
}

fn last_identifier(text: &str) -> Option<String> {
    extract_identifiers(text).into_iter().last()
}

fn is_valid_identifier_token(token: &str) -> bool {
    if token.is_empty() {
        return false;
    }

    let Some(first_char) = token.chars().next() else {
        return false;
    };
    if !first_char.is_ascii_alphabetic() && first_char != '_' {
        return false;
    }

    !matches!(
        token,
        "class"
            | "interface"
            | "function"
            | "fn"
            | "def"
            | "trait"
            | "impl"
            | "let"
            | "const"
            | "var"
            | "extends"
            | "import"
            | "from"
            | "as"
            | "return"
            | "new"
            | "self"
            | "crate"
            | "super"
            | "pub"
            | "mut"
    )
}
