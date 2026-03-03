use anyhow::{anyhow, Context, Result};
use std::path::Path;
use tree_sitter::{Node, Parser};

use crate::graph::{EdgeKind, SymbolEdge, SymbolGraph, SymbolKind, SymbolNode};

pub trait LanguageParser: Send + Sync {
    fn language_name(&self) -> &'static str;
    fn parse_file(&self, file_path: &Path, source: &str, graph: &mut SymbolGraph) -> Result<()>;
}

#[derive(Default)]
pub struct TreeSitterParser;

#[derive(Clone, Copy)]
enum ParserLanguage {
    TypeScript,
    Tsx,
    Python,
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
            "py" => Some(Self::Python),
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
            Self::Python => parser
                .set_language(&tree_sitter_python::LANGUAGE.into())
                .context("failed to set Python grammar"),
            Self::Rust => parser
                .set_language(&tree_sitter_rust::LANGUAGE.into())
                .context("failed to set Rust grammar"),
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            Self::TypeScript => "typescript",
            Self::Tsx => "tsx",
            Self::Python => "python",
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
            ParserLanguage::Python => extract_python(file_path, source, graph, &file_id, root, None),
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
                    from,
                    to: target_id,
                    kind: EdgeKind::Calls,
                });
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

fn extract_python(
    file_path: &Path,
    source: &str,
    graph: &mut SymbolGraph,
    file_id: &str,
    node: Node<'_>,
    current_class: Option<String>,
) {
    match node.kind() {
        "import_statement" => {
            for module_name in extract_python_import_modules(&node_text(node, source).unwrap_or_default()) {
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
        "import_from_statement" => {
            if let Some(module_name) = extract_python_from_module(&node_text(node, source).unwrap_or_default()) {
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
        "class_definition" => {
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

                for parent_name in extract_python_base_classes(&node_text(node, source).unwrap_or_default()) {
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

                recurse_python(file_path, source, graph, file_id, node, Some(class_id));
                return;
            }
        }
        "function_definition" => {
            if let Some(name) = field_identifier(node, "name", source) {
                if let Some(class_id) = current_class.as_ref() {
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
        "assignment" => {
            if let Some(left) = node.child_by_field_name("left") {
                for name in extract_identifiers(&node_text(left, source).unwrap_or_default()) {
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
        "call" => {
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
                    from,
                    to: target_id,
                    kind: EdgeKind::Calls,
                });
            }
        }
        _ => {}
    }

    recurse_python(file_path, source, graph, file_id, node, current_class);
}

fn recurse_python(
    file_path: &Path,
    source: &str,
    graph: &mut SymbolGraph,
    file_id: &str,
    node: Node<'_>,
    current_class: Option<String>,
) {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        extract_python(file_path, source, graph, file_id, child, current_class.clone());
    }
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
                    from,
                    to: target_id,
                    kind: EdgeKind::Calls,
                });
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

fn extract_python_import_modules(text: &str) -> Vec<String> {
    let Some(rest) = text.trim().strip_prefix("import ") else {
        return Vec::new();
    };

    let mut modules: Vec<String> = Vec::new();
    for part in rest.split(',') {
        let token = part
            .split_whitespace()
            .next()
            .unwrap_or_default()
            .trim()
            .trim_end_matches(';');
        if !token.is_empty() {
            modules.push(token.to_string());
        }
    }
    modules.sort();
    modules.dedup();
    modules
}

fn extract_python_from_module(text: &str) -> Option<String> {
    let compact = text.replace('\n', " ");
    let rest = compact.trim().strip_prefix("from ")?;
    let module = rest.split(" import ").next()?.trim();
    (!module.is_empty()).then_some(module.to_string())
}

fn extract_python_base_classes(text: &str) -> Vec<String> {
    let compact = text.replace('\n', " ");
    let start = compact.find('(');
    let end = compact.rfind(')');
    let (Some(start), Some(end)) = (start, end) else {
        return Vec::new();
    };
    if end <= start + 1 {
        return Vec::new();
    }

    let mut classes = Vec::new();
    for value in compact[start + 1..end].split(',') {
        if let Some(name) = first_identifier(value.trim()) {
            classes.push(name);
        }
    }
    classes.sort();
    classes.dedup();
    classes
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
