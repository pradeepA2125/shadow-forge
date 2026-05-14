# Chat Panel VS Code Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the VS Code chat panel WebView, plan card UI with "Implement Plan" button, and inline diff card for small changes — wiring the frontend to the `ChatAgent` backend from Plan 1.

**Architecture:** A new `ChatPanel` class (mirrors `ReviewPanel` pattern) owns the sidebar WebView. The `AiEditorController` grows chat methods. The editor client adds `sendChatMessage()`, `listChatThreads()`, `getChatThread()`, and `streamPatchEvents()`. Plan cards and diff cards are special message types rendered inline in the conversation thread.

**Tech Stack:** TypeScript, VS Code WebView API, existing `HttpBackendClient` + Zod contract pattern, SSE streaming from `sendChatMessage()`.

**Prerequisite:** Plan 1 (chat-agent-backend) must be deployed — backend endpoints `POST /v1/chat/threads/{thread_id}/message`, `GET /v1/chat/threads`, etc. must exist.

---

## Pre-Implementation Review Corrections

The following issues were identified in a codebase review before implementation and are reflected in the task steps below:

1. **`streamPatch` API mismatch** — existing `streamPatch` is callback-based; a new `streamPatchEvents(taskId): AsyncIterable<PatchStreamEvent>` method is added alongside it (does not replace it).
2. **`task.result?.modifiedFiles` → `task.modifiedFiles`** — `getTask()` returns `TaskView` with `modifiedFiles` directly on it; no `.result` wrapper.
3. **`this.client` / `this.workspacePath` don't exist in `AiEditorController`** — replaced throughout with `this.createClient(this.settings.getBackendBaseUrl())` and `this.ui.getWorkspacePath() ?? ""`.
4. **`this.chatClient!` typo** — same fix as #3.
5. **Tests used MSW** — no MSW in project; all tests use `fetchFn` mock pattern with inline `Response` / `ReadableStream`.
6. **`BackendTaskClient` stub in `controller.test.ts` must be extended** with all new chat methods or typecheck fails.
7. **`ChatThread.created_at` missing from Python model** — `storage.py` already stores it in SQLite but the Pydantic model didn't expose it; fix in Task 0.
8. **`ChatPanel` missing `newChat`/`switchThread` handlers** — WebView sends these messages but original plan had no TS handlers; `onNewChat` and `onSwitchThread` callbacks added to constructor.
9. **Task 4 `ControllerUI` wiring was incomplete** — original plan only showed 4 of 9 new methods; all 9 are shown explicitly.
10. **`ChatPanel.show()` context param unused** — removed; disposal is managed via `panel.onDidDispose`.
11. **`untitled:` URI approach for inline diff is non-functional** — marked TODO, requires a virtual document provider; implement when backend emits `inline_diff_ready`.

---

## File Map

| File | Status | Responsibility |
|------|--------|----------------|
| `services/agentd-py/agentd/chat/models.py` | Modify | Add `created_at` field to `ChatThread` |
| `services/agentd-py/agentd/chat/storage.py` | Modify | Populate `created_at` from DB in `list_threads`/`get_thread`/`create_thread` |
| `apps/editor-client/src/contracts/task-contracts.ts` | Modify | Add `ChatMessageSchema`, `ChatEventSchema`, `ChatThreadSummarySchema`, `ChatThreadSchema`; add `streamPatchEvents` + chat methods to `BackendTaskClient` |
| `apps/editor-client/src/client/http-backend-client.ts` | Modify | Implement `listChatThreads()`, `createChatThread()`, `getChatThread()`, `sendChatMessage()`, `streamPatchEvents()` |
| `apps/editor-client/test/http-backend-client.test.ts` | Modify | Tests for new chat methods (fetchFn pattern) |
| `apps/vscode-extension/src/chat-panel.ts` | Create | `ChatPanel` WebView — renders thread, handles user input, wires newChat/switchThread |
| `apps/vscode-extension/src/extension.ts` | Modify | Register `aiEditor.openChat` command; wire all 9 new `ControllerUI` chat methods |
| `apps/vscode-extension/src/controller.ts` | Modify | Add `openChat()`, `sendChatMessage()`, `handlePlanCardAction()`, `streamTaskIntoChatThread()` |
| `apps/vscode-extension/test/controller.test.ts` | Modify | Extend stub + add chat tests |

---

## Task 0: Fix Python ChatThread Model

The SQLite schema stores `created_at` but the `ChatThread` Pydantic model doesn't expose it, so `model_dump()` omits it. The TypeScript client needs it.

**Files:**
- Modify: `services/agentd-py/agentd/chat/models.py`
- Modify: `services/agentd-py/agentd/chat/storage.py`

- [ ] **Step 1: Add `created_at` to `ChatThread` model**

In `services/agentd-py/agentd/chat/models.py`, add the field to `ChatThread`:

```python
class ChatThread(BaseModel):
    thread_id: str
    workspace_path: str
    title: str = "New Chat"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    messages: list[ChatMessage] = Field(default_factory=list)
    touched_files: list[str] = Field(default_factory=list)
```

- [ ] **Step 2: Populate `created_at` from the DB row in `storage.py`**

`create_thread`, `list_threads`, and `get_thread` all construct `ChatThread` objects. Add `created_at` from the DB row:

