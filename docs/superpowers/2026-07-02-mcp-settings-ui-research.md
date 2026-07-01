# MCP Settings UI — How the Field Does It (Research)

**Date:** 2026-07-02 · **Feeds:** P4 (UI/composer phase) of
`2026-06-29-feature-roadmap-copilot-parity.md`; informs one P3 seam (§ "Implication for P3" below).
**Question:** Codex, Claude Code, Cursor, VS Code Copilot, and Claude Desktop all let users
configure MCP servers without hand-editing config files. How, exactly?

---

## 1. The one universal pattern

**No product replaces the config file. Every UI is a guided writer over it.**

Claude Code's wizard writes `~/.claude.json`/`.mcp.json`; Codex's CLI writes `~/.codex/config.toml`;
VS Code's "MCP: Add Server" flow writes `mcp.json`; Cursor's deeplink approval dialog writes
`mcp.json`; only Claude Desktop (consumer product, remote-only connectors) hides the file entirely.
The file stays the source of truth — editable by hand, committable, diffable — and the UI is an
affordance layered on top.

This is exactly the architecture our P3 design already has: `.ai-editor/mcp.json` +
`McpConfigLoader` (mtime-cached, self-updating without restart). **A P4 settings UI that
read-modify-writes that file gets live config reload for free.** The backend delta for P4 is small
(see §4).

## 2. Per-product breakdown

### Claude Code (CLI + in-session panel)

- **`claude mcp add`** — CLI writer: `--transport http|sse|stdio`, `--env KEY=V`, `--header`,
  `--scope local|project|user`, `--` separator for stdio command+args. `add-json` for raw JSON,
  `list`/`get`/`remove`, `claude mcp login|logout <name>` runs a server's OAuth flow from the shell.
- **`/mcp` in-session panel** — the live management surface: every server with connection status +
  tool count, per-server menu (view tools, **Authenticate/Re-authenticate** for OAuth,
  retry a failed server), and **approval of project-scoped servers**.
- **Three scopes:** `local` (`~/.claude.json`, keyed by project path — private), `project`
  (`.mcp.json` in repo, shared via git), `user` (`~/.claude.json`, global).
- **Trust model — the part worth copying:** a `.mcp.json` that arrives via `git clone` is NOT
  trusted by config-presence. Servers show as `⏸ Pending approval` until the user approves them
  in-session; approvals live in the user's own settings files (never the committed ones), and a
  `disabledMcpjsonServers` entry always wins. This is our design decision 4 (`enabled: true`)
  taken one step further — approval is *per-user*, outside the shareable file (see §5 gap).
- **OAuth:** a 401/403 from a remote server flags it in `/mcp`; startup notice when servers need
  sign-in; automatic reconnect w/ backoff for HTTP/SSE, manual retry from the panel after 5 failures.

### Codex (the minimal bar)

- **`codex mcp add <name> --url …` / `-- <command>`** writes `~/.codex/config.toml` (global) or
  `.codex/config.toml` (project, requires trusted dir); `codex mcp list [--json]`.
- **IDE extension has NO form UI**: gear icon → Codex Settings → **"Open config.toml"** — it just
  opens the file. Config is shared CLI↔IDE so you configure once.
- Lesson: the floor is "a CLI writer + open-the-file button." Everyone else does more.

### VS Code / Copilot (most relevant — our frontend IS a VS Code extension)

- **`MCP: Add Server` command** — guided Command Palette flow: pick server type (stdio command /
  HTTP) → prompts for command-or-URL, name → pick scope (**Workspace** `.vscode/mcp.json` vs
  **Global** user `mcp.json`). Pure QuickPick/InputBox — no webview needed.
- **Extensions-view gallery**: search `@mcp` → curated server gallery → Install / right-click →
  Install in Workspace. Installed servers appear in an **MCP SERVERS - INSTALLED** section with
  right-click **Enable/Disable/Uninstall**, gear menu, **Show Output** (server logs).
- **`MCP: List Servers`** → per-server actions (start/stop/restart, enable/disable, show output).
- **Trust:** first start of any server → confirmation dialog with a link to review the config;
  `MCP: Reset Trust` clears decisions.
- **Enabled/disabled state is stored SEPARATELY from the config file**, so a team-shared
  `.vscode/mcp.json` is unaffected by one user disabling a server.
- **Per-tool toggles**: Chat view → Configure Tools → tick/untick individual tools of a server.
- **Secrets:** `inputs` variables (`${input:api-key}`, `password: true`) — VS Code prompts on
  first server start and stores the value in secret storage, never in the file. Plus `${env:VAR}`.
- **One-click install URL handler:** `vscode:mcp/install?{urlencoded-JSON-config}` — README badges
  fire it; VS Code shows the config for review before writing it. Cursor's equivalent:
  `cursor://anysphere.cursor-deeplink/mcp/install?name=$NAME&config=$BASE64` → approval dialog →
  writes `mcp.json`.
