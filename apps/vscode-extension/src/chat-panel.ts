import * as vscode from "vscode";
import type { ChatMessage, ChatThreadSummary, CommandDecision } from "@ai-editor/editor-client";

export type ChatMessageHandler = (message: string) => Promise<void>;
export type PlanCardActionHandler = (
  taskId: string,
  action: "implement" | "feedback",
  feedback?: string
) => Promise<void>;
export type InlineChangeActionHandler = (taskId: string) => Promise<void>;
export type ViewDiffFileHandler = (relativePath: string, shadowPath: string) => Promise<void>;
export type NewChatHandler = () => Promise<void>;
export type SwitchThreadHandler = (threadId: string) => Promise<void>;
export type ScopeDecisionHandler = (taskId: string, files: string[], decision: "approve" | "reject", remember: boolean) => Promise<void>;
export type ValidationDecisionHandler = (taskId: string, decision: "accept" | "reject") => Promise<void>;
export type CommandDecisionHandler = (taskId: string, decision: CommandDecision) => Promise<void>;

export class ChatPanel {
  private panel: vscode.WebviewPanel | null = null;

  constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly onMessage: ChatMessageHandler,
    private readonly onPlanAction: PlanCardActionHandler,
    private readonly onNewChat: NewChatHandler,
    private readonly onSwitchThread: SwitchThreadHandler,
    private readonly onApplyInlineChange: InlineChangeActionHandler,
    private readonly onDiscardInlineChange: InlineChangeActionHandler,
    private readonly onViewDiffFile: ViewDiffFileHandler,
    private readonly onScopeDecision: ScopeDecisionHandler,
    private readonly onValidationDecision: ValidationDecisionHandler,
    private readonly onCommandDecision: CommandDecisionHandler,
    private readonly onReady: () => Promise<void> = async () => {}
  ) {}

  /** Called by the webview serializer when VS Code restores a persisted panel. */
  reattach(restoredPanel: vscode.WebviewPanel): void {
    this.panel = restoredPanel;
    this.panel.webview.html = this.buildHtml();
    this.registerHandlers();
  }

  show(): void {
    if (this.panel) {
      this.panel.reveal();
      return;
    }
    this.panel = vscode.window.createWebviewPanel(
      "aiEditorChat",
      "AI Editor Chat",
      vscode.ViewColumn.Two,
      {
        enableScripts: true,
        localResourceRoots: [vscode.Uri.joinPath(this.extensionUri, "media")],
      }
    );
    this.panel.webview.html = this.buildHtml();
    this.registerHandlers();
  }

  private registerHandlers(): void {
    if (!this.panel) return;
    this.panel.webview.onDidReceiveMessage((msg: unknown) => {
      const m = msg as Record<string, unknown>;
      let p: Promise<void>;
      if (m["type"] === "webviewReady") {
        p = this.onReady();
      } else if (m["type"] === "sendMessage") {
        p = this.onMessage(m["text"] as string);
      } else if (m["type"] === "implementPlan") {
        p = this.onPlanAction(m["taskId"] as string, "implement");
      } else if (m["type"] === "planFeedback") {
        p = this.onPlanAction(m["taskId"] as string, "feedback", m["feedback"] as string);
      } else if (m["type"] === "newChat") {
        p = this.onNewChat();
      } else if (m["type"] === "switchThread") {
        p = this.onSwitchThread(m["threadId"] as string);
      } else if (m["type"] === "applyInlineChange") {
        p = this.onApplyInlineChange(m["taskId"] as string);
      } else if (m["type"] === "discardInlineChange") {
        p = this.onDiscardInlineChange(m["taskId"] as string);
      } else if (m["type"] === "viewDiffFile") {
        p = this.onViewDiffFile(m["path"] as string, m["shadowPath"] as string ?? "");
      } else if (m["type"] === "scopeDecision") {
        const files = Array.isArray(m["files"]) ? m["files"] as string[] : [];
        const decision = m["decision"] === "approve" ? "approve" : "reject";
        const remember = m["remember"] === true;
        p = this.onScopeDecision(m["taskId"] as string, files, decision, remember);
      } else if (m["type"] === "validationDecision") {
        const decision = m["decision"] === "accept" ? "accept" : "reject";
        p = this.onValidationDecision(m["taskId"] as string, decision);
      } else if (m["type"] === "commandDecision") {
        const decision: CommandDecision = {
          approve: m["approve"] === true,
          remember: m["remember"] === true,
          scope: (m["scope"] === "prefix" || m["scope"] === "binary") ? m["scope"] : "exact",
          ruleValue: typeof m["ruleValue"] === "string" ? (m["ruleValue"] as string) : undefined,
        };
        p = this.onCommandDecision(m["taskId"] as string, decision);
      } else {
        return;
      }
      p.catch((err: unknown) => {
        const message = err instanceof Error ? err.message : String(err);
        this.panel?.webview.postMessage({ type: "setInputEnabled", enabled: true });
        vscode.window.showErrorMessage(`Chat error: ${message}`);
      });
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

  renderThreadList(
    threads: Array<Pick<ChatThreadSummary, "threadId" | "title">>,
    activeThreadId: string
  ): void {
    this.panel?.webview.postMessage({ type: "renderThreadList", threads, activeThreadId });
  }

  clearThread(): void {
    this.panel?.webview.postMessage({ type: "clearThread" });
  }

  resolveInlineChangeCard(taskId: string, resolution: "applied" | "discarded"): void {
    this.panel?.webview.postMessage({ type: "resolveInlineChangeCard", taskId, resolution });
  }

  updateThreadTitle(threadId: string, title: string): void {
    this.panel?.webview.postMessage({ type: "thread_title_updated", payload: { thread_id: threadId, title } });
  }

  appendThinkingEntry(text: string): void {
    this.panel?.webview.postMessage({ type: "appendThinkingEntry", text });
  }

  appendThinkingChunk(chunk: string): void {
    this.panel?.webview.postMessage({ type: "appendThinkingChunk", chunk });
  }

  finalizeAgentMessage(): void {
    this.panel?.webview.postMessage({ type: "finalizeAgentMessage" });
  }

  private buildHtml(): string {
    const scriptUri = this.panel!.webview.asWebviewUri(
      vscode.Uri.joinPath(this.extensionUri, "media", "chat.js")
    );
    const markedUri = this.panel!.webview.asWebviewUri(
      vscode.Uri.joinPath(this.extensionUri, "media", "marked.umd.js")
    );
    const nonce = Array.from({ length: 16 }, () =>
      Math.floor(Math.random() * 256).toString(16).padStart(2, "0")
    ).join("");
    const cspSource = this.panel!.webview.cspSource;
    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline' ${cspSource}; script-src 'nonce-${nonce}' ${cspSource};">
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
  .plan-card .plan-md { margin: 8px 0; font-size: 0.88em; line-height: 1.6; }
  .plan-card .plan-md p { margin: 4px 0; }
  .plan-card .plan-md ul, .plan-card .plan-md ol { margin: 4px 0; padding-left: 20px; }
  .plan-card .plan-md li { margin: 2px 0; }
  .plan-card .plan-md h1, .plan-card .plan-md h2, .plan-card .plan-md h3 { margin: 8px 0 4px; font-size: 1em; }
  .plan-card .plan-md code { background: var(--vscode-textCodeBlock-background); padding: 1px 4px; border-radius: 3px; font-family: var(--vscode-editor-font-family, monospace); font-size: 0.9em; }
  .plan-card .plan-md pre { background: var(--vscode-textCodeBlock-background); padding: 8px; border-radius: 4px; overflow-x: auto; margin: 6px 0; }
  .plan-card .plan-md pre code { background: none; padding: 0; }
  .plan-card .plan-md strong { font-weight: 600; }
  .plan-card .plan-md hr { border: none; border-top: 1px solid var(--vscode-panel-border); margin: 8px 0; }
  .plan-actions { display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
  .plan-actions button { padding: 6px 14px; border: none; border-radius: 4px; cursor: pointer; }
  .btn-primary { background: var(--vscode-button-background); color: var(--vscode-button-foreground); }
  .btn-secondary { background: var(--vscode-button-secondaryBackground);
                   color: var(--vscode-button-secondaryForeground); }
  .plan-actions textarea { flex: 1; min-width: 140px; padding: 4px;
                           background: var(--vscode-input-background);
                           color: var(--vscode-input-foreground);
                           border: 1px solid var(--vscode-input-border); border-radius: 4px; }
  .diff-files { margin: 8px 0; font-size: 0.85em; line-height: 1.6; }
  .diff-file { font-family: var(--vscode-editor-font-family, monospace); }
  .diff-adds { color: var(--vscode-gitDecoration-addedResourceForeground, #73c991); }
  .diff-dels { color: var(--vscode-gitDecoration-deletedResourceForeground, #f14c4c); }
  .diff-view-btns { display: flex; gap: 6px; flex-wrap: wrap; width: 100%; margin-top: 4px; }
  .btn-ghost { background: transparent; color: var(--vscode-textLink-foreground);
               border: 1px solid var(--vscode-textLink-foreground) !important;
               font-size: 0.82em; padding: 3px 8px !important; }
  .inline-resolved { font-size: 0.85em; opacity: 0.7; font-style: italic; }
  .thinking-log { margin-top: 8px; font-size: 0.8em; color: var(--vscode-descriptionForeground); }
  .thinking-log summary { cursor: pointer; user-select: none; opacity: 0.7; }
  .thinking-log summary:hover { opacity: 1; }
  .thinking-log ul { margin: 4px 0 0 0; padding-left: 16px; line-height: 1.6; }
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
<script nonce="${nonce}" src="${markedUri}"></script>
<script nonce="${nonce}" src="${scriptUri}"></script>
</body>
</html>`;
  }
}