```python
# create_thread — pass created_at back when returning the new thread
def create_thread(self, workspace_path: str, title: str = "New Chat") -> ChatThread:
    thread_id = f"chat-{uuid.uuid4().hex[:12]}"
    created_at = datetime.now(timezone.utc).isoformat()
    self._conn.execute(
        "INSERT INTO chat_threads (thread_id, workspace_path, title, created_at) VALUES (?, ?, ?, ?)",
        (thread_id, workspace_path, title, created_at),
    )
    self._conn.commit()
    return ChatThread(
        thread_id=thread_id,
        workspace_path=workspace_path,
        title=title,
        created_at=datetime.fromisoformat(created_at),
    )

# list_threads — add created_at to each ChatThread construction
def list_threads(self, workspace_path: str) -> list[ChatThread]:
    rows = self._conn.execute(
        "SELECT * FROM chat_threads WHERE workspace_path = ? ORDER BY created_at DESC",
        (workspace_path,),
    ).fetchall()
    return [
        ChatThread(
            thread_id=row["thread_id"],
            workspace_path=row["workspace_path"],
            title=row["title"],
            created_at=datetime.fromisoformat(row["created_at"]),
            messages=[ChatMessage.model_validate(m) for m in json.loads(row["messages_json"])],
            touched_files=json.loads(row["touched_files_json"]),
        )
        for row in rows
    ]

# get_thread — same
def get_thread(self, thread_id: str) -> ChatThread | None:
    row = self._conn.execute(
        "SELECT * FROM chat_threads WHERE thread_id = ?", (thread_id,)
    ).fetchone()
    if row is None:
        return None
    return ChatThread(
        thread_id=row["thread_id"],
        workspace_path=row["workspace_path"],
        title=row["title"],
        created_at=datetime.fromisoformat(row["created_at"]),
        messages=[ChatMessage.model_validate(m) for m in json.loads(row["messages_json"])],
        touched_files=json.loads(row["touched_files_json"]),
    )
```

- [ ] **Step 3: Verify Python tests still pass**

```bash
cd services/agentd-py && source .venv/bin/activate && pytest tests/ -x -q
```
Expected: all pass (no existing test references `created_at` on `ChatThread`)

- [ ] **Step 4: Commit**

```bash
git add services/agentd-py/agentd/chat/models.py services/agentd-py/agentd/chat/storage.py
git commit -m "feat(chat): expose created_at on ChatThread model"
```

---

## Task 1: Editor Client Contracts + HTTP Methods

**Files:**
- Modify: `apps/editor-client/src/contracts/task-contracts.ts`
- Modify: `apps/editor-client/src/client/http-backend-client.ts`
- Modify: `apps/editor-client/test/http-backend-client.test.ts`

- [ ] **Step 1: Write failing tests**

Append to the existing `describe("HttpBackendClient", ...)` block in `apps/editor-client/test/http-backend-client.test.ts`:

```typescript
  // ── Chat API ──────────────────────────────────────────────────────────────

  test("listChatThreads maps snake_case to camelCase", async () => {
    const client = new HttpBackendClient({
      baseUrl: "http://localhost:8000",
      fetchFn: async () =>
        new Response(
          JSON.stringify({
            threads: [
              {
                thread_id: "chat-abc123",
                workspace_path: "/ws",
                title: "My chat",
                created_at: "2026-05-11T00:00:00Z",
              },
            ],
          }),
          { status: 200, headers: { "content-type": "application/json" } }
        ),
    });
    const result = await client.listChatThreads("/ws");
    expect(result).toHaveLength(1);
    expect(result[0].threadId).toBe("chat-abc123");
    expect(result[0].title).toBe("My chat");
    expect(result[0].createdAt).toBe("2026-05-11T00:00:00Z");
  });

  test("getChatThread maps thread and messages", async () => {
    const client = new HttpBackendClient({
      baseUrl: "http://localhost:8000",
      fetchFn: async () =>
        new Response(
          JSON.stringify({
            thread_id: "chat-abc123",
            workspace_path: "/ws",
            title: "My chat",
            created_at: "2026-05-11T00:00:00Z",
            messages: [
              {
                role: "user",
                content: "hello",
                type: "text",
                task_id: null,
                timestamp: "2026-05-11T00:00:01Z",
                metadata: {},
              },
            ],
            touched_files: [],
          }),
          { status: 200, headers: { "content-type": "application/json" } }
        ),
    });
    const result = await client.getChatThread("chat-abc123");
    expect(result.threadId).toBe("chat-abc123");
    expect(result.messages).toHaveLength(1);
    expect(result.messages[0].role).toBe("user");
  });

  test("sendChatMessage streams SSE events", async () => {
    const sseBody =
      'data: {"type":"intent_classified","payload":{"intent":"qa"}}\n\n' +
      'data: {"type":"chat_done","payload":{}}\n\n';
    const client = new HttpBackendClient({
      baseUrl: "http://localhost:8000",
      fetchFn: async () =>
        new Response(new TextEncoder().encode(sseBody), {
          status: 200,
          headers: { "content-type": "text/event-stream" },
        }),
    });
    const events: Array<{ type: string }> = [];
    for await (const event of client.sendChatMessage("chat-abc123", "hello")) {
      events.push(event);
    }
    expect(events[0].type).toBe("intent_classified");
    expect(events[1].type).toBe("chat_done");
  });
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd "/Users/pradeepkumar/projects/AI editor/.worktrees/feat-agentic-planning"
npm run -w @ai-editor/editor-client test
```
Expected: `Property 'listChatThreads' does not exist on type 'HttpBackendClient'`

- [ ] **Step 3: Add Zod schemas and interface methods to `task-contracts.ts`**

Append after the `ScopeDecisionResponse` type export (before `PatchStreamEvent`):

```typescript
// ── Chat types ────────────────────────────────────────────────────────────

export const ChatMessageSchema = z.object({
  role: z.enum(["user", "agent"]),
  content: z.string(),
  type: z.enum(["text", "plan_card", "diff_card", "diff_summary"]).default("text"),
  taskId: z.string().nullable().optional(),
  timestamp: z.string(),
  metadata: z.record(z.unknown()).default({}),
});
export type ChatMessage = z.infer<typeof ChatMessageSchema>;

export const ChatThreadSummarySchema = z.object({
  threadId: z.string(),
  workspacePath: z.string(),
  title: z.string(),
  createdAt: z.string(),
});
export type ChatThreadSummary = z.infer<typeof ChatThreadSummarySchema>;

export const ChatThreadSchema = z.object({
  threadId: z.string(),
  workspacePath: z.string(),
  title: z.string(),
  messages: z.array(ChatMessageSchema),
  touchedFiles: z.array(z.string()),
});
export type ChatThread = z.infer<typeof ChatThreadSchema>;

export const ChatEventSchema = z.object({
  type: z.string(),
  payload: z.record(z.unknown()).default({}),
});
export type ChatEvent = z.infer<typeof ChatEventSchema>;
```

Add to `BackendTaskClient` interface (after `streamPatch`):