- **Auto-discovery:** `chat.mcp.discovery.enabled` imports server configs from other apps
  (e.g. Claude Desktop's config file).

### Cursor

- **Settings → Tools & MCP pane**: every server listed with a live status dot, an
  **enable/disable toggle**, and its tool list; add-new opens the JSON (form-lite).
- **Deeplink + directory**: `cursor.directory` marketplace with "Add to Cursor" buttons →
  deeplink → approval dialog → written to `mcp.json`; OAuth handled after install.

### Claude Desktop (consumer ceiling)

- **Settings → Connectors → "Add custom connector"**: a form with just **name + remote URL**
  (+ optional OAuth client id/secret under Advanced). Remote-only; no stdio exposure to end users.
- Per-conversation enable/disable toggles on each connector.

## 3. The affordance ladder (composable, in cost order)

1. **Guided add flow** (QuickPick prompts or small form) that writes the config file. — *Everyone.*
2. **Server list w/ live status + tool count + enable/disable toggle + remove.** — *Everyone but Codex.*
3. **Trust/approval gate on first use of a workspace-provided server**, per-user, stored outside
   the shared file. — *Claude Code, VS Code.*
4. **Show Output / logs per server + manual retry-reconnect.** — *VS Code, Claude Code.*
5. **Secrets kept out of the file**: env interpolation (all), promptString→secret-storage
   (VS Code), OAuth flows from the panel (Claude Code, Desktop, Cursor).
6. **Per-tool toggles within a server.** — *VS Code, Claude Desktop (per-chat).*
7. **One-click install deeplinks / gallery.** — *VS Code, Cursor.*

## 4. What this means for OUR P4 (concrete shape)

Backend (small — the P3 loader already does the hard part):
- `GET /v1/mcp/servers` — merged view: config entries + live connection status + tool counts
  (the `/mcp`-panel data). Gated by `is_mcp_enabled()` like the memory routes.
- `POST/PATCH/DELETE /v1/mcp/servers/{name}` — read-modify-write `.ai-editor/mcp.json`
  (preserve unknown keys; never write resolved secrets — store `${VAR}` references verbatim).
  The mtime cache picks the change up; **the client manager must reconcile sessions** (see §5).
- Optional: `POST /v1/mcp/servers/{name}/reconnect` (manual retry, VS Code/Claude Code both have it).

Frontend (two tiers, can ship separately):
- **Tier 1 (cheap, VS Code-native):** `AI Editor: Add MCP Server` command — QuickPick wizard
  (transport → command/URL → name → env var names) posting to the write API; `AI Editor: List MCP
  Servers` QuickPick with enable/disable/remove/reconnect. Mirrors "MCP: Add Server" exactly; no
  webview work.
- **Tier 2 (the pane):** an MCP section in a settings webview — server list with status dots,
  toggles, tool lists, add-form. The `MemoryPanel` second-Vite-entry pattern is the proven
  template (panel class + vscode-free data source + `when`-context off `/v1/config`).
- Bundled one-click GitHub entry = a pre-filled QuickPick template in Tier 1 (deferred decision 5
  lands here naturally).

## 5. Implication for P3 (act on this NOW, before implementation)

1. **Connect-once-per-process conflicts with a settings UI.** P3 §3.2 connects sessions eagerly at
   `select_chat_handler` time and never again ("failed server stays excluded until restart", §9).
   A UI that edits the file expects the new server to connect *without a backend restart*. Fix is
   cheap if done now: shape the client as a **`McpClientManager` with a `reconcile(configs)`
   method** — eager-connect at factory time is just `reconcile(initial)`; P4's write-API calls
   `reconcile(loader.load())` after a write. Baking connect-once into factory wiring instead
   forces a P4 refactor of subprocess lifecycle code.
2. **`enabled: true` inside a shareable file is presence-trust in disguise.** If `.ai-editor/mcp.json`
   is ever committed, a cloned repo arrives with `enabled: true` already set — decision 4's
   allowlist gates nothing. Claude Code and VS Code both keep the *approval/enablement* bit
   per-user, outside the shared file. v1 can keep `enabled` as-is (file is bespoke, likely
   git-ignored), but P4's toggle should write user-local state (extension `globalState` or a
   user-scoped backend table), not flip the shared file.
3. **Status must be queryable, not just logged.** P3's degrade-not-raise (warn + exclude) is right,
   but every product surfaces per-server status in UI. Have the client manager keep a
   `status: connected | failed(reason) | disabled` per server from day one — the P4 `GET` route
   then just serializes it.

## Sources

- Claude Code MCP reference — https://code.claude.com/docs/en/mcp
- Codex MCP — https://developers.openai.com/codex/mcp · config reference — https://developers.openai.com/codex/config-reference
- VS Code: Add & manage MCP servers — https://code.visualstudio.com/docs/agent-customization/mcp-servers
- VS Code MCP extension-API guide (`vscode:mcp/install` handler) — https://code.visualstudio.com/api/extension-guides/ai/mcp
- Cursor MCP install links — https://cursor.com/docs/context/mcp/install-links · MCP docs — https://cursor.com/docs/mcp
- Claude Desktop custom connectors — https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp
- GitHub Copilot + MCP — https://docs.github.com/copilot/customizing-copilot/using-model-context-protocol/extending-copilot-chat-with-mcp
