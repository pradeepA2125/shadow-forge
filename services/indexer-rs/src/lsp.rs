use anyhow::{anyhow, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::thread;
use std::time::{Duration, Instant};

use crate::config::IndexerConfig;

const MAX_RESTART_ATTEMPTS: u32 = 3;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum LspLanguage {
    TypeScript,
    Python,
    Rust,
}

impl LspLanguage {
    pub fn language_for_path(path: &Path) -> Option<Self> {
        match path.extension().and_then(|ext| ext.to_str()) {
            Some("ts" | "tsx") => Some(Self::TypeScript),
            Some("py") => Some(Self::Python),
            Some("rs") => Some(Self::Rust),
            _ => None,
        }
    }

    fn language_id(self) -> &'static str {
        match self {
            Self::TypeScript => "typescript",
            Self::Python => "python",
            Self::Rust => "rust",
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            Self::TypeScript => "typescript",
            Self::Python => "python",
            Self::Rust => "rust",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum SessionState {
    Starting,
    Ready,
    Restarting,
    Failed,
}

/// Workspace-absolute pointer at a symbol returned from an LSP location-style
/// query (`textDocument/definition`, `textDocument/implementation`,
/// `textDocument/references`). Always carries the path as a `PathBuf` and the
/// position in LSP 0-indexed semantics (line + UTF-16 character).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LspLocation {
    pub path: PathBuf,
    pub line: u32,
    pub character: u32,
}

/// Cached result of one LSP resolution query, indexed on `LspSession` so we
/// don't pay the JSON-RPC roundtrip twice for the same `(file, position,
/// method)` triple. Cleared per file when its `file_versions` entry bumps.
#[derive(Debug, Clone)]
enum CachedResolution {
    Definition(Option<LspLocation>),
    Implementations(Vec<LspLocation>),
    References(Vec<LspLocation>),
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct LspDiagnostic {
    pub severity: String,
    pub source: Option<String>,
    pub code: Option<String>,
    pub message: String,
    pub file: String,
    pub line: u32,
    pub column: u32,
    pub language: String,
}

#[derive(Debug)]
struct SessionSlot {
    command: String,
    session: Option<LspSession>,
    state: SessionState,
    restart_count: u32,
    disabled_reason: Option<String>,
}

impl SessionSlot {
    fn new(command: String) -> Self {
        Self {
            command,
            session: None,
            state: SessionState::Starting,
            restart_count: 0,
            disabled_reason: None,
        }
    }

    fn can_restart(&self) -> bool {
        self.restart_count < MAX_RESTART_ATTEMPTS
    }
}

#[derive(Debug, Clone)]
enum SessionOperation {
    Upsert { path: PathBuf, content: String },
    Close { path: PathBuf },
    Flush,
}

#[derive(Debug)]
struct LspSession {
    language: LspLanguage,
    workspace_root: PathBuf,
    child: Child,
    writer: ChildStdin,
    messages: Receiver<Value>,
    next_request_id: u64,
    startup_timeout: Duration,
    request_timeout: Duration,
    file_versions: HashMap<PathBuf, i32>,
    diagnostics_by_uri: HashMap<String, Vec<LspDiagnostic>>,
    /// Resolution-query cache. Key shape: `(file_path, line, character,
    /// method)`. Invalidated for a given `file_path` whenever its
    /// `file_versions` entry bumps via `open_file` / `change_file` / `close_file`.
    resolution_cache: HashMap<(PathBuf, u32, u32, &'static str), CachedResolution>,
}

impl Drop for LspSession {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

impl LspSession {
    fn start(
        language: LspLanguage,
        workspace_root: PathBuf,
        command_text: &str,
        startup_timeout: Duration,
        request_timeout: Duration,
    ) -> Result<Self> {
        let (program, args) = parse_command(command_text)?;
        let mut child = Command::new(&program)
            .args(args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .with_context(|| {
                format!(
                    "failed to spawn language server '{}' for {}",
                    command_text,
                    language.as_str()
                )
            })?;

        let writer = child
            .stdin
            .take()
            .ok_or_else(|| anyhow!("language server stdin unavailable"))?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| anyhow!("language server stdout unavailable"))?;
        let messages = spawn_reader_thread(stdout);

        let mut session = Self {
            language,
            workspace_root,
            child,
            writer,
            messages,
            next_request_id: 1,
            startup_timeout,
            request_timeout,
            file_versions: HashMap::new(),
            diagnostics_by_uri: HashMap::new(),
            resolution_cache: HashMap::new(),
        };

        session.initialize()?;
        Ok(session)
    }

    fn initialize(&mut self) -> Result<()> {
        let workspace_uri = path_to_file_uri(&self.workspace_root);
        let name = self
            .workspace_root
            .file_name()
            .map(|part| part.to_string_lossy().to_string())
            .unwrap_or_else(|| "workspace".to_string());

        let mut params = json!({
            "processId": std::process::id(),
            "rootUri": workspace_uri,
            "capabilities": {},
            "workspaceFolders": [{
                "uri": workspace_uri,
                "name": name
            }]
        });

        // Per-language initializationOptions. Today only rust-analyzer needs
        // them — without `linkedProjects`, it can't find Cargo.toml in a
        // monorepo whose root has no top-level Cargo manifest, so it answers
        // every `definition`/`implementation` with `null` and our resolver
        // sees a flood of "None" returns for every Rust call site.
        if self.language == LspLanguage::Rust {
            let cargo_manifests = discover_cargo_manifests(&self.workspace_root);
            if !cargo_manifests.is_empty() {
                let manifests: Vec<String> = cargo_manifests
                    .iter()
                    .map(|p| p.display().to_string())
                    .collect();
                params["initializationOptions"] = json!({
                    "linkedProjects": manifests,
                    "checkOnSave": false,   // skip cargo check on bootstrap — saves ~30s
                    "cargo": { "buildScripts": { "enable": false } },
                    "procMacro": { "enable": false },
                });
            }
        }

        let _ = self.request_with_timeout("initialize", params, self.startup_timeout)?;
        self.notify("initialized", json!({}))?;
        self.drain_notifications(Duration::from_millis(20))?;
        Ok(())
    }

    fn apply(&mut self, op: &SessionOperation) -> Result<()> {
        match op {
            SessionOperation::Upsert { path, content } => self.upsert_file(path, content),
            SessionOperation::Close { path } => self.close_file(path),
            SessionOperation::Flush => self.drain_notifications(Duration::from_millis(10)),
        }
    }

    fn diagnostics_snapshot(&self) -> Vec<LspDiagnostic> {
        self.diagnostics_by_uri
            .values()
            .flat_map(|diagnostics| diagnostics.iter().cloned())
            .collect()
    }

    fn upsert_file(&mut self, file_path: &Path, content: &str) -> Result<()> {
        if self.file_versions.contains_key(file_path) {
            self.change_file(file_path, content)
        } else {
            self.open_file(file_path, content)
        }
    }

    fn open_file(&mut self, file_path: &Path, content: &str) -> Result<()> {
        let version = 1;
        self.file_versions.insert(file_path.to_path_buf(), version);
        self.invalidate_cache_for_file(file_path);

        self.notify(
            "textDocument/didOpen",
            json!({
                "textDocument": {
                    "uri": path_to_file_uri(file_path),
                    "languageId": self.language.language_id(),
                    "version": version,
                    "text": content
                }
            }),
        )?;
        self.drain_notifications(Duration::from_millis(25))
    }

    fn change_file(&mut self, file_path: &Path, content: &str) -> Result<()> {
        let next_version = self.file_versions.get(file_path).copied().unwrap_or(0) + 1;
        self.file_versions.insert(file_path.to_path_buf(), next_version);
        self.invalidate_cache_for_file(file_path);

        self.notify(
            "textDocument/didChange",
            json!({
                "textDocument": {
                    "uri": path_to_file_uri(file_path),
                    "version": next_version
                },
                "contentChanges": [{
                    "text": content
                }]
            }),
        )?;
        self.drain_notifications(Duration::from_millis(25))
    }

    fn close_file(&mut self, file_path: &Path) -> Result<()> {
        let uri = path_to_file_uri(file_path);
        self.file_versions.remove(file_path);
        self.diagnostics_by_uri.remove(&uri);
        self.invalidate_cache_for_file(file_path);

        self.notify(
            "textDocument/didClose",
            json!({
                "textDocument": { "uri": uri }
            }),
        )?;
        self.drain_notifications(Duration::from_millis(15))
    }

    fn request_with_timeout(
        &mut self,
        method: &str,
        params: Value,
        timeout_duration: Duration,
    ) -> Result<Value> {
        let request_id = self.next_request_id;
        self.next_request_id += 1;

        self.write_message(&json!({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params
        }))?;

        let deadline = Instant::now() + timeout_duration;
        loop {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Err(anyhow!(
                    "timed out waiting for '{}' response from {} server",
                    method,
                    self.language.as_str()
                ));
            }

            let payload = match self.messages.recv_timeout(remaining) {
                Ok(payload) => payload,
                Err(RecvTimeoutError::Timeout) => {
                    return Err(anyhow!(
                        "timed out while waiting for '{}' response from {} server",
                        method,
                        self.language.as_str()
                    ));
                }
                Err(RecvTimeoutError::Disconnected) => {
                    return Err(anyhow!(
                        "{} language server exited while waiting for '{}'",
                        self.language.as_str(),
                        method
                    ));
                }
            };

            if let Some(id) = response_id(&payload) {
                if id != request_id {
                    continue;
                }

                if let Some(result) = payload.get("result") {
                    return Ok(result.clone());
                }

                if let Some(error) = payload.get("error") {
                    return Err(anyhow!(
                        "{} request '{}' failed: {}",
                        self.language.as_str(),
                        method,
                        error
                    ));
                }

                return Err(anyhow!(
                    "{} request '{}' received malformed response",
                    self.language.as_str(),
                    method
                ));
            }

            if payload.get("method").is_some() {
                self.handle_notification(&payload)?;
            }
        }
    }

    fn notify(&mut self, method: &str, params: Value) -> Result<()> {
        self.write_message(&json!({
            "jsonrpc": "2.0",
            "method": method,
            "params": params
        }))
    }

    fn drain_notifications(&mut self, idle_timeout: Duration) -> Result<()> {
        let timeout_window = idle_timeout.min(self.request_timeout);
        loop {
            let payload = match self.messages.recv_timeout(timeout_window) {
                Ok(payload) => payload,
                Err(RecvTimeoutError::Timeout) => break,
                Err(RecvTimeoutError::Disconnected) => {
                    return Err(anyhow!(
                        "{} language server exited while draining notifications",
                        self.language.as_str()
                    ));
                }
            };

            if payload.get("method").is_some() {
                self.handle_notification(&payload)?;
            }
        }
        Ok(())
    }

    fn handle_notification(&mut self, payload: &Value) -> Result<()> {
        let method = payload
            .get("method")
            .and_then(Value::as_str)
            .unwrap_or_default();
        if method != "textDocument/publishDiagnostics" {
            return Ok(());
        }

        let params = payload
            .get("params")
            .ok_or_else(|| anyhow!("publishDiagnostics notification is missing params"))?;
        let uri = params
            .get("uri")
            .and_then(Value::as_str)
            .ok_or_else(|| anyhow!("publishDiagnostics notification is missing uri"))?;

        let file = file_uri_to_path(uri);
        let diagnostics = params
            .get("diagnostics")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();

        let parsed: Vec<LspDiagnostic> = diagnostics
            .iter()
            .map(|diagnostic| parse_lsp_diagnostic(diagnostic, &file, self.language))
            .collect();

        if parsed.is_empty() {
            self.diagnostics_by_uri.remove(uri);
        } else {
            self.diagnostics_by_uri.insert(uri.to_string(), parsed);
        }

        Ok(())
    }

    fn write_message(&mut self, payload: &Value) -> Result<()> {
        let frame = encode_message(payload)?;
        self.writer.write_all(&frame)?;
        self.writer.flush()?;
        Ok(())
    }

    fn invalidate_cache_for_file(&mut self, file_path: &Path) {
        // Cache keys are `(file_path, line, character, method)`; drop every
        // entry whose first component matches the file we just bumped.
        self.resolution_cache.retain(|(p, _, _, _), _| p != file_path);
    }

    /// Ensure the file is open in the LSP session before issuing a position-
    /// based query. Resolves the "we never told the server about this file"
    /// edge case where pyright / tsserver would otherwise reply with an empty
    /// result. Reads the current on-disk content; relies on `upsert_file` to
    /// no-op when the file is already open at a matching version.
    fn ensure_file_open(&mut self, file_path: &Path) -> Result<()> {
        if self.file_versions.contains_key(file_path) {
            return Ok(());
        }
        let content = match std::fs::read_to_string(file_path) {
            Ok(text) => text,
            Err(_) => return Ok(()),
        };
        self.open_file(file_path, &content)
    }

    fn request_definition(
        &mut self,
        file_path: &Path,
        line: u32,
        character: u32,
    ) -> Result<Option<LspLocation>> {
        let key = (
            file_path.to_path_buf(),
            line,
            character,
            "textDocument/definition",
        );
        if let Some(CachedResolution::Definition(cached)) = self.resolution_cache.get(&key) {
            return Ok(cached.clone());
        }

        self.ensure_file_open(file_path)?;
        let params = json!({
            "textDocument": { "uri": path_to_file_uri(file_path) },
            "position": { "line": line, "character": character }
        });
        let request_timeout = self.request_timeout;
        let value =
            match self.request_with_timeout("textDocument/definition", params, request_timeout) {
                Ok(v) => v,
                Err(err) => {
                    tracing::debug!(
                        path = %file_path.display(),
                        line = line,
                        character = character,
                        error = %err,
                        "definition request failed"
                    );
                    return Ok(None);
                }
            };

        let locations = parse_lsp_locations(&value);
        let resolved = locations.into_iter().next();
        self.resolution_cache
            .insert(key, CachedResolution::Definition(resolved.clone()));
        Ok(resolved)
    }

    fn request_implementations(
        &mut self,
        file_path: &Path,
        line: u32,
        character: u32,
    ) -> Result<Vec<LspLocation>> {
        let key = (
            file_path.to_path_buf(),
            line,
            character,
            "textDocument/implementation",
        );
        if let Some(CachedResolution::Implementations(cached)) = self.resolution_cache.get(&key) {
            return Ok(cached.clone());
        }

        self.ensure_file_open(file_path)?;
        let params = json!({
            "textDocument": { "uri": path_to_file_uri(file_path) },
            "position": { "line": line, "character": character }
        });
        let request_timeout = self.request_timeout;
        let value = match self.request_with_timeout(
            "textDocument/implementation",
            params,
            request_timeout,
        ) {
            Ok(v) => v,
            Err(err) => {
                tracing::debug!(
                    path = %file_path.display(),
                    line = line,
                    character = character,
                    error = %err,
                    "implementation request failed"
                );
                return Ok(Vec::new());
            }
        };
        let locations = parse_lsp_locations(&value);
        self.resolution_cache
            .insert(key, CachedResolution::Implementations(locations.clone()));
        Ok(locations)
    }

    fn request_references(
        &mut self,
        file_path: &Path,
        line: u32,
        character: u32,
        include_declaration: bool,
    ) -> Result<Vec<LspLocation>> {
        let key = (
            file_path.to_path_buf(),
            line,
            character,
            "textDocument/references",
        );
        if let Some(CachedResolution::References(cached)) = self.resolution_cache.get(&key) {
            return Ok(cached.clone());
        }

        self.ensure_file_open(file_path)?;
        let params = json!({
            "textDocument": { "uri": path_to_file_uri(file_path) },
            "position": { "line": line, "character": character },
            "context": { "includeDeclaration": include_declaration }
        });
        let request_timeout = self.request_timeout;
        let value =
            match self.request_with_timeout("textDocument/references", params, request_timeout) {
                Ok(v) => v,
                Err(err) => {
                    tracing::debug!(
                        path = %file_path.display(),
                        line = line,
                        character = character,
                        error = %err,
                        "references request failed"
                    );
                    return Ok(Vec::new());
                }
            };
        let locations = parse_lsp_locations(&value);
        self.resolution_cache
            .insert(key, CachedResolution::References(locations.clone()));
        Ok(locations)
    }
}

/// Decode an LSP location response into a flat list of `LspLocation`. Handles
/// the three shapes the spec allows for `definition` / `implementation` /
/// `references` responses: a single `Location`, an array of `Location`, an
/// array of `LocationLink`, or `null`. Unknown shapes return an empty vec so
/// the caller treats it as an unresolved call site.
fn parse_lsp_locations(value: &Value) -> Vec<LspLocation> {
    if value.is_null() {
        return Vec::new();
    }

    let mut out: Vec<LspLocation> = Vec::new();

    let mut consider = |item: &Value| {
        // `Location` shape: {uri, range: {start: {line, character}, ...}}
        if let (Some(uri), Some(range)) = (
            item.get("uri").and_then(Value::as_str),
            item.get("range").and_then(|r| r.get("start")),
        ) {
            if let Some(loc) = location_from(uri, range) {
                out.push(loc);
                return;
            }
        }
        // `LocationLink` shape: {targetUri, targetRange: {start: ...}, targetSelectionRange: {...}}
        if let (Some(target_uri), Some(range)) = (
            item.get("targetUri").and_then(Value::as_str),
            item.get("targetSelectionRange")
                .or_else(|| item.get("targetRange"))
                .and_then(|r| r.get("start")),
        ) {
            if let Some(loc) = location_from(target_uri, range) {
                out.push(loc);
            }
        }
    };

    if let Some(arr) = value.as_array() {
        for item in arr {
            consider(item);
        }
    } else {
        consider(value);
    }

    out
}

fn location_from(uri: &str, start: &Value) -> Option<LspLocation> {
    let line = start.get("line").and_then(Value::as_u64)? as u32;
    let character = start.get("character").and_then(Value::as_u64)? as u32;
    Some(LspLocation {
        path: PathBuf::from(file_uri_to_path(uri)),
        line,
        character,
    })
}

pub struct LspAdapter {
    enabled: bool,
    workspace_root: PathBuf,
    startup_timeout: Duration,
    request_timeout: Duration,
    sessions: HashMap<LspLanguage, SessionSlot>,
    manager_warnings: Vec<LspDiagnostic>,
}

impl LspAdapter {
    pub fn new(config: &IndexerConfig) -> Self {
        let mut sessions = HashMap::new();
        sessions.insert(
            LspLanguage::TypeScript,
            SessionSlot::new(config.lsp_ts_cmd.clone()),
        );
        sessions.insert(LspLanguage::Python, SessionSlot::new(config.lsp_py_cmd.clone()));
        sessions.insert(LspLanguage::Rust, SessionSlot::new(config.lsp_rs_cmd.clone()));

        Self {
            enabled: config.lsp_enabled,
            workspace_root: config.workspace_root.clone(),
            startup_timeout: Duration::from_millis(config.lsp_startup_timeout_ms),
            request_timeout: Duration::from_millis(config.lsp_request_timeout_ms),
            sessions,
            manager_warnings: Vec::new(),
        }
    }

    pub fn language_for_path(path: &Path) -> Option<LspLanguage> {
        LspLanguage::language_for_path(path)
    }

    pub async fn open_workspace_files(&mut self, files: &[PathBuf]) -> Result<()> {
        for file_path in files {
            if LspLanguage::language_for_path(file_path).is_none() {
                continue;
            }

            match std::fs::read_to_string(file_path) {
                Ok(content) => self.upsert_file(file_path, content).await?,
                Err(error) => {
                    tracing::warn!(
                        path = %file_path.display(),
                        error = %error,
                        "unable to read file for LSP bootstrap"
                    );
                }
            }
        }
        Ok(())
    }

    pub async fn upsert_file(&mut self, file_path: &Path, content: String) -> Result<()> {
        let Some(language) = LspLanguage::language_for_path(file_path) else {
            return Ok(());
        };
        self.apply_operation(
            language,
            SessionOperation::Upsert {
                path: file_path.to_path_buf(),
                content,
            },
        )
    }

    pub async fn close_file(&mut self, file_path: &Path) -> Result<()> {
        let Some(language) = LspLanguage::language_for_path(file_path) else {
            return Ok(());
        };
        self.apply_operation(
            language,
            SessionOperation::Close {
                path: file_path.to_path_buf(),
            },
        )
    }

    /// Resolve a call site's static declaration via LSP. Returns `None` when:
    /// the file is in an unsupported language, the LSP is disabled, the
    /// session can't be started, or the LSP itself returned no usable
    /// location. Never errors on the failure path — callers treat the empty
    /// result as "leave the placeholder's `external:call:<name>` fallback
    /// edge in place."
    pub fn resolve_definition(
        &mut self,
        file_path: &Path,
        line: u32,
        character: u32,
    ) -> Option<LspLocation> {
        let session = self.session_for(file_path)?;
        session
            .request_definition(file_path, line, character)
            .ok()
            .flatten()
    }

    /// Resolve every workspace symbol that implements/overrides the symbol at
    /// `(file_path, line, character)`. Used to emit `Implements` edges for
    /// Protocol/ABC/interface dispatch. Returns the empty vec on any failure
    /// or when the target is a concrete leaf with no overrides.
    pub fn resolve_implementations(
        &mut self,
        file_path: &Path,
        line: u32,
        character: u32,
    ) -> Vec<LspLocation> {
        let Some(session) = self.session_for(file_path) else {
            return Vec::new();
        };
        session
            .request_implementations(file_path, line, character)
            .unwrap_or_default()
    }

    /// Resolve the call sites that reference the symbol at the given
    /// position. Reserved for a future "find callers" surface — declared
    /// alongside definition/implementation so the cache primitive covers it.
    pub fn resolve_references(
        &mut self,
        file_path: &Path,
        line: u32,
        character: u32,
        include_declaration: bool,
    ) -> Vec<LspLocation> {
        let Some(session) = self.session_for(file_path) else {
            return Vec::new();
        };
        session
            .request_references(file_path, line, character, include_declaration)
            .unwrap_or_default()
    }

    /// Borrow the LSP session for the language matching `file_path`. Returns
    /// `None` when the adapter is disabled, the language is unsupported, the
    /// session can't be started, or a prior failure marked the session as
    /// disabled. The resolution wrappers above are all built on top of this.
    fn session_for(&mut self, file_path: &Path) -> Option<&mut LspSession> {
        if !self.enabled {
            return None;
        }
        let language = LspLanguage::language_for_path(file_path)?;
        if self.ensure_session(language).is_err() {
            return None;
        }
        let slot = self.sessions.get_mut(&language)?;
        if slot.disabled_reason.is_some() {
            return None;
        }
        slot.session.as_mut()
    }

    pub async fn collect_diagnostics(&mut self, _workspace_root: &Path) -> Result<Vec<LspDiagnostic>> {
        self.flush_notifications()?;
        let mut diagnostics: Vec<LspDiagnostic> = self.manager_warnings.clone();
        for slot in self.sessions.values() {
            if let Some(session) = &slot.session {
                diagnostics.extend(session.diagnostics_snapshot());
            }
        }
        diagnostics.sort_by(|a, b| {
            a.file
                .cmp(&b.file)
                .then(a.line.cmp(&b.line))
                .then(a.column.cmp(&b.column))
        });
        Ok(diagnostics)
    }

    fn flush_notifications(&mut self) -> Result<()> {
        let languages = [LspLanguage::TypeScript, LspLanguage::Python, LspLanguage::Rust];
        for language in languages {
            self.apply_operation(language, SessionOperation::Flush)?;
        }
        Ok(())
    }

    fn apply_operation(&mut self, language: LspLanguage, operation: SessionOperation) -> Result<()> {
        if !self.enabled {
            return Ok(());
        }

        self.ensure_session(language)?;

        let mut slot = self
            .sessions
            .remove(&language)
            .ok_or_else(|| anyhow!("session slot not found for {}", language.as_str()))?;

        if slot.disabled_reason.is_some() {
            self.sessions.insert(language, slot);
            return Ok(());
        }

        let first_attempt = if let Some(session) = slot.session.as_mut() {
            session.apply(&operation)
        } else {
            Err(anyhow!("session unavailable"))
        };

        if let Err(error) = first_attempt {
            tracing::warn!(
                language = %language.as_str(),
                error = %error,
                "LSP operation failed; attempting restart"
            );
            slot.state = SessionState::Restarting;
            slot.session = None;
            slot.restart_count += 1;
            self.push_manager_warning(
                language,
                "session_operation_failed",
                format!("LSP operation failed and restart was scheduled: {error}"),
            );

            if slot.can_restart() {
                let backoff_ms = 100_u64.saturating_mul(slot.restart_count as u64);
                if backoff_ms > 0 {
                    thread::sleep(Duration::from_millis(backoff_ms));
                }
                match LspSession::start(
                    language,
                    self.workspace_root.clone(),
                    &slot.command,
                    self.startup_timeout,
                    self.request_timeout,
                ) {
                    Ok(mut restarted) => {
                        slot.state = SessionState::Ready;
                        if let Err(retry_error) = restarted.apply(&operation) {
                            let reason = format!("LSP retry failed: {retry_error}");
                            tracing::warn!(
                                language = %language.as_str(),
                                reason = %reason,
                                "disabling language session"
                            );
                            self.push_manager_warning(
                                language,
                                "session_retry_failed",
                                reason.clone(),
                            );
                            slot.state = SessionState::Failed;
                            slot.disabled_reason = Some(reason);
                        } else {
                            slot.session = Some(restarted);
                        }
                    }
                    Err(restart_error) => {
                        let reason = format!("unable to restart language server: {restart_error}");
                        tracing::warn!(
                            language = %language.as_str(),
                            reason = %reason,
                            "disabling language session"
                        );
                        self.push_manager_warning(
                            language,
                            "session_restart_failed",
                            reason.clone(),
                        );
                        slot.state = SessionState::Failed;
                        slot.disabled_reason = Some(reason);
                    }
                }
            } else {
                let reason = format!(
                    "restart budget exhausted for {} language server",
                    language.as_str()
                );
                tracing::warn!(
                    language = %language.as_str(),
                    reason = %reason,
                    "disabling language session"
                );
                self.push_manager_warning(
                    language,
                    "session_restart_budget_exhausted",
                    reason.clone(),
                );
                slot.state = SessionState::Failed;
                slot.disabled_reason = Some(reason);
            }
        }

        self.sessions.insert(language, slot);
        Ok(())
    }

    fn ensure_session(&mut self, language: LspLanguage) -> Result<()> {
        let mut slot = self
            .sessions
            .remove(&language)
            .ok_or_else(|| anyhow!("session slot not found for {}", language.as_str()))?;

        if slot.disabled_reason.is_some() || slot.session.is_some() {
            self.sessions.insert(language, slot);
            return Ok(());
        }

        slot.state = SessionState::Starting;
        match LspSession::start(
            language,
            self.workspace_root.clone(),
            &slot.command,
            self.startup_timeout,
            self.request_timeout,
        ) {
            Ok(session) => {
                slot.state = SessionState::Ready;
                slot.session = Some(session);
                tracing::info!(language = %language.as_str(), "LSP session started");
            }
            Err(error) => {
                let reason = format!("unable to start language server: {error}");
                slot.state = SessionState::Failed;
                slot.disabled_reason = Some(reason.clone());
                self.push_manager_warning(
                    language,
                    "session_start_failed",
                    reason.clone(),
                );
                tracing::warn!(
                    language = %language.as_str(),
                    reason = %reason,
                    "disabling language session"
                );
            }
        }

        self.sessions.insert(language, slot);
        Ok(())
    }

    fn push_manager_warning(&mut self, language: LspLanguage, code: &str, message: String) {
        self.manager_warnings.push(LspDiagnostic {
            severity: "warning".to_string(),
            source: Some("lsp-manager".to_string()),
            code: Some(code.to_string()),
            message,
            file: self.workspace_root.display().to_string(),
            line: 1,
            column: 1,
            language: language.as_str().to_string(),
        });
    }
}

fn parse_lsp_diagnostic(payload: &Value, file: &str, language: LspLanguage) -> LspDiagnostic {
    let severity = match payload.get("severity").and_then(Value::as_u64) {
        Some(1) => "error",
        Some(2) => "warning",
        Some(3) => "info",
        Some(4) => "hint",
        _ => "warning",
    }
    .to_string();

    let line = payload
        .get("range")
        .and_then(|range| range.get("start"))
        .and_then(|start| start.get("line"))
        .and_then(Value::as_u64)
        .unwrap_or(0) as u32
        + 1;
    let column = (payload
        .get("range")
        .and_then(|range| range.get("start"))
        .and_then(|start| start.get("character"))
        .and_then(Value::as_u64)
        .unwrap_or(0) as u32)
        .saturating_add(1);

    let code = payload.get("code").and_then(|value| {
        if let Some(value) = value.as_str() {
            return Some(value.to_string());
        }
        if value.is_number() {
            return Some(value.to_string());
        }
        None
    });

    LspDiagnostic {
        severity,
        source: payload
            .get("source")
            .and_then(Value::as_str)
            .map(ToString::to_string),
        code,
        message: payload
            .get("message")
            .and_then(Value::as_str)
            .unwrap_or("unknown diagnostic")
            .to_string(),
        file: file.to_string(),
        line,
        column,
        language: language.as_str().to_string(),
    }
}

fn parse_command(command: &str) -> Result<(String, Vec<String>)> {
    let mut parts: Vec<String> = Vec::new();
    let mut current = String::new();
    let mut in_single_quote = false;
    let mut in_double_quote = false;

    for ch in command.chars() {
        match ch {
            '\'' if !in_double_quote => {
                in_single_quote = !in_single_quote;
            }
            '"' if !in_single_quote => {
                in_double_quote = !in_double_quote;
            }
            ' ' | '\t' if !in_single_quote && !in_double_quote => {
                if !current.is_empty() {
                    parts.push(std::mem::take(&mut current));
                }
            }
            _ => current.push(ch),
        }
    }

    if in_single_quote || in_double_quote {
        return Err(anyhow!("invalid command with unclosed quote: {command}"));
    }
    if !current.is_empty() {
        parts.push(current);
    }

    let program = parts
        .first()
        .ok_or_else(|| anyhow!("empty command for language server"))?
        .to_string();
    let args = parts.iter().skip(1).cloned().collect();
    Ok((program, args))
}

/// Discover every `Cargo.toml` reachable from `workspace_root` so we can
/// pass them to rust-analyzer's `initializationOptions.linkedProjects`. The
/// LSP otherwise gives up on monorepo layouts whose root holds no manifest.
/// Skip the same dirs the rest of the indexer skips so we don't descend into
/// `target/`, `.venv/`, `node_modules/`, etc.
fn discover_cargo_manifests(workspace_root: &Path) -> Vec<PathBuf> {
    const IGNORED: &[&str] = &[
        "node_modules", ".venv", "venv", ".git", "target",
        "__pycache__", "dist", "build", ".next",
        ".mypy_cache", ".pytest_cache", ".ruff_cache",
        ".agentd", ".ai-editor", ".tmp", ".worktrees",
    ];

    let mut found: Vec<PathBuf> = Vec::new();
    let mut stack: Vec<PathBuf> = vec![workspace_root.to_path_buf()];

    while let Some(dir) = stack.pop() {
        let manifest = dir.join("Cargo.toml");
        if manifest.is_file() {
            found.push(manifest);
            // Don't descend further inside a Cargo project: workspace members
            // are wired via the root's [workspace] table and rust-analyzer
            // discovers them itself.
            continue;
        }

        let read = match std::fs::read_dir(&dir) {
            Ok(rd) => rd,
            Err(_) => continue,
        };
        for entry in read.flatten() {
            let path = entry.path();
            if !path.is_dir() {
                continue;
            }
            if let Some(name) = path.file_name().and_then(|n| n.to_str()) {
                if name.starts_with('.') || IGNORED.contains(&name) {
                    continue;
                }
            }
            stack.push(path);
        }
    }
    found
}

fn path_to_file_uri(path: &Path) -> String {
    let normalized = path.to_string_lossy().replace('\\', "/");
    // Percent-encode the bare minimum: space, plus a couple of reserved chars
    // that show up in real workspace paths. Without this, pyright sees a URI
    // it can't parse for any workspace whose path contains a space (and our
    // workspace is literally "/AI editor/...").
    let encoded = encode_uri_path(&normalized);
    if encoded.starts_with('/') {
        format!("file://{encoded}")
    } else {
        format!("file:///{encoded}")
    }
}

fn file_uri_to_path(uri: &str) -> String {
    let raw = uri.strip_prefix("file://").unwrap_or(uri);
    decode_uri_percent(raw)
}

/// Encode characters in a path that need escaping when round-tripped through
/// a `file://` URI. Whitelist alphanumerics, `/`, and common safe punctuation;
/// everything else becomes `%XX`. Deliberately conservative — paths in this
/// codebase contain spaces (workspace name "AI editor"), unicode is rare.
fn encode_uri_path(path: &str) -> String {
    let mut out = String::with_capacity(path.len());
    for byte in path.bytes() {
        match byte {
            b'A'..=b'Z'
            | b'a'..=b'z'
            | b'0'..=b'9'
            | b'/'
            | b'.'
            | b'_'
            | b'-'
            | b'~'
            | b':' => out.push(byte as char),
            _ => out.push_str(&format!("%{:02X}", byte)),
        }
    }
    out
}

/// Reverse of `encode_uri_path`. Handles `%XX` sequences; passes other bytes
/// through. Lossy decoding via UTF-8 is fine for paths (servers we talk to
/// emit valid UTF-8 URIs).
fn decode_uri_percent(uri: &str) -> String {
    let bytes = uri.as_bytes();
    let mut out: Vec<u8> = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            let hi = hex_digit(bytes[i + 1]);
            let lo = hex_digit(bytes[i + 2]);
            if let (Some(h), Some(l)) = (hi, lo) {
                out.push((h << 4) | l);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn hex_digit(b: u8) -> Option<u8> {
    match b {
        b'0'..=b'9' => Some(b - b'0'),
        b'a'..=b'f' => Some(b - b'a' + 10),
        b'A'..=b'F' => Some(b - b'A' + 10),
        _ => None,
    }
}

fn response_id(payload: &Value) -> Option<u64> {
    payload
        .get("id")
        .and_then(|id| id.as_u64().or_else(|| id.as_i64().map(|value| value as u64)))
}

fn encode_message(payload: &Value) -> Result<Vec<u8>> {
    let body = serde_json::to_vec(payload)?;
    let mut frame = format!("Content-Length: {}\r\n\r\n", body.len()).into_bytes();
    frame.extend(body);
    Ok(frame)
}

fn read_message<R>(reader: &mut R) -> Result<Option<Value>>
where
    R: BufRead,
{
    let mut content_length: Option<usize> = None;
    loop {
        let mut line = String::new();
        let bytes = reader.read_line(&mut line)?;
        if bytes == 0 {
            return Ok(None);
        }

        let trimmed = line.trim_end_matches(['\r', '\n']);
        if trimmed.is_empty() {
            break;
        }

        let (name, value) = trimmed
            .split_once(':')
            .ok_or_else(|| anyhow!("invalid JSON-RPC header line: '{trimmed}'"))?;
        if name.eq_ignore_ascii_case("content-length") {
            let parsed = value
                .trim()
                .parse::<usize>()
                .with_context(|| format!("invalid content length header value: '{}'", value.trim()))?;
            content_length = Some(parsed);
        }
    }

    let length = content_length.ok_or_else(|| anyhow!("missing content-length header"))?;
    let mut body = vec![0_u8; length];
    reader.read_exact(&mut body)?;
    let payload: Value = serde_json::from_slice(&body)?;
    Ok(Some(payload))
}

fn spawn_reader_thread(stdout: impl Read + Send + 'static) -> Receiver<Value> {
    let (tx, rx) = mpsc::channel();
    thread::spawn(move || {
        let mut reader = BufReader::new(stdout);
        loop {
            match read_message(&mut reader) {
                Ok(Some(payload)) => {
                    if tx.send(payload).is_err() {
                        break;
                    }
                }
                Ok(None) => break,
                Err(error) => {
                    let _ = tx.send(json!({
                        "jsonrpc": "2.0",
                        "method": "$internal/readError",
                        "params": { "message": error.to_string() }
                    }));
                    break;
                }
            }
        }
    });
    rx
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn jsonrpc_roundtrip_frame_encode_decode() {
        let payload = json!({
            "jsonrpc": "2.0",
            "id": 7,
            "method": "example/test",
            "params": { "ok": true }
        });

        let frame = encode_message(&payload).expect("encode");
        let mut reader = BufReader::new(frame.as_slice());
        let decoded = read_message(&mut reader).expect("read").expect("payload");

        assert_eq!(decoded, payload);
    }

    #[test]
    fn language_routing_uses_expected_extensions() {
        assert_eq!(
            LspAdapter::language_for_path(Path::new("src/main.ts")),
            Some(LspLanguage::TypeScript)
        );
        assert_eq!(
            LspAdapter::language_for_path(Path::new("src/main.py")),
            Some(LspLanguage::Python)
        );
        assert_eq!(
            LspAdapter::language_for_path(Path::new("src/main.rs")),
            Some(LspLanguage::Rust)
        );
        assert_eq!(
            LspAdapter::language_for_path(Path::new("README.md")),
            None
        );
    }

    #[test]
    fn command_parser_handles_quoted_args() {
        let (program, args) =
            parse_command("pyright-langserver --stdio --node-ipc --log-file \"a b.log\"")
                .expect("parse");
        assert_eq!(program, "pyright-langserver");
        assert_eq!(args, vec!["--stdio", "--node-ipc", "--log-file", "a b.log"]);
    }

    #[test]
    fn restart_budget_transitions_disable_after_limit() {
        let mut slot = SessionSlot::new("rust-analyzer".to_string());
        assert!(slot.can_restart());
        slot.restart_count = MAX_RESTART_ATTEMPTS;
        assert!(!slot.can_restart());
    }

    #[tokio::test]
    async fn adapter_collects_diagnostics_from_mock_server() {
        if std::process::Command::new("python3")
            .arg("--version")
            .output()
            .is_err()
        {
            return;
        }

        let root = std::env::temp_dir().join(format!(
            "ai-editor-indexer-lsp-test-{}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).expect("workspace");

        let script_path = root.join("mock_lsp.py");
        fs::write(
            &script_path,
            r#"import json, sys
def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        k, v = line.decode("utf-8").split(":", 1)
        headers[k.lower().strip()] = v.strip()
    length = int(headers.get("content-length", "0"))
    body = sys.stdin.buffer.read(length)
    return json.loads(body.decode("utf-8"))
def send_message(payload):
    body = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()
while True:
    msg = read_message()
    if msg is None:
        break
    method = msg.get("method")
    if method == "initialize":
        send_message({"jsonrpc":"2.0","id":msg["id"],"result":{"capabilities":{}}})
    elif method == "textDocument/didOpen":
        uri = msg["params"]["textDocument"]["uri"]
        send_message({
            "jsonrpc":"2.0",
            "method":"textDocument/publishDiagnostics",
            "params":{
                "uri": uri,
                "diagnostics":[{
                    "range":{"start":{"line":0,"character":0},"end":{"line":0,"character":1}},
                    "severity":1,
                    "source":"mock",
                    "code":"E1",
                    "message":"mock diagnostic"
                }]
            }
        })
"#,
        )
        .expect("script");

        let ts_file = root.join("sample.ts");
        fs::write(&ts_file, "const n: number = 'oops';\n").expect("sample file");

        let config = IndexerConfig {
            workspace_root: root.clone(),
            max_parse_workers: 1,
            watch_enabled: false,
            lsp_enabled: true,
            lsp_ts_cmd: format!("python3 -u {}", script_path.display()),
            lsp_py_cmd: "definitely-missing-pyright".to_string(),
            lsp_rs_cmd: "definitely-missing-rust-analyzer".to_string(),
            lsp_startup_timeout_ms: 2_000,
            lsp_request_timeout_ms: 2_000,
            snapshot_output_path: root.join("snapshot.json"),
        };

        let mut adapter = LspAdapter::new(&config);
        adapter
            .upsert_file(&ts_file, "const n: number = 'oops';\n".to_string())
            .await
            .expect("upsert");
        let diagnostics = adapter.collect_diagnostics(&root).await.expect("collect");
        assert!(diagnostics.iter().any(|diag| diag.message == "mock diagnostic"));
    }

    #[tokio::test]
    async fn adapter_records_warning_when_server_times_out() {
        if std::process::Command::new("python3")
            .arg("--version")
            .output()
            .is_err()
        {
            return;
        }

        let root = std::env::temp_dir().join(format!(
            "ai-editor-indexer-lsp-timeout-{}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).expect("workspace");

        let script_path = root.join("timeout_lsp.py");
        fs::write(
            &script_path,
            r#"import json, sys, time
def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        k, v = line.decode("utf-8").split(":", 1)
        headers[k.lower().strip()] = v.strip()
    length = int(headers.get("content-length", "0"))
    body = sys.stdin.buffer.read(length)
    return json.loads(body.decode("utf-8"))
while True:
    msg = read_message()
    if msg is None:
        break
    if msg.get("method") == "initialize":
        time.sleep(5)
"#,
        )
        .expect("script");

        let ts_file = root.join("sample.ts");
        fs::write(&ts_file, "const n: number = 'oops';\n").expect("sample file");

        let config = IndexerConfig {
            workspace_root: root.clone(),
            max_parse_workers: 1,
            watch_enabled: false,
            lsp_enabled: true,
            lsp_ts_cmd: format!("python3 -u {}", script_path.display()),
            lsp_py_cmd: "definitely-missing-pyright".to_string(),
            lsp_rs_cmd: "definitely-missing-rust-analyzer".to_string(),
            lsp_startup_timeout_ms: 100,
            lsp_request_timeout_ms: 100,
            snapshot_output_path: root.join("snapshot.json"),
        };

        let mut adapter = LspAdapter::new(&config);
        adapter
            .upsert_file(&ts_file, "const n: number = 'oops';\n".to_string())
            .await
            .expect("upsert");
        let diagnostics = adapter.collect_diagnostics(&root).await.expect("collect");

        assert!(diagnostics
            .iter()
            .any(|diag| diag.code.as_deref() == Some("session_start_failed")));
    }
}