```typescript
  streamPatchEvents(taskId: string): AsyncIterable<PatchStreamEvent>;
  listChatThreads(workspacePath: string): Promise<ChatThreadSummary[]>;
  createChatThread(workspacePath: string, title?: string): Promise<ChatThreadSummary>;
  getChatThread(threadId: string): Promise<ChatThread>;
  sendChatMessage(threadId: string, message: string): AsyncIterable<ChatEvent>;
```

- [ ] **Step 4: Implement the five new methods in `http-backend-client.ts`**

Add after the `streamPatch` method:

```typescript
async *streamPatchEvents(taskId: string): AsyncIterable<PatchStreamEvent> {
  // Wraps the callback-based streamPatch as an async iterable for use in
  // contexts that need for-await (e.g., the chat controller).
  const events: PatchStreamEvent[] = [];
  let resolve: (() => void) | null = null;
  let done = false;

  const push = (event: PatchStreamEvent) => {
    events.push(event);
    resolve?.();
    resolve = null;
  };

  const streamPromise = this.streamPatch(taskId, push);
  streamPromise.then(() => { done = true; resolve?.(); }).catch(() => { done = true; resolve?.(); });

  while (true) {
    if (events.length === 0 && !done) {
      await new Promise<void>((r) => { resolve = r; });
    }
    while (events.length > 0) {
      const event = events.shift()!;
      yield event;
      if (event.type === "done") return;
    }
    if (done) return;
  }
}

async listChatThreads(workspacePath: string): Promise<ChatThreadSummary[]> {
  const raw = await this.fetchJson(
    `/v1/chat/threads?workspace=${encodeURIComponent(workspacePath)}`
  ) as Record<string, unknown>;
  const threads = Array.isArray(raw["threads"]) ? raw["threads"] : [];
  return threads.map((t: Record<string, unknown>) =>
    ChatThreadSummarySchema.parse({
      threadId: t["thread_id"],
      workspacePath: t["workspace_path"],
      title: t["title"],
      createdAt: t["created_at"],
    })
  );
}

async createChatThread(workspacePath: string, title = "New Chat"): Promise<ChatThreadSummary> {
  const raw = await this.fetchJson("/v1/chat/threads", {
    method: "POST",
    body: JSON.stringify({ workspace: workspacePath, title }),
  }) as Record<string, unknown>;
  return ChatThreadSummarySchema.parse({
    threadId: raw["thread_id"],
    workspacePath: raw["workspace_path"],
    title: raw["title"],
    createdAt: raw["created_at"],
  });
}

async getChatThread(threadId: string): Promise<ChatThread> {
  const raw = await this.fetchJson(
    `/v1/chat/threads/${encodeURIComponent(threadId)}`
  ) as Record<string, unknown>;
  const messages = Array.isArray(raw["messages"]) ? raw["messages"] : [];
  return ChatThreadSchema.parse({
    threadId: raw["thread_id"],
    workspacePath: raw["workspace_path"],
    title: raw["title"],
    messages: messages.map((m: Record<string, unknown>) => ({
      role: m["role"],
      content: m["content"],
      type: m["type"] ?? "text",
      taskId: m["task_id"] ?? null,
      timestamp: typeof m["timestamp"] === "string"
        ? m["timestamp"]
        : new Date(m["timestamp"] as string).toISOString(),
      metadata: typeof m["metadata"] === "object" && m["metadata"] !== null
        ? m["metadata"]
        : {},
    })),
    touchedFiles: Array.isArray(raw["touched_files"]) ? raw["touched_files"] : [],
  });
}

async *sendChatMessage(threadId: string, message: string): AsyncIterable<ChatEvent> {
  const response = await this.fetchFn(
    `${this.options.baseUrl}/v1/chat/threads/${encodeURIComponent(threadId)}/message`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ content: message }),
    }
  );
  if (!response.ok) {
    throw new Error(`Chat message failed (${response.status}) for thread ${threadId}`);
  }
  if (!response.body) return;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        if (!line.startsWith("data:")) continue;
        try {
          yield ChatEventSchema.parse(JSON.parse(line.slice(5).trim()));
        } catch {
          // skip malformed SSE line
        }
      }
    }
  } finally {
    reader.cancel().catch(() => {});
  }
}
```

Also add the imports at the top of the implementation file:

```typescript
import {
  // ... existing imports ...
  ChatThreadSummarySchema,
  ChatThreadSchema,
  ChatEventSchema,
  type ChatThreadSummary,
  type ChatThread,
  type ChatEvent,
} from "../contracts/task-contracts.js";
```

- [ ] **Step 5: Run tests to confirm they pass**

```bash
npm run -w @ai-editor/editor-client test
npm run -w @ai-editor/editor-client typecheck
```
Expected: all pass, no type errors

- [ ] **Step 6: Commit**

```bash
git add apps/editor-client/src/contracts/task-contracts.ts \
        apps/editor-client/src/client/http-backend-client.ts \
        apps/editor-client/test/http-backend-client.test.ts
git commit -m "feat(chat): editor-client contracts and HTTP methods for chat API"
```

---

## Task 2: ChatPanel WebView

**Files:**
- Create: `apps/vscode-extension/src/chat-panel.ts`

Key corrections from review:
- `show()` takes no `context` param (disposal managed internally via `onDidDispose`)
- Constructor accepts `onNewChat` and `onSwitchThread` callbacks (WebView sends these)

- [ ] **Step 1: Create `apps/vscode-extension/src/chat-panel.ts`**

