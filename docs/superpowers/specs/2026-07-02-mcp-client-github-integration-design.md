# P3 — MCP Client + GitHub Integration — Design

**Status:** Approved design, pre-implementation · **Date:** 2026-07-02 · **Owner:** pradeep
**Roadmap:** Phase 3 of `docs/superpowers/2026-06-29-feature-roadmap-copilot-parity.md`
**Next:** `writing-plans` → implementation plan.

---

## 1. Goal

Connect external MCP (Model Context Protocol) tool servers — databases, web, and **GitHub** as the
reference integration — matching Copilot's core extensibility story on the open MCP standard, the
same "adopt standards, don't invent" principle that drove P1/P2.

An MCP server (local subprocess over stdio, or a remote HTTP/SSE endpoint) exposes a set of tools
with their own JSON-schema parameter definitions. The agent connects to servers listed in a local
config file, and the tools those servers expose become callable alongside the existing built-in
tools — gated behind a live approval, the same way `run_command` already is.

This plugs into the same seams P1/P2 used: the **controller system-prompt assembly** (gated teaching
blocks), the **`ToolSource`/`AggregatingToolRegistry`** composite, and the existing **command-approval
gate machinery** (`PendingGate`, async future + timeout, remember-rule).

## 2. Decisions (resolved during brainstorming)

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | **Controller-only** (mirrors P1/P2) | The planning/task ReAct path is flag-gated OFF by default with no live consumer today; scoping there now is speculative surface area for a dormant path. |
| 2 | **Both stdio and HTTP/SSE transports in v1** | Matches the roadmap scope as written, even though stdio covers the large majority of real-world servers (including GitHub's official one). |
| 3 | **Bespoke `.ai-editor/mcp.json`**, not the ecosystem `.mcp.json` convention | Matches the precedent P2 actually set (it deviated from its own roadmap text and shipped `.ai-editor/skills/` only in v1, deferring ecosystem dirs as "a trivial later add"). Reading `.mcp.json` directly is a low-effort follow-up if wanted. |
| 4 | **Explicit per-server allowlist beyond config presence** — each entry needs `"enabled": true` | P1/P2 both use "master flag on ⇒ everything discovered is trusted" (no per-item toggle until P4). MCP servers execute arbitrary tool calls against real external services — a materially larger blast radius than a skill's static markdown or an instructions file — so simply being *listed* in a config file isn't treated as sufficient trust on its own. |
| 5 | **GitHub is proof-via-user-config, not a bundled default** | Keeps P3 scoped to "build a generic, spec-compliant MCP client." No GitHub-specific code ships; we verify a user-authored entry for the official GitHub MCP server works end-to-end. A bundled one-click default is P4 settings-pane territory. |
| 6 | **Official `mcp` Python SDK**, not a hand-rolled client | Matches actual codebase precedent: `openai`/`anthropic`/`google-genai`/`groq`/`ibm-watsonx-ai` are all official SDKs as **hard** dependencies in `pyproject.toml` (not optional extras) — hand-rolled HTTP is only used where no SDK exists (TurboQuant, a local server with none). MCP's bidirectional JSON-RPC protocol (capability negotiation, subprocess lifecycle, SSE reconnect) is meaningfully more complex than a REST chat-completions call — the class of complexity where reimplementing correctly costs far more than the dependency. |
| 7 | **New system-prompt teaching block + a new `"mcp_tool"` gate kind** — not a reuse of the skills catalog block or the `"command"` gate | Tool *schemas* flow into `tools_json` for free via the existing `ToolDefinition`/`AggregatingToolRegistry` seam (no progressive-disclosure step needed, unlike skills). But the model still needs to be told these are external/side-effecting actions that will pause for approval — different framing from a local file read, and different UI copy from "Run command:". |
| 8 | **Tool-count scale path is flagged, not built** | If a workspace connects enough servers/tools to bloat `tools_json`, order-truncation (v1) can start silently dropping useful tools. The fix (query-ranked filtering via the existing memory `Embedder`) is the same Tool-RAG mechanism P2's spec already earmarked for this exact scenario (see §9/§10) — recorded here as a dormant follow-up, not implemented in v1. |

## 3. Architecture

### 3.1 Config (`agentd/mcp/config.py`)

- **New module `agentd/mcp/`** (mirrors `agentd/skills/`'s module shape).
- `McpConfigLoader` — mtime-cached reader for `<workspace>/.ai-editor/mcp.json` (same caching
  discipline as `SkillCatalogLoader`/`ProjectInstructionsLoader`: self-updates on edit, no restart,
  best-effort — a malformed file degrades to "no servers," never crashes a turn).
- Each server entry: standard MCP shape (`command`/`args`/`env` for stdio; `url`/`headers` for HTTP)
  **plus** a required `"enabled": true` (decision 4 — presence in the file alone doesn't connect it).
- **Secret handling:** `env`/`headers` values support `${VAR}` interpolation resolved against the
  real process environment at connect time — a token is never embedded raw in a file that might get
  committed.
- Output: `list[McpServerConfig]` (Pydantic/dataclass model in `agentd/mcp/models.py`).

### 3.2 Client (`agentd/mcp/client.py`)

- Thin wrapper around the official `mcp` SDK's `ClientSession`. One session per **enabled** server.
- **Eager connection, once per process** — the manager is *constructed* at
  `controller_factory.select_chat_handler` time (alongside the workspace-frozen
  `SkillCatalogLoader`/`ProjectInstructionsLoader`, §3.6) but *connects* in a FastAPI **startup
  event handler** (`app.add_event_handler("startup", manager.start)` in `main.py`): the factory
  runs at module import with **no running event loop**, and the SDK's transports/sessions are
  async context managers, so connecting at factory time is literally impossible (verified against
  `main.py:373` during planning). Connection is **not** inside `ChatController._build_registry`
  (which runs once per `_run_loop`, i.e. once per user *message*). `McpToolSource` wraps the already-connected sessions; it never triggers a new
  connection itself. Getting this scope wrong — reconnecting subprocesses on every chat message
  instead of once for the process lifetime — is exactly the class of bug this session's `decide_entry`
  fix was about (a value silently built at the wrong lifecycle scope), so it's called out explicitly
  here rather than left to the implementer to infer. Mirrors the LSP watcher's one-time startup
  warmup: stdio servers are real subprocesses worth starting once, not on first use or every turn.
- A server that fails to connect (bad command, unreachable URL, handshake failure) logs a warning
  and contributes zero tools — degrade-not-raise, same contract as every other optional subsystem
  (memory, instructions, skills).
- **Shape the client as a manager with a `reconcile(configs)` method** (eager boot connect =
  `reconcile(loader.load())` once at factory time), and keep a queryable per-server
  `status: connected | failed(reason) | disabled` — not connect-once logic baked into factory
  wiring. Rationale: the P4 settings UI (see
  `docs/superpowers/2026-07-02-mcp-settings-ui-research.md`) edits `.ai-editor/mcp.json` at
  runtime and expects servers to connect/disconnect without a backend restart, and every surveyed
  product exposes per-server status in its UI. v1 still only *calls* reconcile once — the seam
  costs nothing now and avoids a P4 refactor of subprocess-lifecycle code.

### 3.3 Tool exposure (`agentd/mcp/tool_source.py`)

- **New `McpToolSource`** implementing the existing `ToolSource` protocol (`name`/`definitions()`/
  `owns()`/`execute()` — the exact seam `SkillToolSource` already proves out). Unlike
  `SkillToolSource`'s fixed one-tool schema, `definitions()` here is **dynamic**: built from whatever
  the connected sessions report via `list_tools()`.
- **Naming/namespacing:** `mcp__<server>__<tool>` — avoids collisions across servers (two servers
  can both expose a `search` tool) and matches an already-proven, already-familiar convention (the
  same shape used by MCP tools visible in this very development environment).
- Real per-tool JSON schemas (`ToolDefinition.parameters`) flow into the controller's `tools_json`
  system-prompt block automatically via the existing `AggregatingToolRegistry.definitions()`
  concatenation — **no new schema-injection plumbing needed.**

### 3.4 Approval gate (new `"mcp_tool"` `PendingGate` kind)

- Reuses the exact mechanism from `_command_approval_cb` — async future + timeout + durable Class-A
  gate (survives a reload, renders from `/live`) + a resolved-gate breadcrumb — as its own gate
  *kind* rather than overloading `"command"`, since the UI copy needs to read "Call MCP tool:
  `server.tool(args)`" (not "Run command:") and a remember-rule keyed on `(server, tool)` rather than
  a shell-command string.
- New `AI_EDITOR_MCP_DECISION_TIMEOUT_SEC` (mirrors `AI_EDITOR_COMMAND_DECISION_TIMEOUT_SEC`,
  same default-reject-on-timeout behavior) — independently tunable from the command gate.

### 3.5 Prompt teaching (`agentd/chat/controller_prompts.py`)

- **New gated block**, present only when MCP tools are active (mirrors `_SKILLS_BLOCK_HEADER`/
  `_MEMORY_BLOCK`), teaching two things the schema alone doesn't convey:
  1. These are **external, potentially side-effecting** actions against real third-party services —
     closer in kind to `run_command` than to a local read tool.
  2. Calling one **will pause for a live approval gate** — that's expected, not an error to route
     around.
- **What this block deliberately does NOT need** (contrast with P2): no skill-style "check the
  catalog before acting" triage instruction. MCP tools have no progressive-disclosure step — the
  full schema is already visible in `tools_json`, so the model calls one directly like any other
  tool. The decide_entry/few-shot compliance fight from P2 does not recur here.
- **Budget guard:** `AI_EDITOR_MCP_TOOLS_MAX_CHARS` (order-truncated, mirrors
  `select_catalog_for_budget`) — MCP tool schemas are full JSON schemas, heavier than skills'
  one-line catalog entries, so an unbounded `tools_json` is a real risk with more than a few
  chatty servers connected.

### 3.6 Flags (`agentd/chat/controller_factory.py`)

- `is_mcp_enabled()` next to `is_skills_enabled()`/`is_memory_enabled()` — `AI_EDITOR_MCP_ENABLED`,
  **default OFF** (truthy = `1/true/yes/on`). Off: the config loader is never built, no servers
  connect, `McpToolSource` is not registered, the prompt block never appends.
- `select_chat_handler` builds the `McpConfigLoader`/connects servers from the frozen
  `workspace_path` when enabled (same place the memory harness + instructions + skills loaders are
  wired).

## 4. Components & boundaries

| Unit | Responsibility | Depends on |
|------|----------------|------------|
| `mcp/config.py::McpConfigLoader` | mtime-cached scan + parse `.ai-editor/mcp.json`, `${VAR}` interpolation, `enabled` filter | filesystem only |
| `mcp/models.py::McpServerConfig` | `{name, transport, command/args/env or url/headers, enabled}` | — |
| `mcp/client.py` | wraps `mcp` SDK `ClientSession` per enabled server; connect/list_tools/call_tool | official `mcp` SDK, config |
| `mcp/tool_source.py::McpToolSource` | dynamic `definitions()` from connected sessions; namespaced `owns()`/`execute()`; routes through the approval gate | client, approval gate callback |
| `controller.py` — new `"mcp_tool"` `PendingGate` kind + `_mcp_approval_cb` | approve/reject/timeout + remember-rule, mirrors `_command_approval_cb` | existing gate/`PendingGate` infra |
| `controller_prompts.py` — new MCP teaching block + budget guard | render teaching text; truncate `tools_json` contribution when over budget | none (pure text/budget function) |
| `controller_factory.py::is_mcp_enabled` + loader/client wiring | resolve flag (default off); build loader + connect servers from frozen workspace | env |

Each unit is testable in isolation: the config loader against a `tmp_path` `mcp.json`; the client
against a fake/stub `ClientSession`; the tool source against a stub client; the approval gate as a
direct mirror of the existing command-gate tests; the prompt block as a pure-text test over a
manifest list.

## 5. Data flow

1. Model emits `tool_call` with name `mcp__<server>__<tool>` (DECIDE or EDIT phase, same as any
   other tool).
2. `AggregatingToolRegistry.execute()` routes by `owns()` (prefix match) to `McpToolSource`.
3. `McpToolSource` raises the `"mcp_tool"` gate for that call (payload: server, tool, args) and
   awaits the decision (approve / reject / timeout).
4. On approve: calls `session.call_tool(tool, args)` via the SDK against the already-connected
   session.
5. MCP results can be structured (text/image/resource content blocks). v1 flattens text blocks into
   the `ToolOutput` string and notes/skips non-text blocks — matching how every other tool result in
   this loop is plain text today; richer content types are a later follow-up, not a v1 blocker.
6. Result rides back into the ReAct loop exactly like any other `tool_result` — no special-casing
   needed downstream in `ControllerLoop`.

On reject: same shape as a rejected command — `ToolOutput(is_error=True, ...)`, the loop adapts
rather than hard-failing.

## 6. Error handling

- **Server fails to connect at boot** (bad command, crashed subprocess, unreachable URL, failed
  handshake): log a warning, exclude that server's tools, don't crash the backend.
- **A tool call times out or the server dies mid-call:** `ToolOutput(is_error=True, ...)` with a
  clear message; the loop adapts, no crash.
- **Approval timeout:** default-reject, per `AI_EDITOR_MCP_DECISION_TIMEOUT_SEC`.
- **Malformed/missing `.ai-editor/mcp.json`:** no servers connected, no crash — same contract as a
  missing AGENTS.md or empty skills dir.
- **A server's `${VAR}` reference is unset:** that server fails to connect (treated the same as any
  other connect failure) with a warning naming the missing variable, rather than connecting with a
  blank/broken credential.

## 7. Testing

**Python (pytest):**
- Config loader: empty/missing file → `[]`; valid entries → parsed configs; `enabled:false`/absent →
  excluded; `${VAR}` interpolation resolved/missing; mtime-unchanged → cached; malformed JSON →
  empty + warning, scan continues.
- Client + `McpToolSource`: fake `ClientSession` stub (mirrors `ScriptedReasoningEngine`-style test
  doubles already used throughout the suite) — `definitions()` reflects `list_tools()`; `owns()`
  matches the `mcp__<server>__` prefix only; `execute()` routes to the right session and maps a
  result to `ToolOutput`; a disconnected/failed server contributes zero tools without raising.
- Approval gate: approve/reject/timeout paths, remember-rule persistence — direct mirror of the
  existing `_command_approval_cb` test suite.
- Prompt block: appended iff MCP tools are active; budget guard truncates over cap.
- Factory: `is_mcp_enabled` default-off; `1/true` on.
- **Integration (real stdio round-trip):** spin up one of the `mcp` SDK's own trivial example/test
  servers as a real subprocess in a test — connect → `list_tools` → `call_tool` against the actual
  protocol, not just the stub.

**Live smoke (manual, not CI):**
1. Configure the official GitHub MCP server in `.ai-editor/mcp.json` with a real token, `enabled:true`
   → tools appear in the catalog, connect succeeds.
2. Drive a real request that calls a GitHub tool (read an issue / open a PR) → the `"mcp_tool"`
   approval gate renders → approve → the call executes → result lands in the transcript.
3. Reject path: same request, reject at the gate → the loop adapts (doesn't crash, doesn't retry the
   same call silently).
4. Kill-switch: `AI_EDITOR_MCP_ENABLED=0` → no servers connect, no MCP tools in `tools_json`, no
   teaching block.
5. A server with `enabled:false` (or absent from the file) → confirmed NOT connected, contributing
   no tools (decision 4's allowlist actually holding).

## 8. Exit criteria

- A configured, enabled MCP server's tools are discoverable and callable by the agent end-to-end
  (live), gated by the `"mcp_tool"` approval.
- GitHub MCP demonstrably opens a PR / reads an issue via a user-authored config entry.
- `AI_EDITOR_MCP_ENABLED` kill-switch verified (default off; on enables).
- The `enabled:true` per-server allowlist is verified as an actual gate, not a no-op.
- All Python suites + typecheck green; live smoke (1–5) passes.

## 9. Out of scope (deferred)

- **Ecosystem `.mcp.json` discovery** — bespoke `.ai-editor/mcp.json` only in v1 (decision 3); reading
  the cross-tool convention is a low-effort later add if wanted.
- **Bundled/one-click GitHub default** — proof-via-user-config only in v1 (decision 5); a shipped
  default entry is P4 settings-pane territory.
- **Query-ranked tool filtering at scale ("what if a user connects 200 MCP servers")** — v1 ships
  only the cheap order-truncation budget guard (§3.5). The fix is query-ranked filtering over the
  existing memory `Embedder`, the same Tool-RAG mechanism P2's spec already earmarked for exactly
  this scenario (see §10) — wire it only when order-truncation actually starts dropping useful tools
  in practice, not preemptively.
- **Rich MCP content types** (image/resource result blocks) — v1 flattens to text only.
- **Server health/reconnect policy beyond "fails once, logs, excluded"** — no retry/backoff loop in
  v1; a failed server stays excluded until the next config-driven reconnect (mtime change or
  restart).
- **Per-server management UI** (add/remove/health/enable-disable pane) — **P4**, same as skills.
  Field research on how Claude Code/Codex/VS Code/Cursor/Claude Desktop do this (and the concrete
  P4 shape: a write-API over `mcp.json` + a QuickPick-wizard tier + a settings-pane tier) is in
  `docs/superpowers/2026-07-02-mcp-settings-ui-research.md`. Two of its findings act on P3
  itself: the `reconcile()` client-manager seam (§3.2), and the caveat that `enabled: true` in a
  *committed* config file degrades to presence-trust — P4's per-user toggle must live outside the
  shared file.
- **Planning/task-path injection** — dormant path, untouched (decision 1, same scope call as P1/P2).
- **MCP *resources* (as opposed to tools)** — the roadmap scope is tools; resource access is a
  candidate follow-up if a real use case shows up.

## 10. Relationship to other phases

- **P1/P2 (done):** reuses the controller prompt-assembly seam, the `ToolSource`/
  `AggregatingToolRegistry` composite, and the command-approval gate machinery.
- **P2's own spec forward-referenced this exact scale problem:** `2026-06-30-agent-skills-design.md`
  §9 states *"P3 (MCP): the budget-gated Embedder ranking here is the same Tool-RAG mechanism MCP
  needs at 200+ tools — shared infra,"* and §10 cites *"MCP/tool definitions are heavy +
  provider-capped (~128), which is why Tool-RAG (embed descriptions, retrieve top-K/turn: 13%→43%
  accuracy, ½ tokens) is a P3 concern"* (RAG-MCP, arXiv 2505.03275; Red Hat Tool-RAG). §9 of this
  doc records that follow-up as deferred, not built — the infra (`Embedder`, the ranking-function
  shape) already exists from P2's dormant `rank_skills_by_relevance`.
- **P4 (UI):** surfaces MCP server management (add/remove/health/enable-disable) + the bundled
  GitHub default deferred here.
- **P5 (subagents):** heavy/long-running MCP tool calls are a candidate for forked-context execution,
  same as P2's deferred `context: fork` skills.
</content>
