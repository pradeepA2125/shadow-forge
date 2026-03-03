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

        let params = json!({
            "processId": std::process::id(),
            "rootUri": workspace_uri,
            "capabilities": {},
            "workspaceFolders": [{
                "uri": workspace_uri,
                "name": name
            }]
        });

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

fn path_to_file_uri(path: &Path) -> String {
    let normalized = path.to_string_lossy().replace('\\', "/");
    if normalized.starts_with('/') {
        format!("file://{normalized}")
    } else {
        format!("file:///{normalized}")
    }
}

fn file_uri_to_path(uri: &str) -> String {
    uri.strip_prefix("file://")
        .unwrap_or(uri)
        .to_string()
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