```typescript
import * as vscode from "vscode";
import type { ChatMessage } from "@ai-editor/editor-client";

export type ChatMessageHandler = (message: string) => Promise<void>;
export type PlanCardActionHandler = (
  taskId: string,
  action: "implement" | "feedback",
  feedback?: string
) => Promise<void>;
export type NewChatHandler = () => Promise<void>;
export type SwitchThreadHandler = (threadId: string) => Promise<void>;

export class ChatPanel {
  private panel: vscode.WebviewPanel | null = null;

  constructor(
    private readonly onMessage: ChatMessageHandler,
    private readonly onPlanAction: PlanCardActionHandler,
    private readonly onNewChat: NewChatHandler,
    private readonly onSwitchThread: SwitchThreadHandler
  ) {}

  show(): void {
    if (this.panel) {
      this.panel.reveal();
      return;
    }
    this.panel = vscode.window.createWebviewPanel(
      "aiEditorChat",
      "AI Editor Chat",
      vscode.ViewColumn.Beside,
      { enableScripts: true, retainContextWhenHidden: true }
    );
    this.panel.webview.html = this.buildHtml();
    this.panel.webview.onDidReceiveMessage(async (msg: unknown) => {
      const m = msg as Record<string, unknown>;
      if (m["type"] === "sendMessage") {
        await this.onMessage(m["text"] as string);
      } else if (m["type"] === "implementPlan") {
        await this.onPlanAction(m["taskId"] as string, "implement");
      } else if (m["type"] === "planFeedback") {
        await this.onPlanAction(m["taskId"] as string, "feedback", m["feedback"] as string);
      } else if (m["type"] === "newChat") {
        await this.onNewChat();
      } else if (m["type"] === "switchThread") {
        await this.onSwitchThread(m["threadId"] as string);
      }
    });
    this.panel.onDidDispose(() => {
      this.panel = null;
    });
  }

  appendMessage(message: ChatMessage): void {
    this.panel?.webview.postMessage({ type: "appendMessage", message });
  }

  appendChunk(chunk: string): void {
    this.panel?.webview.postMessage({ type: "appendChunk", chunk });
  }

  showThinking(message: string): void {
    this.panel?.webview.postMessage({ type: "showThinking", message });
  }

  updateThinking(message: string): void {
    this.panel?.webview.postMessage({ type: "updateThinking", message });
  }

  hideThinking(): void {
    this.panel?.webview.postMessage({ type: "hideThinking" });
  }

  setInputEnabled(enabled: boolean): void {
    this.panel?.webview.postMessage({ type: "setInputEnabled", enabled });
  }

  renderThreadList(threads: Array<{ threadId: string; title: string }>, activeThreadId: string): void {
    this.panel?.webview.postMessage({ type: "renderThreadList", threads, activeThreadId });
  }

  clearThread(): void {
    this.panel?.webview.postMessage({ type: "clearThread" });
  }

  private buildHtml(): string {
    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
  body { font-family: var(--vscode-font-family); margin: 0; display: flex;
         flex-direction: column; height: 100vh; background: var(--vscode-editor-background); }
  #thread-list { border-bottom: 1px solid var(--vscode-panel-border); padding: 6px 10px;
                 display: flex; gap: 6px; align-items: center; overflow-x: auto; flex-shrink: 0; }
  .thread-tab { padding: 3px 10px; border-radius: 4px; cursor: pointer; white-space: nowrap;
                border: 1px solid transparent; font-size: 0.85em; background: none;
                color: var(--vscode-foreground); }
  .thread-tab.active { border-color: var(--vscode-focusBorder);
                       background: var(--vscode-editor-inactiveSelectionBackground); }
  #new-chat-btn { margin-left: auto; padding: 3px 10px; border: none; border-radius: 4px;
                  background: var(--vscode-button-secondaryBackground);
                  color: var(--vscode-button-secondaryForeground); cursor: pointer; font-size: 0.85em; }
  #thread { flex: 1; overflow-y: auto; padding: 12px; display: flex; flex-direction: column; gap: 8px; }
  .msg { max-width: 85%; padding: 8px 12px; border-radius: 8px; white-space: pre-wrap; word-break: break-word; }
  .user { align-self: flex-end; background: var(--vscode-button-background);
          color: var(--vscode-button-foreground); }
  .agent { align-self: flex-start; background: var(--vscode-editor-inactiveSelectionBackground); }
  .thinking { align-self: flex-start; font-size: 0.8em; color: var(--vscode-descriptionForeground);
              font-style: italic; padding: 4px 8px; display: flex; align-items: center; gap: 6px; }
  .thinking-dot { width: 6px; height: 6px; border-radius: 50%;
                  background: var(--vscode-descriptionForeground);
                  animation: pulse 1.2s ease-in-out infinite; }
  @keyframes pulse { 0%,100% { opacity: 0.3; } 50% { opacity: 1; } }
  .plan-card { border: 1px solid var(--vscode-panel-border); border-radius: 6px; padding: 12px;
               align-self: flex-start; max-width: 85%; }
  .plan-card pre { white-space: pre-wrap; margin: 8px 0; font-size: 0.85em; }
  .plan-actions { display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
  .plan-actions button { padding: 6px 14px; border: none; border-radius: 4px; cursor: pointer; }
  .btn-primary { background: var(--vscode-button-background);
                 color: var(--vscode-button-foreground); }
  .btn-secondary { background: var(--vscode-button-secondaryBackground);
                   color: var(--vscode-button-secondaryForeground); }
  .plan-actions textarea { flex: 1; min-width: 140px; padding: 4px;
                           background: var(--vscode-input-background);
                           color: var(--vscode-input-foreground);
                           border: 1px solid var(--vscode-input-border); border-radius: 4px; }
  #input-row { display: flex; gap: 8px; padding: 10px;
               border-top: 1px solid var(--vscode-panel-border); }
  #input { flex: 1; padding: 8px; border: 1px solid var(--vscode-input-border);
           background: var(--vscode-input-background); color: var(--vscode-input-foreground);
           border-radius: 4px; resize: none; font-family: inherit; }
  #send { padding: 8px 16px; background: var(--vscode-button-background);
          color: var(--vscode-button-foreground); border: none; border-radius: 4px; cursor: pointer; }
</style>
</head>
<body>
<div id="thread-list"><button id="new-chat-btn">+ New Chat</button></div>
<div id="thread"></div>
<div id="input-row">
  <textarea id="input" rows="2" placeholder="Ask anything or describe a change…"></textarea>
  <button id="send">Send</button>
</div>
<script>
  const vscode = acquireVsCodeApi();
  const threadEl = document.getElementById('thread');
  const input = document.getElementById('input');
  const sendBtn = document.getElementById('send');
  let currentAgentBubble = null;
  let thinkingEl = null;

  document.getElementById('new-chat-btn').addEventListener('click', () => {
    vscode.postMessage({ type: 'newChat' });
  });

  function send() {
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    vscode.postMessage({ type: 'sendMessage', text });
  }
  sendBtn.addEventListener('click', send);
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });

  function escHtml(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  function showThinking(message) {
    if (thinkingEl) { thinkingEl.querySelector('span').textContent = message; return; }
    thinkingEl = document.createElement('div');
    thinkingEl.className = 'thinking';
    thinkingEl.innerHTML = '<div class="thinking-dot"></div><span>' + escHtml(message) + '</span>';
    threadEl.appendChild(thinkingEl);
    threadEl.scrollTop = threadEl.scrollHeight;
  }
  function updateThinking(message) {
    if (thinkingEl) thinkingEl.querySelector('span').textContent = message;
  }
  function hideThinking() {
    if (thinkingEl) { thinkingEl.remove(); thinkingEl = null; }
  }

  function renderThreadList(threads, activeId) {
    const list = document.getElementById('thread-list');
    list.querySelectorAll('.thread-tab').forEach(el => el.remove());
    const btn = document.getElementById('new-chat-btn');
    threads.forEach(t => {
      const tab = document.createElement('button');
      tab.className = 'thread-tab' + (t.threadId === activeId ? ' active' : '');
      tab.textContent = t.title;
      tab.onclick = () => vscode.postMessage({ type: 'switchThread', threadId: t.threadId });
      list.insertBefore(tab, btn);
    });
  }

  function appendMessage(msg) {
    currentAgentBubble = null;
    if (msg.type === 'plan_card') {
      const taskId = escHtml(msg.metadata && msg.metadata.taskId ? msg.metadata.taskId : '');
      const div = document.createElement('div');
      div.className = 'plan-card';
      div.innerHTML =
        '<strong>Plan</strong><pre>' + escHtml(msg.content) + '</pre>' +
        '<div class="plan-actions">' +
        '<button class="btn-primary" onclick="implementPlan(\'' + taskId + '\')">Implement Plan</button>' +
        '<textarea id="fb-' + taskId + '" placeholder="Give feedback…" rows="2"></textarea>' +
        '<button class="btn-secondary" onclick="sendFeedback(\'' + taskId + '\')">Send Feedback</button>' +
        '</div>';
      threadEl.appendChild(div);
    } else {
      const div = document.createElement('div');
      div.className = 'msg ' + (msg.role === 'user' ? 'user' : 'agent');
      div.textContent = msg.content;
      threadEl.appendChild(div);
    }
    threadEl.scrollTop = threadEl.scrollHeight;
  }

  function appendChunk(chunk) {
    if (!currentAgentBubble) {
      currentAgentBubble = document.createElement('div');
      currentAgentBubble.className = 'msg agent';
      threadEl.appendChild(currentAgentBubble);
    }
    currentAgentBubble.textContent += chunk;
    threadEl.scrollTop = threadEl.scrollHeight;
  }

  function implementPlan(taskId) {
    vscode.postMessage({ type: 'implementPlan', taskId });
  }
  function sendFeedback(taskId) {
    const el = document.getElementById('fb-' + taskId);
    const fb = el ? el.value.trim() : '';
    if (!fb) return;
    vscode.postMessage({ type: 'planFeedback', taskId, feedback: fb });
  }

  window.addEventListener('message', e => {
    const msg = e.data;
    if (msg.type === 'appendMessage') { hideThinking(); appendMessage(msg.message); }
    else if (msg.type === 'appendChunk') { hideThinking(); appendChunk(msg.chunk); }
    else if (msg.type === 'showThinking') showThinking(msg.message);
    else if (msg.type === 'updateThinking') updateThinking(msg.message);
    else if (msg.type === 'hideThinking') hideThinking();
    else if (msg.type === 'setInputEnabled') {
      input.disabled = !msg.enabled;
      sendBtn.disabled = !msg.enabled;
    } else if (msg.type === 'renderThreadList') {
      renderThreadList(msg.threads, msg.activeThreadId);
    } else if (msg.type === 'clearThread') {
      hideThinking();
      threadEl.innerHTML = '';
      currentAgentBubble = null;
    }
  });
</script>
</body>
</html>`;
  }
}
```

- [ ] **Step 2: Verify it compiles**

```bash
npm run -w @ai-editor/vscode-extension typecheck
```
Expected: no errors in `chat-panel.ts`

- [ ] **Step 3: Commit**

```bash
git add apps/vscode-extension/src/chat-panel.ts
git commit -m "feat(chat): ChatPanel WebView with thread list, thinking indicator, plan card"
```

---

## Task 3: Controller Chat Methods

**Files:**
- Modify: `apps/vscode-extension/src/controller.ts`
- Modify: `apps/vscode-extension/test/controller.test.ts`

Key corrections from review:
- No `this.client` field — use `this.createClient(this.settings.getBackendBaseUrl())` inline
- No `this.workspacePath` — use `this.ui.getWorkspacePath() ?? ""`
- `task.modifiedFiles` not `task.result?.modifiedFiles`
- `streamPatchEvents` not `streamPatch` in `streamTaskIntoChatThread`
- Extend `createStubBackend` in tests to include all new `BackendTaskClient` methods

- [ ] **Step 1: Write failing tests**

Add to `apps/vscode-extension/test/controller.test.ts` after the closing brace of the main `describe` block:

```typescript
describe("AiEditorController — chat", () => {
  test("sendChatMessage appends user message and streams agent response", async () => {
    const appendedMessages: Array<{ role: string; content: string }> = [];
    const chunks: string[] = [];

    const chatBackend: BackendTaskClient = {
      ...createStubBackend({
        submitPayloads: [], getTaskCalls: [], acceptCalls: [],
        rejectCalls: [], getResultCalls: [], planFeedbackCalls: [],
      }),
      createChatThread: async () => ({
        threadId: "chat-new",
        workspacePath: "/tmp/workspace",
        title: "New Chat",
        createdAt: "2026-05-11T00:00:00Z",
      }),
      listChatThreads: async () => [],
      getChatThread: async (threadId) => ({
        threadId,
        workspacePath: "/tmp/workspace",
        title: "New Chat",
        messages: [],
        touchedFiles: [],
      }),
      sendChatMessage: async function* () {
        yield { type: "chat_agent_thinking", payload: { message: "Exploring…" } };
        yield { type: "intent_classified", payload: { intent: "qa" } };
        yield { type: "chat_response", payload: { chunk: "The answer is 42." } };
        yield { type: "chat_done", payload: {} };
      },
    };

    const store = new MemorySessionStore();
    const controller = new AiEditorController(
      () => chatBackend,
      store,
      createSettings(),
      createUi({
        appendChatMessage: (m) => appendedMessages.push({ role: m.role, content: m.content }),
        appendChatChunk: (c) => chunks.push(c),
        showChatThinking: () => {},
        updateChatThinking: () => {},
        hideChatThinking: () => {},
        setChatInputEnabled: () => {},
        openChatPanel: () => {},
        renderChatThreadList: () => {},
        clearChatThread: () => {},
      }),
      { openDiff: async () => {} },
      () => "2026-05-11T00:00:00.000Z"
    );

    await controller.sendChatMessage("What is the answer?");
    controller.dispose();

    expect(appendedMessages[0].role).toBe("user");
    expect(appendedMessages[0].content).toBe("What is the answer?");
    expect(chunks).toContain("The answer is 42.");
  });

  test("showChatThinking fires on chat_agent_thinking and updateThinking on explore_tool_call", async () => {
    const thinkingMessages: string[] = [];

    const chatBackend: BackendTaskClient = {
      ...createStubBackend({
        submitPayloads: [], getTaskCalls: [], acceptCalls: [],
        rejectCalls: [], getResultCalls: [], planFeedbackCalls: [],
      }),
      createChatThread: async () => ({
        threadId: "chat-th",
        workspacePath: "/tmp/workspace",
        title: "New Chat",
        createdAt: "2026-05-11T00:00:00Z",
      }),
      listChatThreads: async () => [],
      getChatThread: async (threadId) => ({
        threadId, workspacePath: "/tmp/workspace",
        title: "New Chat", messages: [], touchedFiles: [],
      }),
      sendChatMessage: async function* () {
        yield { type: "chat_agent_thinking", payload: { message: "Exploring workspace…" } };
        yield { type: "explore_tool_call", payload: { tool: "search_code", args: { pattern: "auth" } } };
        yield { type: "intent_classified", payload: { intent: "qa" } };
        yield { type: "chat_response", payload: { chunk: "It handles auth." } };
        yield { type: "chat_done", payload: {} };
      },
    };

    const store = new MemorySessionStore();
    const controller = new AiEditorController(
      () => chatBackend,
      store,
      createSettings(),
      createUi({
        showChatThinking: (m) => thinkingMessages.push(`show:${m}`),
        updateChatThinking: (m) => thinkingMessages.push(`update:${m}`),
        hideChatThinking: () => thinkingMessages.push("hide"),
        appendChatMessage: () => {},
        appendChatChunk: () => {},
        setChatInputEnabled: () => {},
        openChatPanel: () => {},
        renderChatThreadList: () => {},
        clearChatThread: () => {},
      }),
      { openDiff: async () => {} },
      () => "2026-05-11T00:00:00.000Z"
    );

    await controller.sendChatMessage("What does auth do?");
    controller.dispose();

    expect(thinkingMessages[0]).toBe("show:Exploring workspace…");
    expect(thinkingMessages[1]).toBe("update:search_code: auth");
    // final hide fires in the finally block
    expect(thinkingMessages).toContain("hide");
  });
});
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
npm run -w @ai-editor/vscode-extension test
```
Expected: `Property 'sendChatMessage' does not exist on type 'AiEditorController'` and type errors on missing `ControllerUI` methods

- [ ] **Step 3: Add new `ControllerUI` methods**

In `apps/vscode-extension/src/controller.ts`, extend the `ControllerUI` interface:

```typescript
export interface ControllerUI {
  // ... existing methods ...
  openChatPanel(): void;
  appendChatMessage(message: ChatMessage): void;
  appendChatChunk(chunk: string): void;
  showChatThinking(message: string): void;
  updateChatThinking(message: string): void;
  hideChatThinking(): void;
  setChatInputEnabled(enabled: boolean): void;
  renderChatThreadList(threads: ChatThreadSummary[], activeThreadId: string): void;
  clearChatThread(): void;
}
```

Add imports at the top of `controller.ts`:

```typescript
import type {
  // ... existing imports ...
  ChatMessage,
  ChatThreadSummary,
} from "@ai-editor/editor-client";
```

- [ ] **Step 4: Add chat state and methods to `AiEditorController`**

Add private field after existing fields:

```typescript
private activeThreadId: string | null = null;
```

Add the following methods (note: no `this.client` or `this.workspacePath` — use the factory and UI):

```typescript
async openChat(): Promise<void> {
  this.ui.openChatPanel();
  const client = this.createClient(this.settings.getBackendBaseUrl());
  const workspacePath = this.ui.getWorkspacePath() ?? "";
  const threads = await client.listChatThreads(workspacePath);
  if (threads.length === 0) {
    await this.newChatThread();
    return;
  }
  this.ui.renderChatThreadList(threads, threads[0].threadId);
  await this.switchChatThread(threads[0].threadId);
}

