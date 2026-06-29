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
  /** Internal client-side annotation (NOT on the wire): plan-card version signature used for dedup. */
  _sig?: string;
}

// Diff entries arrive snake_case (SSE + /live payloads are not case-mapped).
export interface DiffEntry {
  path: string;
  additions: number;
  deletions: number;
  temp_path?: string;
  // Capped unified diff text (snake_case — SSE/live payloads and persisted
  // metadata are not case-mapped). Absent on pre-v2 messages → FileRow fallback.
  unified_diff?: string;
}

export interface Diagnostic {
  /* backend sends arbitrary level strings (pydantic str) — do not narrow to a literal union */
  level: string;
  message: string;
  source?: string;
}

export interface ThreadSummary {
  threadId: string;
  title: string;
  createdAt: string;
  updatedAt?: string;
  messageCount?: number;
  status?: "running" | "review" | "done" | "failed" | null;
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
  kind: "command" | "scope" | "validation" | "step" | "mode" | "edit" | "clarify";
  taskId: string;
  payload: Record<string, unknown>;  // pending_* payload, snake_case
}

export interface LivePlanView { taskId: string; planMarkdown: string }

export interface TodoItem {
  title: string;
  status: "pending" | "in_progress" | "done" | "blocked" | "cancelled";
  note: string;
}
export interface LiveTodosView { items: TodoItem[] }

// LLM-authored run narrative (headline + points), shown on the Review/Error cards.
export interface TaskNarrativeView {
  headline: string;
  points: string[];
}

export interface LiveReviewView {
  taskId: string;
  modifiedFiles: string[];
  shadowWorkspacePath: string | null;
  // run summary: derived from result.plan + extension-observed events
  stepsCompleted: number | null;
  stepsTotal: number | null;
  deviations: string[];
  narrative?: TaskNarrativeView;
}

export interface LiveErrorView {
  taskId: string;
  status: "FAILED" | "ABORTED";
  detail?: string;
  narrative?: TaskNarrativeView;
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
  | { type: "renderLiveTodos"; todos: LiveTodosView }
  | { type: "clearLiveTodos" }
  | { type: "liveStatus"; status: string | null; turnActive?: boolean }
  | { type: "resolveInlineChangeCard"; taskId: string; resolution: "applied" | "discarded" }
  | { type: "thread_title_updated"; payload: { thread_id: string; title: string } }
  // P1: prompt-file expansion replies from the host
  | { type: "promptList"; names: string[] }
  | { type: "promptExpanded"; name: string; found: boolean; text: string };

// ── Webview → Extension ──────────────────────────────────────────────────────
export type WebviewMessage =
  | { type: "webviewReady" }
  | { type: "sendMessage"; text: string; stepReview?: boolean }
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
  // Agentic chat controller: mode-recommendation gate pick + per-edit review decision
  | { type: "modeDecision"; threadId: string; mode: string }
  | { type: "clarifyDecision"; threadId: string; answer: string }
  | { type: "editDecision"; threadId: string; decision: "accept" | "reject"; reason: string }
  | { type: "acceptTask"; taskId: string }
  | { type: "rejectTask"; taskId: string; reason: string }
  | { type: "resumeTask"; taskId: string; stage: "plan" | "execute" }
  | { type: "stopTurn" }
  // Tier B: cooperative Stop for a running task (revert rolls back vs keeps changes)
  | { type: "abortTask"; revert: boolean }
  // Tier B: live-mutable "Review each step" preference for the running task
  | { type: "setReviewPref"; autoAccept: boolean }
  // P1: prompt-file (.ai-editor/prompts/<name>.md) listing + expand-before-send
  | { type: "listPrompts" }
  | { type: "expandPrompt"; name: string; args: string };

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
  liveTodos: LiveTodosView | null;
  workbar: WorkbarInfo | null;
  liveStatus: string | null;
  // True while a controller turn / held-open controller gate is in flight (durable
  // input-disable signal from /live; survives reload). Distinct from inputEnabled,
  // which is the ephemeral per-turn flag a fresh webview mounts as `true`.
  turnActive: boolean;
}
