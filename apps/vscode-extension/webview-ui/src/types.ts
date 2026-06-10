// ── Wire shape of a persisted chat message (mirrors editor-client ChatMessageSchema).
// EVERY message has role + type; cards are discriminated by `type`, not by `role`.
export interface ChatMsg {
  role: "user" | "agent";
  content: string;
  type: "text" | "plan_card" | "diff_card" | "diff_summary" | "task_card"
      | "scope_card" | "validation_card" | "command_card";
  taskId?: string | null;
  timestamp: string;
  metadata: Record<string, unknown>;
}

// Diff entries arrive snake_case (SSE + /live payloads are not case-mapped).
export interface DiffEntry {
  path: string;
  additions: number;
  deletions: number;
  temp_path?: string;
}

export interface Diagnostic { level: string; message: string; source?: string }

export interface ThreadSummary {
  threadId: string;
  title: string;
  createdAt: string;
}

// ── Structured tool events ────────────────────────────────────────────────────
export interface ToolEventView {
  id: number;                 // monotonically increasing per turn (extension-assigned)
  tool: string;
  args: Record<string, unknown>;
  thought?: string;
  source: "explore" | "execution" | "planning";
  output?: string;            // filled by the matching toolResult
  isError?: boolean;
  done: boolean;
}

// ── Live slot views ──────────────────────────────────────────────────────────
export interface LiveGateView {
  kind: "command" | "scope" | "validation" | "step";
  taskId: string;
  payload: Record<string, unknown>;  // pending_* payload, snake_case
}

export interface LivePlanView { taskId: string; planMarkdown: string }

export interface LiveReviewView {
  taskId: string;
  modifiedFiles: string[];
  shadowWorkspacePath: string | null;
  // run summary: derived from result.plan + extension-observed events
  stepsCompleted: number | null;
  stepsTotal: number | null;
  deviations: string[];
}

export interface LiveErrorView {
  taskId: string;
  status: "FAILED" | "ABORTED";
  detail?: string;
}

export interface WorkbarInfo {
  stepIndex?: number;       // tier 1 — step progress
  totalSteps?: number;
  stepTitle?: string;
  phaseLabel?: string;      // tier 2 — transient event override
}

// ── Extension → Webview ──────────────────────────────────────────────────────
export type ExtensionMessage =
  | { type: "appendMessage"; message: ChatMsg }
  | { type: "appendChunk"; chunk: string }
  | { type: "appendThinkingEntry"; text: string }
  | { type: "appendThinkingChunk"; chunk: string }
  | { type: "appendToolEvent"; event: Omit<ToolEventView, "output" | "isError" | "done"> }
  | { type: "appendToolResult"; id: number; output: string; isError: boolean }
  | { type: "updateWorkbar"; info: WorkbarInfo | null }
  | { type: "finalizeAgentMessage" }
  | { type: "showThinking"; message: string }
  | { type: "updateThinking"; message: string }
  | { type: "hideThinking" }
  | { type: "setInputEnabled"; enabled: boolean }
  | { type: "renderThreadList"; threads: ThreadSummary[]; activeThreadId: string }
  | { type: "clearThread" }
  | { type: "renderLiveGate"; gate: LiveGateView }
  | { type: "clearLiveGate" }
  | { type: "renderLivePlan"; plan: LivePlanView }
  | { type: "clearLivePlan" }
  | { type: "renderLiveReview"; review: LiveReviewView }
  | { type: "clearLiveReview" }
  | { type: "renderLiveError"; error: LiveErrorView }
  | { type: "clearLiveError" }
  | { type: "liveStatus"; status: string | null }
  | { type: "resolveInlineChangeCard"; taskId: string; resolution: "applied" | "discarded" }
  | { type: "thread_title_updated"; payload: { thread_id: string; title: string } };

// ── Webview → Extension ──────────────────────────────────────────────────────
export type WebviewMessage =
  | { type: "webviewReady" }
  | { type: "sendMessage"; text: string }
  | { type: "implementPlan"; taskId: string }
  | { type: "planFeedback"; taskId: string; feedback: string }
  | { type: "newChat" }
  | { type: "switchThread"; threadId: string }
  | { type: "applyInlineChange"; taskId: string }
  | { type: "discardInlineChange"; taskId: string }
  | { type: "viewDiffFile"; path: string; shadowPath: string }
  | { type: "scopeDecision"; taskId: string; files: string[]; decision: "approve" | "reject"; remember: boolean }
  | { type: "validationDecision"; taskId: string; decision: "accept" | "reject" }
  | { type: "commandDecision"; taskId: string; approve: boolean; remember?: boolean; scope?: string; ruleValue?: string }
  | { type: "stepDecision"; taskId: string; decision: "accept" | "discard" }
  | { type: "acceptTask"; taskId: string }
  | { type: "rejectTask"; taskId: string; reason: string }
  | { type: "resumeTask"; taskId: string; stage: "plan" | "execute" }
  | { type: "stopTurn" };

// ── App state ─────────────────────────────────────────────────────────────────
export interface StreamingBubble {
  text: string;
  thinkingEntries: string[];
  activeThinkingChunk: string;
  toolEvents: ToolEventView[];
}

export interface AppState {
  view: "history" | "thread";
  threads: ThreadSummary[];
  activeThreadId: string;
  messages: ChatMsg[];
  streaming: StreamingBubble | null;
  thinkingStatus: string | null;
  inputEnabled: boolean;
  liveGate: LiveGateView | null;
  livePlan: LivePlanView | null;
  liveReview: LiveReviewView | null;
  liveError: LiveErrorView | null;
  workbar: WorkbarInfo | null;
  liveStatus: string | null;
}