async newChatThread(): Promise<void> {
  const client = this.createClient(this.settings.getBackendBaseUrl());
  const workspacePath = this.ui.getWorkspacePath() ?? "";
  const summary = await client.createChatThread(workspacePath);
  this.activeThreadId = summary.threadId;
  const threads = await client.listChatThreads(workspacePath);
  this.ui.renderChatThreadList(threads, summary.threadId);
  this.ui.clearChatThread();
}

async switchChatThread(threadId: string): Promise<void> {
  const client = this.createClient(this.settings.getBackendBaseUrl());
  const thread = await client.getChatThread(threadId);
  this.activeThreadId = threadId;
  this.ui.clearChatThread();
  for (const msg of thread.messages) {
    this.ui.appendChatMessage(msg);
  }
}

async sendChatMessage(message: string): Promise<void> {
  const client = this.createClient(this.settings.getBackendBaseUrl());
  if (!this.activeThreadId) {
    await this.newChatThread();
  }
  this.ui.appendChatMessage({
    role: "user",
    content: message,
    type: "text",
    timestamp: this.now(),
    metadata: {},
  });
  this.ui.setChatInputEnabled(false);
  try {
    for await (const event of client.sendChatMessage(this.activeThreadId!, message)) {
      if (event.type === "chat_agent_thinking") {
        this.ui.showChatThinking((event.payload["message"] as string) ?? "Thinking…");
      } else if (event.type === "explore_tool_call") {
        const tool = event.payload["tool"] as string;
        const args = (event.payload["args"] as Record<string, unknown>) ?? {};
        const hint = args["pattern"] ?? args["path"] ?? args["query"] ?? "";
        this.ui.updateChatThinking(`${tool}${hint ? `: ${hint}` : ""}`);
      } else if (event.type === "intent_classified") {
        this.ui.hideChatThinking();
      } else if (event.type === "chat_response") {
        this.ui.appendChatChunk((event.payload["chunk"] as string) ?? "");
      } else if (event.type === "plan_card") {
        // Emitted in future Plan 2 backend wiring when intent is small/large_change
        this.ui.appendChatMessage({
          role: "agent",
          content: (event.payload["plan_markdown"] as string) ?? "",
          type: "plan_card",
          taskId: (event.payload["task_id"] as string) ?? null,
          timestamp: this.now(),
          metadata: { taskId: event.payload["task_id"] },
        });
      } else if (event.type === "task_created") {
        // Emitted in future Plan 2 backend wiring when intent is small/large_change
        const taskId = event.payload["task_id"] as string;
        await this.streamTaskIntoChatThread(taskId);
      }
    }
  } finally {
    this.ui.hideChatThinking();
    this.ui.setChatInputEnabled(true);
  }
}

