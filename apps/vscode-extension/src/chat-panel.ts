import * as fs from "node:fs";
import * as vscode from "vscode";
import type { ChatMessage, ChatThreadSummary, CommandDecision } from "@ai-editor/editor-client";
import type { LiveGateView, LivePlanView } from "./controller.js";

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
export type StepDecisionHandler = (taskId: string, decision: "accept" | "discard") => Promise<void>;
export type AcceptTaskHandler = (taskId: string) => Promise<void>;
export type RejectTaskHandler = (taskId: string, reason: string) => Promise<void>;
export type ResumeTaskHandler = (taskId: string, stage: "plan" | "execute") => Promise<void>;
export type StopTurnHandler = () => void;

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
    private readonly onStepDecision: StepDecisionHandler,
    private readonly onAcceptTask: AcceptTaskHandler,
    private readonly onRejectTask: RejectTaskHandler,
    private readonly onResumeTask: ResumeTaskHandler,
    private readonly onStopTurn: StopTurnHandler,
    private readonly onReady: () => Promise<void> = async () => {}
  ) {}

  /** Called by the webview serializer when VS Code restores a persisted panel. */
  reattach(restoredPanel: vscode.WebviewPanel): void {
    this.panel = restoredPanel;
    // Restored panels keep serialized options — reset before building html so
    // the CSP and localResourceRoots are correct for the new build path.
    this.panel.webview.options = {
      enableScripts: true,
      localResourceRoots: [
        vscode.Uri.joinPath(this.extensionUri, "media"),
        vscode.Uri.joinPath(this.extensionUri, "webview-ui", "dist"),
      ],
    };
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
        localResourceRoots: [
          vscode.Uri.joinPath(this.extensionUri, "media"),
          vscode.Uri.joinPath(this.extensionUri, "webview-ui", "dist"),
        ],
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
      } else if (m["type"] === "stepDecision") {
        const decision = m["decision"] === "accept" ? "accept" : "discard";
        p = this.onStepDecision(m["taskId"] as string, decision);
      } else if (m["type"] === "acceptTask") {
        p = this.onAcceptTask(m["taskId"] as string);
      } else if (m["type"] === "rejectTask") {
        p = this.onRejectTask(m["taskId"] as string, (m["reason"] as string) ?? "");
      } else if (m["type"] === "resumeTask") {
        const stage = m["stage"] === "plan" ? "plan" : "execute";
        p = this.onResumeTask(m["taskId"] as string, stage);
      } else if (m["type"] === "stopTurn") {
        this.onStopTurn();
        return;
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

  // Live, state-driven cards (Class A). The webview keeps a single slot per kind and
  // replaces in place, so these are safe to call every poll tick.
  renderLiveGate(gate: LiveGateView): void {
    this.panel?.webview.postMessage({ type: "renderLiveGate", gate });
  }

  clearLiveGate(): void {
    this.panel?.webview.postMessage({ type: "clearLiveGate" });
  }

  renderLivePlan(plan: LivePlanView): void {
    this.panel?.webview.postMessage({ type: "renderLivePlan", plan });
  }

  clearLivePlan(): void {
    this.panel?.webview.postMessage({ type: "clearLivePlan" });
  }

  renderLiveReview(review: { taskId: string; modifiedFiles: string[]; shadowWorkspacePath: string | null; stepsCompleted: number | null; stepsTotal: number | null; deviations: string[] }): void {
    this.panel?.webview.postMessage({ type: "renderLiveReview", review });
  }

  clearLiveReview(): void {
    this.panel?.webview.postMessage({ type: "clearLiveReview" });
  }

  renderLiveError(error: { taskId: string; status: "FAILED" | "ABORTED"; detail?: string }): void {
    this.panel?.webview.postMessage({ type: "renderLiveError", error });
  }

  clearLiveError(): void {
    this.panel?.webview.postMessage({ type: "clearLiveError" });
  }

  sendLiveStatus(status: string | null): void {
    this.panel?.webview.postMessage({ type: "liveStatus", status });
  }

  appendToolEvent(event: { id: number; tool: string; args: Record<string, unknown>; thought?: string; source: "explore" | "execution" | "planning" }): void {
    this.panel?.webview.postMessage({ type: "appendToolEvent", event });
  }

  appendToolResult(id: number, output: string, isError: boolean): void {
    this.panel?.webview.postMessage({ type: "appendToolResult", id, output, isError });
  }

  updateWorkbar(info: { stepIndex?: number; totalSteps?: number; stepTitle?: string; phaseLabel?: string } | null): void {
    this.panel?.webview.postMessage({ type: "updateWorkbar", info });
  }

  private buildHtml(): string {
    const distPath = vscode.Uri.joinPath(this.extensionUri, "webview-ui", "dist");
    let html = fs.readFileSync(vscode.Uri.joinPath(distPath, "index.html").fsPath, "utf8");

    const nonce = Array.from({ length: 16 }, () =>
      Math.floor(Math.random() * 256).toString(16).padStart(2, "0")
    ).join("");
    const cspSource = this.panel!.webview.cspSource;

    // Vite emits relative refs (base "./"): src="./assets/index.js" href="./assets/index.css"
    html = html.replace(/(src|href)="\.\/(assets\/[^"]+)"/g, (_m, attr: string, assetPath: string) => {
      const uri = this.panel!.webview.asWebviewUri(vscode.Uri.joinPath(distPath, assetPath));
      return `${attr}="${uri}"`;
    });
    html = html.replace(/<script /g, `<script nonce="${nonce}" `);
    html = html.replace(
      "<head>",
      `<head>\n<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline' ${cspSource}; script-src 'nonce-${nonce}' ${cspSource}; img-src ${cspSource} data:; font-src ${cspSource};">`
    );
    return html;
  }
}
