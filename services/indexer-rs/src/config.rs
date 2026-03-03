use std::env;
use std::path::PathBuf;

#[derive(Debug, Clone)]
pub struct IndexerConfig {
    pub workspace_root: PathBuf,
    pub max_parse_workers: usize,
    pub watch_enabled: bool,
    pub lsp_enabled: bool,
    pub lsp_ts_cmd: String,
    pub lsp_py_cmd: String,
    pub lsp_rs_cmd: String,
    pub lsp_startup_timeout_ms: u64,
    pub lsp_request_timeout_ms: u64,
    pub snapshot_output_path: PathBuf,
}

impl IndexerConfig {
    pub fn from_env() -> Self {
        let workspace_root = env::args()
            .skip_while(|arg| arg != "--workspace")
            .nth(1)
            .map(PathBuf::from)
            .or_else(|| env::var("AI_EDITOR_WORKSPACE").ok().map(PathBuf::from))
            .unwrap_or_else(|| PathBuf::from("."));

        let max_parse_workers = env::var("AI_EDITOR_MAX_PARSE_WORKERS")
            .ok()
            .and_then(|value| value.parse::<usize>().ok())
            .unwrap_or(4);

        let watch_enabled = env::var("AI_EDITOR_WATCH")
            .map(|value| value != "0")
            .unwrap_or(true);

        let lsp_enabled = env::var("AI_EDITOR_LSP_ENABLED")
            .map(|value| value != "0")
            .unwrap_or(true);

        let lsp_ts_cmd = env::var("AI_EDITOR_LSP_TS_CMD")
            .unwrap_or_else(|_| "typescript-language-server --stdio".to_string());
        let lsp_py_cmd = env::var("AI_EDITOR_LSP_PY_CMD")
            .unwrap_or_else(|_| "pyright-langserver --stdio".to_string());
        let lsp_rs_cmd =
            env::var("AI_EDITOR_LSP_RS_CMD").unwrap_or_else(|_| "rust-analyzer".to_string());

        let lsp_startup_timeout_ms = env::var("AI_EDITOR_LSP_STARTUP_TIMEOUT_MS")
            .ok()
            .and_then(|value| value.parse::<u64>().ok())
            .unwrap_or(3_000);
        let lsp_request_timeout_ms = env::var("AI_EDITOR_LSP_REQUEST_TIMEOUT_MS")
            .ok()
            .and_then(|value| value.parse::<u64>().ok())
            .unwrap_or(3_000);

        let snapshot_output_path = env::var("AI_EDITOR_INDEX_SNAPSHOT_PATH")
            .ok()
            .map(PathBuf::from)
            .unwrap_or_else(|| workspace_root.join(".ai-editor/index-snapshot.json"));

        Self {
            workspace_root,
            max_parse_workers,
            watch_enabled,
            lsp_enabled,
            lsp_ts_cmd,
            lsp_py_cmd,
            lsp_rs_cmd,
            lsp_startup_timeout_ms,
            lsp_request_timeout_ms,
            snapshot_output_path,
        }
    }
}