async handlePlanCardAction(
  taskId: string,
  action: "implement" | "feedback",
  feedback?: string
): Promise<void> {
  const client = this.createClient(this.settings.getBackendBaseUrl());
  await client.providePlanFeedback(taskId, action === "implement" ? null : (feedback ?? ""));
}

private async streamTaskIntoChatThread(taskId: string): Promise<void> {
  const client = this.createClient(this.settings.getBackendBaseUrl());
  this.ui.showChatThinking("Running task…");
  let planCardShown = false;
  try {
    for await (const event of client.streamPatchEvents(taskId)) {
      if (event.type === "operation_success") {
        const file = (event as Record<string, unknown>)["path"] as string ?? "";
        this.ui.updateChatThinking(`Patching ${file || "files"}…`);
      } else if (event.type === "operation_error") {
        const file = (event as Record<string, unknown>)["path"] as string ?? "";
        this.ui.updateChatThinking(`Error in ${file || "patch"} — see review panel`);
      } else if (event.type === "done") {
        break;
      }
    }
  } finally {
    this.ui.hideChatThinking();
  }
  // Race guard: fetch final task state and show plan card or diff summary
  const task = await client.getTask(taskId);
  if (!planCardShown && task.status === "AWAITING_PLAN_APPROVAL" && task.planMarkdown) {
    this.ui.appendChatMessage({
      role: "agent",
      content: task.planMarkdown,
      type: "plan_card",
      taskId,
      timestamp: this.now(),
      metadata: { taskId },
    });
    return;
  }
  // task.modifiedFiles is directly on TaskView (not task.result?.modifiedFiles)
  const filesChanged = task.modifiedFiles.length;
  this.ui.appendChatMessage({
    role: "agent",
    content: `Done — ${filesChanged} file${filesChanged !== 1 ? "s" : ""} changed. Review and accept in the panel.`,
    type: "diff_summary",
    taskId,
    timestamp: this.now(),
    metadata: { taskId },
  });
}
```

- [ ] **Step 5: Update `createStubBackend` in `controller.test.ts` to include new interface methods**

Add to the object returned by `createStubBackend`:

```typescript
    streamPatchEvents: async function* (_taskId: string) {
      yield { type: "done" as const };
    },
    listChatThreads: async () => [],
    createChatThread: async (_workspacePath: string, _title?: string) => ({
      threadId: "chat-stub",
      workspacePath: _workspacePath,
      title: _title ?? "New Chat",
      createdAt: "2026-01-01T00:00:00Z",
    }),
    getChatThread: async (threadId: string) => ({
      threadId,
      workspacePath: "/tmp/workspace",
      title: "New Chat",
      messages: [],
      touchedFiles: [],
    }),
    sendChatMessage: async function* (_threadId: string, _message: string) {
      yield { type: "chat_done", payload: {} };
    },
```

Also add missing `ControllerUI` methods to `createUi`:

```typescript
function createUi(overrides?: Partial<ControllerUI>): ControllerUI {
  return {
    // ... existing methods ...
    openChatPanel: () => {},
    appendChatMessage: () => {},
    appendChatChunk: () => {},
    showChatThinking: () => {},
    updateChatThinking: () => {},
    hideChatThinking: () => {},
    setChatInputEnabled: () => {},
    renderChatThreadList: () => {},
    clearChatThread: () => {},
    ...overrides,
  };
}
```

- [ ] **Step 6: Run tests**

```bash
npm run -w @ai-editor/vscode-extension test
npm run -w @ai-editor/vscode-extension typecheck
```
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add apps/vscode-extension/src/controller.ts apps/vscode-extension/test/controller.test.ts
git commit -m "feat(chat): controller chat methods — openChat, sendChatMessage, streamTaskIntoChatThread"
```

---

## Task 4: Wire ChatPanel into Extension

**Files:**
- Modify: `apps/vscode-extension/src/extension.ts`
- Modify: `apps/vscode-extension/package.json`

Key corrections from review:
- `ChatPanel` import goes at the top of the file, not inside `activate()`
- All 9 new `ControllerUI` methods must be explicitly wired — no `// ... existing methods ...` placeholder
- `inline_diff_ready` handler marked as TODO (requires virtual document provider)

- [ ] **Step 1: Add `ChatPanel` import to the top of `extension.ts`**

```typescript
import { ChatPanel } from "./chat-panel.js";
```

- [ ] **Step 2: Wire `ChatPanel` and the `aiEditor.openChat` command in `activate()`**

After the `panel` (`ReviewPanel`) construction and before the `ui` object, add:

```typescript
const chatPanel = new ChatPanel(
  async (message) => { await controller.sendChatMessage(message); },
  async (taskId, action, feedback) => { await controller.handlePlanCardAction(taskId, action, feedback); },
  async () => { await controller.newChatThread(); },
  async (threadId) => { await controller.switchChatThread(threadId); }
);
```

- [ ] **Step 3: Replace the `ui` object to include all 9 new `ControllerUI` methods**

The full `ui` object must now include ALL new methods. Replace the existing `ui` declaration with:

```typescript
const ui: ControllerUI = {
  getWorkspacePath: () => vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? null,
  promptForGoal: () =>
    vscode.window.showInputBox({
      prompt: "Describe what you want AI Editor to do",
      placeHolder: "Example: Refactor auth middleware to support refresh tokens",
      ignoreFocusOut: true,
    }),
  promptForRejectReason: () =>
    vscode.window.showInputBox({
      prompt: "Why are you rejecting this patch?",
      value: "Needs revision",
      ignoreFocusOut: true,
    }),
  showInfo: (message) => { void vscode.window.showInformationMessage(message); },
  showWarning: (message) => { void vscode.window.showWarningMessage(message); },
  showError: (message) => { void vscode.window.showErrorMessage(message); },
  updatePanel: (model) => { panel.update(model); },
  promptForResumeStage: () =>
    vscode.window.showQuickPick(
      ["plan", "feedback", "execute"] as const,
      { placeHolder: "Select stage to resume from" },
    ) as Promise<"plan" | "feedback" | "execute" | undefined>,
  promptForMaxIterationsOverride: async () => {
    const value = await vscode.window.showInputBox({
      prompt: "Override max iterations? (leave blank to keep current)",
      placeHolder: "e.g. 10",
      validateInput: (v) =>
        v === "" || /^\d+$/.test(v) ? null : "Enter a positive integer or leave blank",
    });
    return value === "" || value === undefined ? undefined : parseInt(value, 10);
  },
  promptForTaskId: () =>
    vscode.window.showInputBox({
      prompt: "Enter the task ID to attach to",
      placeHolder: "task-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
      ignoreFocusOut: true,
    }),
  promptForScopeDecision: async ({ files, reason, stepId }) => {
    const fileList = files.length === 1 ? files[0] : `${files.length} files (${files.join(", ")})`;
    const choice = await vscode.window.showInformationMessage(
      `[Step ${stepId}] Agent wants to also modify ${fileList}.\n\nReason: ${reason}`,
      { modal: true },
      "Approve",
      "Approve & Remember",
      "Reject"
    );
    if (!choice) return undefined;
    return {
      decision: choice.startsWith("Approve") ? "approve" : "reject",
      remember: choice === "Approve & Remember",
    };
  },
  // Chat panel methods
  openChatPanel: () => chatPanel.show(),
  appendChatMessage: (msg) => chatPanel.appendMessage(msg),
  appendChatChunk: (chunk) => chatPanel.appendChunk(chunk),
  showChatThinking: (message) => chatPanel.showThinking(message),
  updateChatThinking: (message) => chatPanel.updateThinking(message),
  hideChatThinking: () => chatPanel.hideThinking(),
  setChatInputEnabled: (enabled) => chatPanel.setInputEnabled(enabled),
  renderChatThreadList: (threads, activeThreadId) =>
    chatPanel.renderThreadList(threads, activeThreadId),
  clearChatThread: () => chatPanel.clearThread(),
};
```

- [ ] **Step 4: Register `aiEditor.openChat` command**

After the existing command registrations:

```typescript
context.subscriptions.push(
  vscode.commands.registerCommand("aiEditor.openChat", async () => {
    await controller.openChat();
  })
);
```

- [ ] **Step 5: Add command to `package.json`**

In `apps/vscode-extension/package.json` under `contributes.commands`:

```json
{
  "command": "aiEditor.openChat",
  "title": "AI Editor: Open Chat"
}
```

- [ ] **Step 6: Note on `inline_diff_ready` (TODO — deferred)**

The `inline_diff_ready` event (for small-change inline diffs) is not emitted by the current backend. When the backend adds it, the controller's `sendChatMessage` event loop should handle it. Using `vscode.Uri.parse("untitled:...")` does not work for pre-populated content — a `TextDocumentContentProvider` registered with a custom URI scheme is needed. Leave this as a TODO comment in `sendChatMessage`:

```typescript
// TODO: handle "inline_diff_ready" event when backend emits it.
// Requires a virtual TextDocumentContentProvider (untitled: URIs cannot
// be pre-populated via WorkspaceEdit). Implement when Plan 2 backend lands.
```

- [ ] **Step 7: Build and verify**

```bash
npm run build
npm run typecheck
```
Expected: clean build across all workspaces

- [ ] **Step 8: Commit**

```bash
git add apps/vscode-extension/src/extension.ts apps/vscode-extension/package.json
git commit -m "feat(chat): wire ChatPanel into extension — openChat command, full ControllerUI wiring"
```

---

## Verification

- [ ] Start backend with turboquant or gemini, open the extension dev host

```bash
npm run build
code --extensionDevelopmentPath="$PWD/apps/vscode-extension" "$PWD/workspaces/shadow-forge-stress"
```

- [ ] Run `AI Editor: Open Chat` from command palette (`Cmd+Shift+P`)

Expected: chat panel opens in a side column, "+ New Chat" button visible

- [ ] Type a question ("What does this project do?")

Expected:
1. User bubble appears immediately
2. Thinking indicator shows "Exploring workspace…" then tool name updates
3. Thinking hides on `intent_classified`
4. Agent response streams in as text chunks
5. Input re-enables when `chat_done` arrives

- [ ] Type another question in the same thread; verify history is preserved and the thread tab shows in the header

- [ ] Click "+ New Chat"; verify a fresh thread is created, panel clears, thread tab list updates
