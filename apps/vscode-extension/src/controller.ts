import type {
  BackendTaskClient,
  ChatMessage,
  ChatThreadSummary,
  CommandDecision,
  StreamEvent,
  ResumeTaskResponse,
  TaskResult,
  TaskStatus,
  TaskSubmission,
  TaskView,
  ThreadLiveState,
} from "@ai-editor/editor-client";

import * as path from "path";
import type { MemoryDataSource } from "./memory-data.js";
import { listPromptNames, loadPromptBody, substitutePrompt } from "./prompt-files.js";
import { buildReviewFileEntries } from "./review-files.js";
import { SessionStore } from "./session-store.js";
import { shouldStopPolling, TaskPoller } from "./task-poller.js";
import type {
  ReviewFileEntry,
  TaskMode,
  TaskSessionState,
} from "./types.js";

const TERMINAL_STATUSES: ReadonlySet<TaskStatus> = new Set(["SUCCEEDED", "FAILED", "ABORTED"]);

export interface SettingsProvider {
  getBackendBaseUrl(): string;
  getDefaultMode(): TaskMode;
  getPollIntervalMs(): number;
}

export interface ScopeDecisionPromptInput {
  files: string[];
  reason: string;
  stepId: string;
}

export interface ScopeDecisionPromptResult {
  decision: "approve" | "reject";
  remember: boolean;
}

export interface ControllerUI {
  getWorkspacePath(): string | null;
  promptForGoal(): Promise<string | undefined>;
  promptForTaskId(): Promise<string | undefined>;
  promptForRejectReason(): Promise<string | undefined>;
  promptForResumeStage(): Promise<"plan" | "feedback" | "execute" | undefined>;
  promptForMaxIterationsOverride(): Promise<number | undefined>;
  promptForScopeDecision(input: ScopeDecisionPromptInput): Promise<ScopeDecisionPromptResult | undefined>;
  showInfo(message: string): void;
  showWarning(message: string): void;
  showError(message: string): void;
  openChatPanel(): void;
  appendChatMessage(message: ChatMessage): void;
  appendChatChunk(chunk: string): void;
  showChatThinking(message: string): void;
  updateChatThinking(message: string): void;
  hideChatThinking(): void;
  setChatInputEnabled(enabled: boolean): void;
  renderChatThreadList(threads: ChatThreadSummary[], activeThreadId: string): void;
  clearChatThread(): void;
  resolveInlineChangeCard(taskId: string, resolution: "applied" | "discarded"): void;
  updateThreadTitle(threadId: string, title: string): void;
  appendChatThinkingEntry(text: string): void;
  appendChatThinkingChunk(chunk: string): void;
  finalizeAgentMessage(): void;
  // Live, state-driven cards (Class A). One slot per kind, replace-not-append; written
  // by both the SSE path (instant) and the /live poll (durable across reload/resume).
  renderLiveGate(gate: LiveGateView): void;
  clearLiveGate(): void;
  renderLivePlan(plan: LivePlanView): void;
  clearLivePlan(): void;
  appendToolEvent(event: { id: number; tool: string; args: Record<string, unknown>; thought?: string; source: "explore" | "execution" | "planning" }): void;
  appendToolResult(id: number, output: string, isError: boolean): void;
  updateWorkbar(info: { stepIndex?: number; totalSteps?: number; stepTitle?: string; phaseLabel?: string } | null): void;
  renderLiveReview(review: { taskId: string; modifiedFiles: string[]; shadowWorkspacePath: string | null; stepsCompleted: number | null; stepsTotal: number | null; deviations: string[]; narrative?: { headline: string; points: string[] } }): void;
  clearLiveReview(): void;
  renderLiveError(error: { taskId: string; status: "FAILED" | "ABORTED"; detail?: string; narrative?: { headline: string; points: string[] } }): void;
  clearLiveError(): void;
  renderLiveTodos(todos: LiveTodosView): void;
  clearLiveTodos(): void;
  sendLiveStatus(status: string | null, turnActive: boolean): void;
}

export interface LiveGateView {
  kind: "command" | "step" | "scope" | "validation" | "mode" | "edit" | "clarify";
  payload: Record<string, unknown>;
  taskId: string;
}

export interface LivePlanView {
  taskId: string;
  planMarkdown: string;
}

export interface LiveTodosView {
  items: {
    title: string;
    status: "pending" | "in_progress" | "done" | "blocked" | "cancelled";
    note: string;
  }[];
}

export interface DiffService {
  openDiff(entry: ReviewFileEntry): Promise<void>;
}

export type BackendClientFactory = (baseUrl: string) => BackendTaskClient;

export class AiEditorController {
  private session: TaskSessionState | null = null;
  private latestTask: TaskView | null = null;
  private latestResult: TaskResult | null = null;
  private poller: TaskPoller | null = null;
  private streamController: AbortController | null = null;
  private activeThreadId: string | null = null;
  // /live poll: re-derives the active gate/plan from persisted task state so cards
  // survive reload and resume task-id churn. Source of truth for action binding (Task 8).
  private liveStateTimer: ReturnType<typeof setInterval> | null = null;
  private latestLiveState: ThreadLiveState | null = null;
  private lastLiveSignature: string | null = null;
  // Structured tool event forwarding state
  private toolEventSeq = 0;
  private openToolEvent: Partial<Record<"explore" | "execution" | "planning", number>> = {};
  private lastStepStarted: { stepId: string; stepTitle: string; stepIndex: number; totalSteps: number } | null = null;
  private lastPatchError: string | null = null;
  private runDeviations: string[] = [];
  private deviationsTaskId: string | null = null;
  private turnAbort: AbortController | null = null;
  private seenStepIds = new Set<string>();
  private latestLiveReview: { taskId: string; shadowWorkspacePath: string | null } | null = null;
  private livePollInFlight = false;
  // Tracks which task id the seenStepIds set belongs to, independently of deviationsTaskId.
  private stepTrackingTaskId: string | null = null;

  constructor(
    private readonly createClient: BackendClientFactory,
    private readonly sessionStore: SessionStore,
    private readonly settings: SettingsProvider,
    private readonly ui: ControllerUI,
    private readonly diffService: DiffService,
    private readonly now: () => string = () => new Date().toISOString()
  ) {}

  async initialize(): Promise<void> {
    const restored = await this.sessionStore.load();
    if (!restored) {
      return;
    }

    this.session = restored;
    if (!shouldStopPolling(restored.status)) {
      this.startPolling();
    }
    await this.refreshTask();
  }

  async startTask(): Promise<void> {
    const workspacePath = this.ui.getWorkspacePath();
    if (!workspacePath) {
      this.ui.showError("Open a workspace folder before starting an AI Editor task.");
      return;
    }

    const goal = (await this.ui.promptForGoal())?.trim();
    if (!goal) {
      return;
    }

    const backendBaseUrl = this.settings.getBackendBaseUrl();
    const mode = this.settings.getDefaultMode();
    const client = this.createClient(backendBaseUrl);

    let submission: { taskId: string };
    try {
      submission = await client.submitTask({
        goal,
        workspacePath,
        mode: mode satisfies TaskSubmission["mode"],
      });
    } catch (error) {
      this.ui.showError(`Failed to submit task: ${formatError(error)}`);
      return;
    }

    this.session = {
      taskId: submission.taskId,
      status: "QUEUED",
      workspacePath,
      backendBaseUrl,
      updatedAt: this.now(),
    };
    this.latestTask = {
      taskId: submission.taskId,
      goal,
      status: "QUEUED",
      modifiedFiles: [],
      diagnostics: [],
    };
    this.latestResult = null;
    this.stopStream();

    await this.sessionStore.save(this.session);
    this.startPolling();
    this.startStream(submission.taskId);
    this.ui.showInfo(`Started AI Editor task ${submission.taskId}`);
  }

  openReviewPanel(): void {
    // The unified chat panel replaced the review panel.
    this.ui.openChatPanel();
  }

  // ── Memory inspector (Phase 3-B) ────────────────────────────────────────────
  // vscode-free seam: the controller exposes the read-only data source + the active
  // thread + workspace; extension.ts owns MemoryPanel construction (it needs vscode +
  // extensionUri). The client is session-independent (built from the backend URL, like
  // attachToTask) since the inspector opens from chat with no task session.
  memoryDataSource(): MemoryDataSource {
    return {
      getInspect: (tid) =>
        tid ? this.memoryClient().getMemoryInspect(tid) : Promise.resolve(null),
      browse: (filter) => this.memoryClient().listMemories(filter),
      getChain: (memoryId) => this.memoryClient().getSupersedeChain(memoryId),
    };
  }

  memoryThreadId(): string {
    return this.activeThreadId ?? "";
  }

  memoryWorkspacePath(): string {
    return this.session?.workspacePath ?? this.ui.getWorkspacePath() ?? "";
  }

  private promptsDir(): string {
    return path.join(this.memoryWorkspacePath(), ".ai-editor", "prompts");
  }

  /** Names of available `.ai-editor/prompts/*.md` for composer `/` autocomplete. */
  async listPrompts(): Promise<string[]> {
    const ws = this.memoryWorkspacePath();
    if (!ws) return [];
    return listPromptNames(this.promptsDir());
  }

  /** Expand `/name args` to its substituted body; `found=false` when no such prompt. */
  async expandPrompt(name: string, args: string): Promise<{ found: boolean; text: string }> {
    const ws = this.memoryWorkspacePath();
    if (!ws) return { found: false, text: "" };
    const body = await loadPromptBody(this.promptsDir(), name);
    if (body === null) return { found: false, text: "" };
    return { found: true, text: substitutePrompt(body, args) };
  }

  private memoryClient(): BackendTaskClient {
    return this.createClient(this.settings.getBackendBaseUrl());
  }

  async attachToTask(): Promise<void> {
    const taskId = (await this.ui.promptForTaskId())?.trim();
    if (!taskId) {
      return;
    }

    const backendBaseUrl = this.settings.getBackendBaseUrl();
    const client = this.createClient(backendBaseUrl);

    let task: TaskView;
    try {
      task = await client.getTask(taskId);
    } catch (error) {
      this.ui.showError(`Task not found: ${formatError(error)}`);
      return;
    }

    const workspacePath = this.session?.workspacePath ?? this.ui.getWorkspacePath() ?? "";

    this.stopPolling();
    this.session = {
      taskId: task.taskId,
      status: task.status,
      workspacePath,
      backendBaseUrl,
      updatedAt: this.now(),
    };
    this.latestTask = task;
    this.latestResult = null;
    this.stopStream();

    await this.sessionStore.save(this.session);

    if (!shouldStopPolling(task.status)) {
      this.startPolling();
      this.syncStream(task.status, task.taskId);
    } else {
      await this.refreshTask();
    }

    this.ui.showInfo(`Attached to task ${taskId} (${task.status})`);
  }

  async refreshTask(): Promise<void> {
    await this.pullLatestTask();
  }

  async acceptPatch(): Promise<void> {
    if (!this.session) {
      this.ui.showWarning("No active task to accept.");
      return;
    }

    const client = this.clientForSession();
    try {
      this.latestResult = await client.acceptPatch(this.session.taskId);
    } catch (error) {
      if (isConflictError(error)) {
        this.ui.showWarning("Task is no longer reviewable. Refreshing state.");
        await this.refreshTask();
        return;
      }
      this.ui.showError(`Failed to accept patch: ${formatError(error)}`);
      return;
    }

    await this.pullLatestTask();
    this.ui.showInfo("Patch accepted.");
  }

  async rejectPatch(): Promise<void> {
    if (!this.session) {
      this.ui.showWarning("No active task to reject.");
      return;
    }

    const reason = (await this.ui.promptForRejectReason())?.trim();
    if (!reason) {
      return;
    }

    const client = this.clientForSession();
    try {
      this.latestResult = await client.rejectPatch(this.session.taskId, reason);
    } catch (error) {
      if (isConflictError(error)) {
        this.ui.showWarning("Task is no longer reviewable. Refreshing state.");
        await this.refreshTask();
        return;
      }
      this.ui.showError(`Failed to reject patch: ${formatError(error)}`);
      return;
    }

    await this.pullLatestTask();
    this.ui.showInfo("Patch rejected.");
  }

  async providePlanFeedback(feedback: string | null): Promise<void> {
    if (!this.session) {
      this.ui.showWarning("No active task for plan feedback.");
      return;
    }

    const client = this.clientForSession();
    const trimmedFeedback = (feedback ?? "").trim();
    const normalizedFeedback = trimmedFeedback.length > 0 ? trimmedFeedback : null;
    try {
      const task = await client.providePlanFeedback(this.session.taskId, normalizedFeedback);
      this.latestTask = task;
      this.session = {
        ...this.session,
        status: task.status,
        updatedAt: this.now(),
      };
      await this.sessionStore.save(this.session);
      
      if (!shouldStopPolling(task.status)) {
        this.startPolling();
      }
      // The route returns the pre-transition status (AWAITING_PLAN_APPROVAL) because
      // continue_task runs in the background. Always start the stream here since
      // execution begins immediately after approval regardless of the returned status.
      this.stopStream();
      this.startStream(task.taskId);

      if (normalizedFeedback) {
        this.ui.showChatThinking("Regenerating plan with your feedback…");
        this.ui.setChatInputEnabled(false);
        this.ui.showInfo(`Submitted plan feedback. Regenerating...`);
      } else {
        this.ui.showInfo(`Plan approved. Proceeding to execution...`);
      }
    } catch (error) {
      this.ui.showError(`Failed to provide plan feedback: ${formatError(error)}`);
    }
  }

  async resumeTask(): Promise<void> {
    if (!this.session) {
      this.ui.showWarning("No active task to resume.");
      return;
    }
    const status = this.latestTask?.status;
    if (status !== "FAILED" && status !== "ABORTED") {
      this.ui.showWarning("Resume is only available for failed or aborted tasks.");
      return;
    }

    const stage = await this.ui.promptForResumeStage();
    if (!stage) return;

    let maxIterations: number | undefined;
    if (stage === "execute") {
      maxIterations = await this.ui.promptForMaxIterationsOverride();
    }

    const client = this.clientForSession();
    let response: ResumeTaskResponse;
    try {
      response = await client.resumeTask(this.session.taskId, {
        stage,
        budgetOverride: maxIterations !== undefined ? { maxIterations } : undefined,
      });
    } catch (error) {
      this.ui.showError(`Failed to resume task: ${formatError(error)}`);
      return;
    }

    // Switch session to the new child task
    const childInitialStatus: TaskStatus = stage === "feedback" ? "AWAITING_PLAN_APPROVAL" : "QUEUED";
    this.session = {
      ...this.session,
      taskId: response.taskId,
      status: childInitialStatus,
      updatedAt: this.now(),
    };
    this.latestTask = null;
    this.latestResult = null;
    await this.sessionStore.save(this.session);
    this.startPolling();
    this.ui.showInfo(`Resumed as new task ${response.taskId}`);
  }

  async openInlineDiff(relativePath: string, shadowPath: string): Promise<void> {
    const workspacePath = this.ui.getWorkspacePath();
    // ReviewCard may post shadowPath "" — fall back to the latest live review's shadow path.
    if (!shadowPath && this.latestLiveReview?.shadowWorkspacePath) {
      const path = await import("node:path");
      shadowPath = path.join(this.latestLiveReview.shadowWorkspacePath, relativePath);
    }
    if (!workspacePath || !shadowPath) {
      this.ui.showWarning("Diff is unavailable — shadow path missing.");
      return;
    }
    const path = await import("node:path");
    const fs = await import("node:fs");
    const entry = {
      relativePath,
      realPath: path.join(workspacePath, relativePath),
      shadowPath,
      existsReal: fs.existsSync(path.join(workspacePath, relativePath)),
      existsShadow: fs.existsSync(shadowPath),
    };
    try {
      await this.diffService.openDiff(entry);
    } catch (error) {
      this.ui.showError(`Failed to open diff: ${formatError(error)}`);
    }
  }

  async openDiffForFile(relativePath: string): Promise<void> {
    if (!this.session || !this.latestResult) {
      this.ui.showWarning("No review result is available for diff inspection.");
      return;
    }

    if (!this.latestResult.shadowWorkspacePath) {
      this.ui.showWarning("Shadow workspace is unavailable for this task result.");
      return;
    }

    const entries = buildReviewFileEntries(
      this.session.workspacePath,
      this.latestResult.shadowWorkspacePath,
      this.latestResult.modifiedFiles
    );
    const entry = entries.find((candidate) => candidate.relativePath === relativePath);
    if (!entry) {
      this.ui.showWarning(`File not found in review list: ${relativePath}`);
      return;
    }

    try {
      await this.diffService.openDiff(entry);
    } catch (error) {
      this.ui.showError(`Failed to open diff: ${formatError(error)}`);
    }
  }

  async openChat(): Promise<void> {
    const workspacePath = this.ui.getWorkspacePath() ?? "";
    const client = this.createClient(this.settings.getBackendBaseUrl());
    let threads: ChatThreadSummary[];
    try {
      threads = await client.listChatThreads(workspacePath);
    } catch {
      threads = [];
    }
    const first = threads[0];
    if (first && !this.activeThreadId) {
      this.activeThreadId = first.threadId;
    } else if (!this.activeThreadId) {
      try {
        const thread = await client.createChatThread(workspacePath);
        this.activeThreadId = thread.threadId;
        threads = [thread];
      } catch (error) {
        this.ui.showError(`Failed to create chat thread: ${formatError(error)}`);
        return;
      }
    }
    this.ui.openChatPanel();
    this.ui.renderChatThreadList(threads, this.activeThreadId ?? "");
    if (this.activeThreadId) {
      try {
        const thread = await client.getChatThread(this.activeThreadId);
        this.ui.clearChatThread();
        for (const message of thread.messages) {
          this.ui.appendChatMessage(message);
        }
      } catch {
        // non-fatal — panel opens empty
      }
    }
    // Re-derive any pending gate/plan from persisted state so cards survive the
    // reload/reopen that just discarded the SSE-rendered ones.
    this.lastLiveSignature = null;
    this.startLiveStatePolling();
  }

  async newChatThread(): Promise<void> {
    const workspacePath = this.ui.getWorkspacePath() ?? "";
    const client = this.createClient(this.settings.getBackendBaseUrl());
    let thread;
    try {
      thread = await client.createChatThread(workspacePath);
    } catch (error) {
      this.ui.showError(`Failed to create chat thread: ${formatError(error)}`);
      return;
    }
    this.activeThreadId = thread.threadId;
    this.ui.openChatPanel();
    this.ui.clearChatThread();
    this.ui.clearLiveGate();
    this.ui.clearLivePlan();
    this.lastLiveSignature = null;
    this._liveResumeThreadId = null;
    let threads: ChatThreadSummary[];
    try {
      threads = await client.listChatThreads(workspacePath);
    } catch {
      threads = [thread];
    }
    this.ui.renderChatThreadList(threads, thread.threadId);
    this.startLiveStatePolling();
  }

  async switchChatThread(threadId: string): Promise<void> {
    this.activeThreadId = threadId;
    const client = this.createClient(this.settings.getBackendBaseUrl());
    const thread = await client.getChatThread(threadId);
    this.ui.clearChatThread();
    // Drop the previous thread's live cards; the new thread's /live poll repopulates.
    this.ui.clearLiveGate();
    this.ui.clearLivePlan();
    this.lastLiveSignature = null;
    this._liveResumeThreadId = null;
    for (const message of thread.messages) {
      this.ui.appendChatMessage(message);
    }
    this.startLiveStatePolling();
  }

  async sendChatMessage(text: string, stepReview?: boolean): Promise<void> {
    const workspacePath = this.ui.getWorkspacePath() ?? "";
    const client = this.createClient(this.settings.getBackendBaseUrl());

    if (!this.activeThreadId) {
      try {
        const thread = await client.createChatThread(workspacePath);
        this.activeThreadId = thread.threadId;
        this.ui.openChatPanel();
      } catch (error) {
        this.ui.showError(`Failed to create chat thread: ${formatError(error)}`);
        return;
      }
    }

    const threadId = this.activeThreadId;

    this.ui.appendChatMessage({
      role: "user",
      content: text,
      type: "text",
      timestamp: this.now(),
      metadata: {},
    });

    this.ui.setChatInputEnabled(false);
    this.turnAbort = new AbortController();
    await this.streamTurn(
      client.sendChatMessage(
        threadId,
        text,
        this.turnAbort.signal,
        stepReview !== undefined ? { stepReview } : undefined,
      ),
    );
  }

  // Thread currently being live-resumed (channel re-subscribe) — idempotency guard.
  private _liveResumeThreadId: string | null = null;

  /**
   * Resume the live overlay for an in-flight controller turn after a webview reload.
   * Subscribe-only relay (no turn launch) over GET /v1/channels/{id}/stream; reuses
   * streamTurn's event rendering. Best-effort: events older than the 50-event replay
   * buffer (and everything after a backend restart) come from the reconstructed
   * transcript, not here.
   */
  private async resumeLiveOverlay(threadId: string): Promise<void> {
    try {
      this.turnAbort = new AbortController();
      await this.streamTurn(this.clientForChat().streamChannel(`chat:${threadId}`));
    } catch (error) {
      // A closed/empty channel is expected (turn already done) — clear the guard so a
      // later turn can resume, and let the /live poll keep driving durable state.
      this._liveResumeThreadId = null;
      if (!(error instanceof Error && error.name === "AbortError")) {
        // non-fatal
      }
    }
  }

  /**
   * Consume a chat-turn SSE stream and render its events. Shared by sendChatMessage
   * and the controller mode-decision dispatch — both stream the same chat events.
   * The caller disables input + sets `turnAbort`; this owns the loop and teardown.
   */
  private async streamTurn(stream: AsyncIterable<StreamEvent>): Promise<void> {
    let currentTaskId: string | undefined;
    try {
      this.openToolEvent = {}; // defensive: clear any stale ids from a previous turn
      for await (const event of stream) {
        if (event.type === "chat_agent_thinking") {
          const message = (event.payload["message"] as string) ?? "Thinking…";
          this.ui.appendChatThinkingEntry(message);
        } else if (event.type === "chat_agent_thinking_chunk") {
          const chunk = (event.payload["chunk"] as string) ?? "";
          if (chunk) this.ui.appendChatThinkingChunk(chunk);
        } else if (event.type === "tool_thinking_chunk") {
          const chunk = (event.payload["chunk"] as string) ?? "";
          if (chunk) this.ui.appendChatThinkingChunk(chunk);
        } else if (event.type === "explore_tool_call") {
          this.forwardToolCall("explore", event.payload as Record<string, unknown>);
        } else if (event.type === "explore_tool_result") {
          this.forwardToolResult("explore", event.payload as Record<string, unknown>);
        } else if (event.type === "tool_call") {
          this.forwardToolCall("execution", event.payload as Record<string, unknown>);
        } else if (event.type === "tool_result") {
          this.forwardToolResult("execution", event.payload as Record<string, unknown>);
        } else if (event.type === "patch_applied") {
          this.ui.appendChatThinkingEntry("patch applied");
        } else if (event.type === "intent_classified") {
          // no-op: intent classification doesn't need a persistent entry
        } else if (event.type === "chat_response") {
          const chunk = (event.payload["chunk"] as string) ?? "";
          this.ui.appendChatChunk(chunk);
        } else if (event.type === "chat_breadcrumb") {
          // Controller decisions (mode choice, edit accept/reject) broadcast their
          // breadcrumbs on THIS turn stream. streamTurn previously had no branch for
          // them, so they only appeared on reload (persisted) — render live too.
          this.ui.appendChatMessage({
            role: "agent",
            content: (event.payload["text"] as string) ?? "",
            type: "text",
            taskId: (event.payload["task_id"] as string) ?? "",
            timestamp: this.now(),
            metadata: { breadcrumb: true },
          });
        } else if (event.type === "memory_compacted") {
          // Observability: the memory harness compacted older history into the anchored
          // summary. Render a subtle system line so the user can see it fired.
          const evicted = (event.payload["evicted"] as number) ?? 0;
          const version = (event.payload["anchor_version"] as number) ?? 0;
          this.ui.appendChatMessage({
            role: "agent",
            content: `🗜️ Compacted ${evicted} earlier message${evicted === 1 ? "" : "s"} into memory (v${version})`,
            type: "text",
            timestamp: this.now(),
            metadata: { breadcrumb: true },
          });
        } else if (event.type === "diff_ready") {
          const taskId = (event.payload["task_id"] as string) ?? "";
          // `resolved` is set by the chat controller's auto-accept edit path (the
          // change is already promoted) → the card renders inert. The legacy inline
          // path omits it → interactive Accept/Reject, as before.
          const resolved = event.payload["resolved"] as
            | "applied"
            | "discarded"
            | undefined;
          this.ui.appendChatMessage({
            role: "agent",
            content: taskId,
            type: "diff_card",
            taskId,
            timestamp: this.now(),
            metadata: {
              diff_entries: event.payload["diff_entries"],
              thinking_log: event.payload["thinking_log"] ?? [],
              ...(resolved ? { resolved } : {}),
            },
          });
        } else if (event.type === "task_card") {
          currentTaskId = (event.payload["task_id"] as string) ?? "";
          this.ui.appendChatMessage({
            role: "agent",
            content: currentTaskId,
            type: "task_card",
            taskId: currentTaskId,
            timestamp: this.now(),
            metadata: {},
          });
        } else if (event.type === "planning_thinking_chunk") {
          const chunk = (event.payload["chunk"] as string) ?? "";
          if (chunk) this.ui.appendChatThinkingChunk(chunk);
        } else if (event.type === "planning_tool_call") {
          this.forwardToolCall("planning", event.payload as Record<string, unknown>);
          this.ui.updateWorkbar({ phaseLabel: `Planning: ${(event.payload["tool"] as string) ?? ""}…` });
        } else if (event.type === "planning_tool_result") {
          this.forwardToolResult("planning", event.payload as Record<string, unknown>);
        } else if (event.type === "planning_complete") {
          const confidence = (event.payload["confidence"] as string) ?? "";
          this.ui.appendChatThinkingEntry(`plan ready (${confidence} confidence)`);
          this.ui.updateWorkbar(null);
        } else if (event.type === "task_status_changed") {
          const taskId = (event.payload["task_id"] as string) ?? "";
          const status = (event.payload["status"] as string) ?? "";
          const planMarkdown = event.payload["plan_markdown"] as string | undefined;
          if (planMarkdown) {
            // Plan is a Class-A live card — let /live render it in the pinned slot.
            void this.pollThreadLiveState();
          } else {
            this.ui.appendChatMessage({
              role: "agent",
              content: `Task ${taskId}: ${status}`,
              type: "text",
              taskId,
              timestamp: this.now(),
              metadata: {},
            });
          }
        } else if (event.type === "plan_card") {
          // Read-only transcript record, delivered live (display-only — the backend
          // already persisted the single copy). chat.js dedups by task id, and the
          // interactive Implement/Feedback affordance is the pinned /live slot.
          this.ui.appendChatMessage({
            role: "agent",
            content: event.payload.plan_markdown,
            type: "plan_card",
            taskId: event.payload.task_id,
            timestamp: this.now(),
            metadata: { taskId: event.payload.task_id, plan_markdown: event.payload.plan_markdown },
          });
        } else if (event.type === "scope_extension_requested") {
          // Class-A gate — render from /live (instant poke now, durable on reload).
          this.forwardGateWait("scope");
        } else if (event.type === "validation_decision_requested") {
          this.forwardGateWait("validation");
        } else if (event.type === "command_approval_requested") {
          this.forwardGateWait("command");
        } else if (event.type === "thread_title_updated") {
          const threadId = (event.payload["thread_id"] as string) ?? "";
          const title = (event.payload["title"] as string) ?? "";
          this.ui.updateThreadTitle(threadId, title);
        } else if (
          event.type === "env_profile_building" ||
          event.type === "env_profile_built" ||
          event.type === "env_install_running" ||
          event.type === "env_install_done"
        ) {
          this.forwardEnvEvent({ type: event.type, payload: event.payload as Record<string, unknown> });
        } else if (event.type === "chat_done") {
          this.ui.updateWorkbar(null);
          this.ui.finalizeAgentMessage();
          break;
        }
      }
    } catch (error) {
      // Swallow AbortError — turn was stopped by the user intentionally.
      if (error instanceof Error && error.name === "AbortError") {
        // do nothing
      } else {
        throw error;
      }
    } finally {
      this.openToolEvent = {}; // clear cross-turn stale ids
      this.ui.updateWorkbar(null);
      this.turnAbort = null;
      this.ui.hideChatThinking();
      this.ui.finalizeAgentMessage();
      this.ui.setChatInputEnabled(true);
    }
  }

  async handlePlanCardAction(
    taskId: string,
    action: "implement" | "feedback",
    feedback?: string
  ): Promise<void> {
    if (action === "implement") {
      const client = this.createClient(this.settings.getBackendBaseUrl());
      try {
        await client.providePlanFeedback(taskId, null);
      } catch (error) {
        this.ui.showError(`Failed to approve plan: ${formatError(error)}`);
        return;
      }
      await this.streamTaskIntoChatThread(taskId);
    } else {
      if (!feedback?.trim()) return;
      const client = this.createClient(this.settings.getBackendBaseUrl());
      try {
        await client.providePlanFeedback(taskId, feedback.trim());
      } catch (error) {
        this.ui.showError(`Failed to submit plan feedback: ${formatError(error)}`);
        return;
      }
      this.ui.setChatInputEnabled(false);
      this.ui.appendChatThinkingEntry("Replanning with feedback…");
      const abort = new AbortController();
      try {
        await client.streamPatch(taskId, (event) => {
          if (event.type === "planning_thinking_chunk") {
            const chunk = (event.payload["chunk"] as string) ?? "";
            if (chunk) this.ui.appendChatThinkingChunk(chunk);
          } else if (event.type === "planning_tool_call") {
            this.forwardToolCall("planning", event.payload as Record<string, unknown>);
            this.ui.updateWorkbar({ phaseLabel: `Planning: ${(event.payload["tool"] as string) ?? ""}…` });
          } else if (event.type === "planning_tool_result") {
            this.forwardToolResult("planning", event.payload as Record<string, unknown>);
          } else if (event.type === "planning_complete") {
            const confidence = (event.payload["confidence"] as string) ?? "";
            this.ui.appendChatThinkingEntry(`plan ready (${confidence} confidence)`);
            this.ui.updateWorkbar(null);
            abort.abort();
          }
        }, abort.signal);
      } catch {
        // AbortError when we cancel after planning_complete — expected
      } finally {
        this.openToolEvent = {}; // clear cross-turn stale ids
        this.ui.updateWorkbar(null);
      }
      try {
        const task = await client.getTask(taskId);
        if (task.status === "AWAITING_PLAN_APPROVAL" && task.planMarkdown) {
          // Plan is a Class-A live card — render it from /live in the pinned slot.
          void this.pollThreadLiveState();
        } else if (task.status === "FAILED") {
          this.ui.showError("Re-planning failed — the model used its full tool budget without producing a plan. Try submitting feedback again with more specific instructions.");
        }
      } finally {
        this.ui.setChatInputEnabled(true);
      }
    }
  }

  async streamTaskIntoChatThread(taskId: string, openingEntry = "Generating execution plan…"): Promise<void> {
    const client = this.createClient(this.settings.getBackendBaseUrl());
    this.ui.setChatInputEnabled(false);
    this.ui.appendChatThinkingEntry(openingEntry);
    try {
      for await (const event of client.streamPatchEvents(taskId)) {
        if (event.type === "planning_thinking_chunk") {
          const chunk = (event.payload["chunk"] as string) ?? "";
          if (chunk) this.ui.appendChatThinkingChunk(chunk);
        } else if (event.type === "tool_thinking_chunk") {
          const chunk = (event.payload["chunk"] as string) ?? "";
          if (chunk) this.ui.appendChatThinkingChunk(chunk);
        } else if (event.type === "task_status_changed") {
          const msg = (event.payload["message"] as string | undefined) ?? (event.payload["status"] as string) ?? "";
          if (msg) this.ui.appendChatThinkingEntry(msg);
        } else if (event.type === "tool_call") {
          this.forwardToolCall("execution", event.payload as Record<string, unknown>);
        } else if (event.type === "tool_result") {
          this.forwardToolResult("execution", event.payload as Record<string, unknown>);
        } else if (event.type === "planning_tool_call") {
          this.forwardToolCall("planning", event.payload as Record<string, unknown>);
          this.ui.updateWorkbar({ phaseLabel: `Planning: ${(event.payload["tool"] as string) ?? ""}…` });
        } else if (event.type === "planning_tool_result") {
          this.forwardToolResult("planning", event.payload as Record<string, unknown>);
        } else if (event.type === "step_started") {
          const p = event.payload;
          this.lastStepStarted = { stepId: p.step_id, stepTitle: p.step_title, stepIndex: p.step_index, totalSteps: p.total_steps };
          // Reset seenStepIds when a different task's stream begins.
          if (this.stepTrackingTaskId !== taskId) {
            this.stepTrackingTaskId = taskId;
            this.seenStepIds.clear();
          }
          this.seenStepIds.add(p.step_id);
          this.ui.updateWorkbar({ stepIndex: p.step_index, totalSteps: p.total_steps, stepTitle: p.step_title });
        } else if (event.type === "patch_applied") {
          const files = event.payload.touched_files?.join(", ") ?? "";
          this.ui.appendChatThinkingEntry(`patch applied${files ? ` (${files})` : ""}`);
        } else if (event.type === "patch_failed") {
          const err = (event.payload["error"] as string) ?? "";
          this.lastPatchError = err;
          this.ui.appendChatThinkingEntry(`✗ patch failed: ${err.slice(0, 200)}`);
        } else if (event.type === "revision_needed") {
          this.noteDeviation(taskId, "Delta replan triggered: " + ((event.payload["reason"] as string) ?? ""));
          this.ui.appendChatThinkingEntry("revision needed — delta replanning…");
        } else if (event.type === "planning_complete") {
          this.ui.appendChatThinkingEntry(`delta replan complete (${event.payload.confidence} confidence)`);
          this.ui.updateWorkbar(null);
        } else if (event.type === "operation_success") {
          this.ui.appendChatMessage({
            role: "agent",
            content: `✓ ${event.payload.op_type}: ${event.payload.path}`,
            type: "text",
            timestamp: this.now(),
            metadata: {},
          });
        } else if (event.type === "operation_error") {
          this.ui.appendChatMessage({
            role: "agent",
            content: `✗ ${event.payload.op_type}: ${event.payload.path} — ${event.payload.error}`,
            type: "text",
            timestamp: this.now(),
            metadata: {},
          });
        } else if (event.type === "chat_breadcrumb") {
          // Durable transcript record of a resolved gate/plan action, pushed live so it
          // lands in history immediately instead of only on the next thread reload.
          this.ui.appendChatMessage({
            role: "agent",
            content: event.payload.text,
            type: "text",
            taskId: event.payload.task_id,
            timestamp: this.now(),
            metadata: { taskId: event.payload.task_id, breadcrumb: true },
          });
          // Capture deviation breadcrumbs for run context.
          // COUPLING: prefixes mirror backend breadcrumb constants in
          // services/agentd-py/agentd/orchestrator/engine.py (write_chat_breadcrumb call
          // sites). A reword there silently drops deviation capture — keep in sync.
          const breadcrumbText: string = event.payload.text ?? "";
          if (
            breadcrumbText.startsWith("✓ Scope extension approved") ||
            breadcrumbText.startsWith("✓ Command approved") ||
            breadcrumbText.startsWith("↩ Step changes discarded") ||
            breadcrumbText.startsWith("✓ Validation accepted")
          ) {
            this.noteDeviation(event.payload.task_id ?? taskId, breadcrumbText);
          }
        } else if (event.type === "plan_card") {
          // Read-only transcript record (e.g. a feedback-regenerated version picked up
          // when execution starts). Display-only; chat.js dedups by task+content.
          this.ui.appendChatMessage({
            role: "agent",
            content: event.payload.plan_markdown,
            type: "plan_card",
            taskId: event.payload.task_id,
            timestamp: this.now(),
            metadata: { taskId: event.payload.task_id, plan_markdown: event.payload.plan_markdown },
          });
        } else if (event.type === "scope_extension_requested") {
          // Class-A gates render from /live in the pinned slot; poke for an instant update.
          this.forwardGateWait("scope");
        } else if (event.type === "validation_decision_requested") {
          this.forwardGateWait("validation");
        } else if (event.type === "command_approval_requested") {
          this.forwardGateWait("command");
        } else if (
          event.type === "env_profile_building" ||
          event.type === "env_profile_built" ||
          event.type === "env_install_running" ||
          event.type === "env_install_done"
        ) {
          this.forwardEnvEvent({ type: event.type, payload: event.payload as Record<string, unknown> });
        } else if (event.type === "done") {
          const status = (event.payload["status"] as string | undefined) ?? "";
          this.ui.hideChatThinking();
          this.ui.updateWorkbar(null);
          if (status === "READY_FOR_REVIEW") {
            this.ui.appendChatMessage({
              role: "agent",
              content: "Execution complete — review the diff in the Tasks panel.",
              type: "text",
              timestamp: this.now(),
              metadata: { task_id: taskId },
            });
          } else if (status === "FAILED" || status === "ABORTED") {
            this.ui.appendChatMessage({
              role: "agent",
              content: `Execution ${status.toLowerCase()} — check the Tasks panel for details.`,
              type: "text",
              timestamp: this.now(),
              metadata: { task_id: taskId },
            });
          }
          break;
        }
      }
    } catch (error) {
      this.ui.showError(`Stream error: ${formatError(error)}`);
    } finally {
      this.openToolEvent = {}; // clear cross-turn stale ids
      this.ui.updateWorkbar(null);
      this.ui.hideChatThinking();
      this.ui.setChatInputEnabled(true);
    }
  }

  async acceptStep(taskId: string): Promise<void> {
    const client = this.clientForChat();
    try {
      await client.sendStepDecision(this.liveTaskIdOr(taskId), "accept");
    } catch (error) {
      if (this.isBenignConflict(error)) return;
      this.ui.showError(`Failed to accept step: ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  async discardStep(taskId: string): Promise<void> {
    const client = this.clientForChat();
    try {
      await client.sendStepDecision(this.liveTaskIdOr(taskId), "discard");
    } catch (error) {
      if (this.isBenignConflict(error)) return;
      this.ui.showError(`Failed to discard step: ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  /**
   * Resolve the controller mode gate (Phase F2). A STREAMED dispatch: edit/explain
   * re-enter the loop and create_task hands off, all producing live chat events —
   * so consume it through streamTurn exactly like a normal message turn.
   */
  async handleModeDecisionFromChat(threadId: string, mode: string): Promise<void> {
    const client = this.clientForChat();
    this.ui.setChatInputEnabled(false);
    this.turnAbort = new AbortController();
    await this.streamTurn(client.postModeDecision(threadId, mode));
  }

  /**
   * Resolve the controller clarify gate. A STREAMED dispatch: the answer re-enters the
   * loop (EDIT if the clarify fired mid-edit, else DECIDE), producing live chat events —
   * consume via streamTurn like a normal turn (mirror of handleModeDecisionFromChat).
   */
  async handleClarifyDecisionFromChat(threadId: string, answer: string): Promise<void> {
    const client = this.clientForChat();
    this.ui.setChatInputEnabled(false);
    this.turnAbort = new AbortController();
    await this.streamTurn(client.postClarifyDecision(threadId, answer));
  }

  /**
   * Resolve the controller per-edit review gate (Phase F3). A plain POST — the
   * loop continuation rides the already-open message SSE stream (still consumed by
   * the in-flight streamTurn), so there is nothing to consume here.
   */
  async handleEditDecisionFromChat(
    threadId: string,
    decision: "accept" | "reject",
    reason: string,
  ): Promise<void> {
    const client = this.clientForChat();
    try {
      await client.postEditDecision(threadId, decision, reason);
    } catch (error) {
      if (this.isBenignConflict(error)) return;
      this.ui.showError(
        `Failed to send edit decision: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
  }

  async applyInlineChange(inlineTaskId: string): Promise<void> {
    const client = this.createClient(this.settings.getBackendBaseUrl());
    try {
      await client.applyInlineChange(inlineTaskId);
      this.ui.showInfo("Changes applied to workspace.");
      this.ui.resolveInlineChangeCard(inlineTaskId, "applied");
    } catch (error) {
      this.ui.showError(`Failed to apply inline change: ${formatError(error)}`);
    }
  }

  async discardInlineChange(inlineTaskId: string): Promise<void> {
    const client = this.createClient(this.settings.getBackendBaseUrl());
    try {
      await client.discardInlineChange(inlineTaskId);
      this.ui.resolveInlineChangeCard(inlineTaskId, "discarded");
    } catch (error) {
      this.ui.showError(`Failed to discard inline change: ${formatError(error)}`);
    }
  }

  async acceptTaskPatch(taskId: string): Promise<void> {
    try {
      const result = await this.clientForChat().acceptPatch(taskId);
      this.ui.showInfo("Task finished — changes are in your workspace.");
      // Optimistic copy of the breadcrumb the backend persists on /accept: nothing
      // is listening on the task SSE channel by Finish-time, so without this the
      // durable record only appears after the next webview reload.
      this.appendBreadcrumbMessage(
        taskId,
        `✓ Task finished — ${result.modifiedFiles.length} file(s) applied to the workspace.`,
      );
      // poke so the review card clears sub-interval once status advances
      this.lastLiveSignature = null;
      void this.pollThreadLiveState();
    } catch (error) {
      if (this.isBenignConflict(error)) return;
      this.ui.showError(`Failed to finish task: ${formatError(error)}`);
    }
  }

  async rejectTaskPatch(taskId: string, reason: string): Promise<void> {
    try {
      await this.clientForChat().rejectPatch(taskId, reason.trim() || "closed from chat");
      // Tier B: reject is now a TRUE revert (the backend restores the pre-execution state
      // and deletes task-created files), not "keep changes". Message + breadcrumb mirror that.
      this.ui.showInfo("All changes discarded — workspace rolled back to its pre-task state.");
      this.appendBreadcrumbMessage(
        taskId,
        "✗ All changes discarded — workspace rolled back to its pre-task state.",
      );
      this.lastLiveSignature = null;
      void this.pollThreadLiveState();
    } catch (error) {
      if (this.isBenignConflict(error)) return;
      this.ui.showError(`Failed to discard changes: ${formatError(error)}`);
    }
  }

  /** Cooperative Stop for the running task (work-bar). revert rolls the workspace back to
   * its pre-execution state; otherwise the changes applied so far are kept (Tier B). */
  async abortActiveTask(revert: boolean): Promise<void> {
    const taskId = this.latestLiveState?.activeTaskId;
    if (!taskId) return;
    try {
      await this.clientForChat().abortTask(taskId, { revert });
      this.ui.showInfo(
        revert ? "Stopping task and reverting changes…" : "Stopping task; changes so far are kept…",
      );
      this.lastLiveSignature = null;
      void this.pollThreadLiveState();
    } catch (error) {
      if (this.isBenignConflict(error)) return;
      this.ui.showError(`Failed to stop task: ${formatError(error)}`);
    }
  }

  /** Live-mutable "Review each step" preference for the running task (Tier B). A 409 (no
   * task running) is benign — the toggle only governs creation-time default in that case. */
  async setReviewPref(autoAccept: boolean): Promise<void> {
    const taskId = this.latestLiveState?.activeTaskId;
    if (!taskId) return;
    try {
      await this.clientForChat().setReviewPref(taskId, { autoAccept });
      this.lastLiveSignature = null;
      void this.pollThreadLiveState();
    } catch (error) {
      if (this.isBenignConflict(error)) return;
      this.ui.showError(`Failed to update review preference: ${formatError(error)}`);
    }
  }

  /** Mirror of the backend's persisted breadcrumb, rendered immediately. The
   *  persisted copy takes over on the next thread reload (full list replace —
   *  no duplication). */
  private appendBreadcrumbMessage(taskId: string, text: string): void {
    this.ui.appendChatMessage({
      role: "agent",
      content: text,
      type: "text",
      taskId,
      timestamp: this.now(),
      metadata: { taskId, breadcrumb: true },
    });
  }

  async resumeTaskById(taskId: string, stage: "plan" | "execute"): Promise<void> {
    let childId: string;
    try {
      const response = await this.clientForChat().resumeTask(taskId, { stage });
      childId = response.taskId;
      this.ui.showInfo(`Resumed as ${childId}`);
    } catch (error) {
      this.ui.showError(`Failed to resume: ${formatError(error)}`);
      return;
    }
    // the child task is a fresh run — never let the parent's step/error history describe it
    this.seenStepIds.clear();
    this.stepTrackingTaskId = null;
    this.lastStepStarted = null;
    this.lastPatchError = null;
    this.runDeviations = [];
    this.deviationsTaskId = null;
    // Optimistic task_card anchor for the child, mirroring the backend record the
    // resume route now persists (the persisted copy replaces this on the next reload —
    // same optimistic pattern as appendBreadcrumbMessage). Gives the resumed run a
    // transcript row instead of appearing only as live pills.
    this.ui.appendChatMessage({
      role: "agent",
      content: childId,
      type: "task_card",
      taskId: childId,
      timestamp: this.now(),
      metadata: {},
    });
    // Force the next poll to re-render against the child task (signature reset).
    this.lastLiveSignature = null;
    void this.pollThreadLiveState();
    // An execute-stage resume runs execution immediately on the child's task channel.
    // Nobody else subscribes to a resumed child's stream, so without this the run is
    // mute — no tool pills, no work bar, no step progress — until a reload. Attach to
    // it through the same render path as a post-approval implement. A plan-stage resume
    // re-plans and pauses at AWAITING_PLAN_APPROVAL (no terminal event to end the
    // stream); it is driven by /live + the live plan card's Implement path instead.
    if (stage === "execute") {
      await this.streamTaskIntoChatThread(childId, "Resuming execution…");
    }
  }

  dispose(): void {
    this.stopPolling();
    this.stopStream();
    this.stopLiveStatePolling();
  }

  async stopActiveTurn(): Promise<void> {
    // Detached turns are not cancelled by disconnecting the SSE anymore — Stop is an
    // explicit POST /stop (a slimmer cousin of task /abort). Still abort the local SSE
    // reader so the relay loop unwinds promptly; the server-side cancel is the real stop.
    this.turnAbort?.abort();
    const threadId = this.activeThreadId;
    if (!threadId) return;
    try {
      const result = await this.clientForChat().stopChatTurn(threadId);
      // We aborted our SSE reader above, so the backend's live "✗ Stopped" breadcrumb
      // broadcast never reaches the open webview (finding 7) — only its durable copy
      // survives (shows on reopen). Render it optimistically, like accept/discard; a
      // reopen replaces it with the durable copy (no dup). Gated on ok = a real stop.
      if (result.ok) this.appendBreadcrumbMessage("", "✗ Stopped");
    } catch (error) {
      if (this.isBenignConflict(error)) return;
      // A failed stop is non-fatal — the turn finishes on its own; log only.
      this.ui.showWarning(`Stop failed: ${formatError(error)}`);
    } finally {
      this.lastLiveSignature = null;
      void this.pollThreadLiveState();
    }
  }

  private forwardToolCall(source: "explore" | "execution" | "planning", payload: Record<string, unknown>): void {
    // Prefer the backend's call_index as the pill id (controller execution pills): it
    // equals the persisted pill id, so a switch-back resume dedups replayed pills against
    // the loaded in-flight message (useAppState appendToolEvent). Other sources keep the
    // session-monotonic seq.
    const id = typeof payload["call_index"] === "number"
      ? (payload["call_index"] as number)
      : ++this.toolEventSeq;
    this.openToolEvent[source] = id;
    const thought = (payload["thought"] as string) || undefined;
    this.ui.appendToolEvent({
      id,
      tool: (payload["tool"] as string) ?? "",
      args: (payload["args"] as Record<string, unknown>) ?? {},
      ...(thought !== undefined ? { thought } : {}),
      source,
    });
  }

  private forwardToolResult(source: "explore" | "execution" | "planning", payload: Record<string, unknown>): void {
    const id = this.openToolEvent[source];
    if (id === undefined) return; // result without a call (replay edge) — drop
    delete this.openToolEvent[source];
    this.ui.appendToolResult(id, (payload["output"] as string) ?? "", payload["is_error"] === true);
  }

  /** Test/diagnostic surface — exposes deviation breadcrumbs captured during the last stream. */
  get observedDeviations(): readonly string[] {
    return this.runDeviations;
  }

  private noteDeviation(taskId: string, text: string): void {
    if (this.deviationsTaskId !== taskId) { this.deviationsTaskId = taskId; this.runDeviations = []; }
    this.runDeviations.push(text);
  }

  /**
   * Forward one of the four env lifecycle events (env_profile_building / env_profile_built /
   * env_install_running / env_install_done) as a thinking entry + workbar phase label/clear.
   * Both SSE loops call this to keep behaviour in sync.
   */
  private forwardEnvEvent(event: { type: string; payload: Record<string, unknown> }): void {
    if (event.type === "env_profile_building") {
      this.ui.appendChatThinkingEntry("Preparing workspace env profile…");
      this.ui.updateWorkbar({ phaseLabel: "Profiling workspace environment…" });
    } else if (event.type === "env_profile_built") {
      const count = (event.payload["ecosystems_count"] as number) ?? 0;
      const bootstrap = (event.payload["bootstrap_needed"] as boolean) ?? false;
      this.ui.appendChatThinkingEntry(
        bootstrap
          ? "Env profile: workspace has no manifests yet (bootstrap_needed)"
          : `Env profile ready (${count} ecosystem${count === 1 ? "" : "s"})`,
      );
      this.ui.updateWorkbar(null);
    } else if (event.type === "env_install_running") {
      const cmd = (event.payload["command"] as string) ?? "";
      const scope = (event.payload["scope_key"] as string) ?? "";
      this.ui.appendChatThinkingEntry(`Syncing deps: ${cmd} (${scope})`);
      this.ui.updateWorkbar({ phaseLabel: `Syncing dependencies: ${cmd}…` });
    } else if (event.type === "env_install_done") {
      const ok = (event.payload["exit_ok"] as boolean) ?? false;
      const scope = (event.payload["scope_key"] as string) ?? "";
      this.ui.appendChatThinkingEntry(
        ok ? `Deps synced for ${scope}` : `Deps sync FAILED for ${scope}`,
      );
      this.ui.updateWorkbar(null);
    }
  }

  /**
   * Forward a gate-wait event: append the matching "Waiting for … approval/decision…"
   * thinking entry AND poke a /live poll so the pinned gate card renders instantly.
   * Both SSE loops call this — reconciles a pre-existing divergence where
   * streamTaskIntoChatThread's validation/command cases were poke-only.
   */
  private forwardGateWait(kind: "scope" | "validation" | "command"): void {
    const label =
      kind === "scope" ? "Waiting for scope approval…"
      : kind === "validation" ? "Waiting for validation decision…"
      : "Waiting for command approval…";
    this.ui.appendChatThinkingEntry(label);
    void this.pollThreadLiveState();
  }

  private startStream(taskId: string): void {
    if (this.streamController) return;
    this.streamController = new AbortController();
    const { signal } = this.streamController;
    const client = this.clientForSession();
    client
      .streamPatch(taskId, (event) => {
        if (event.type === "planning_tool_call") {
          const tool = (event.payload as Record<string, unknown>)["tool"] as string ?? "";
          const action = tool === "read_file" ? "Reading file"
            : tool === "list_directory" ? "Listing directory"
            : tool === "search_code" ? "Searching codebase"
            : tool === "search_semantic" ? "Semantic search"
            : tool;
          this.ui.updateChatThinking(`Replanning: ${action}…`);
        } else if (event.type === "planning_complete") {
          void this.clientForSession().getTask(taskId).then((task) => {
            if (task.planMarkdown) {
              this.ui.appendChatMessage({
                role: "agent",
                content: task.planMarkdown,
                type: "plan_card",
                taskId,
                timestamp: this.now(),
                metadata: { taskId, plan_markdown: task.planMarkdown },
              });
            }
            this.ui.hideChatThinking();
            this.ui.setChatInputEnabled(true);
          });
        } else if (event.type === "scope_extension_requested") {
          // Fire and forget — prompt the user; on response, post the decision.
          void this.handleScopeExtensionRequest(taskId, event);
        }
        // step_review_requested: step review surfaces via the /live poll (renderLiveGate kind "step").
      }, signal)
      .catch((err: unknown) => {
        if (err instanceof Error && err.name === "AbortError") return;
        this.ui.showWarning(`Patch stream error: ${err instanceof Error ? err.message : String(err)}`);
      })
      .finally(() => {
        this.streamController = null;
      });
  }

  async handleScopeDecisionFromChat(
    taskId: string,
    files: string[],
    decision: "approve" | "reject",
    remember: boolean
  ): Promise<void> {
    try {
      await this.clientForChat().sendScopeDecision(
        this.liveTaskIdOr(taskId),
        { decision, files: decision === "approve" ? files : [], remember }
      );
    } catch (err) {
      if (this.isBenignConflict(err)) return;
      this.ui.showError(`Failed to send scope decision: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  async handleValidationDecisionFromChat(
    taskId: string,
    decision: "accept" | "reject"
  ): Promise<void> {
    try {
      await this.clientForChat().sendValidationDecision(this.liveTaskIdOr(taskId), decision);
    } catch (err) {
      if (this.isBenignConflict(err)) return;
      this.ui.showError(`Failed to send validation decision: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  async handleCommandDecisionFromChat(
    taskId: string,
    decision: CommandDecision
  ): Promise<void> {
    try {
      // A controller (chat EDIT) command gate has no task — /live reports activeTaskId
      // null and the gate id is the thread id (LiveSlot renders activeTaskId ?? threadId).
      // Route it to the chat endpoint; otherwise it's a task gate → the task route. Mirrors
      // handleEditDecisionFromChat. The undefined-latestLiveState case (no poll yet) stays
      // on the task route — backward-compatible with the direct task-gate path.
      if (this.latestLiveState != null && this.latestLiveState.activeTaskId == null) {
        await this.clientForChat().postChatCommandDecision(taskId, decision);
        return;
      }
      await this.clientForChat().sendCommandDecision(this.liveTaskIdOr(taskId), decision);
    } catch (err) {
      if (this.isBenignConflict(err)) return;
      this.ui.showError(`Failed to send command decision: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  private async handleScopeExtensionRequest(
    taskId: string,
    event: Extract<StreamEvent, { type: "scope_extension_requested" }>
  ): Promise<void> {
    const result = await this.ui.promptForScopeDecision({
      files: event.payload.files,
      reason: event.payload.reason,
      stepId: event.payload.step_id
    });
    if (!result) return; // user dismissed — task stays paused until timeout

    const client = this.clientForChat();
    try {
      await client.sendScopeDecision(this.liveTaskIdOr(taskId), {
        decision: result.decision,
        files: result.decision === "approve" ? event.payload.files : [],
        remember: result.remember
      });
    } catch (err) {
      if (this.isBenignConflict(err)) return;
      this.ui.showError(
        `Failed to send scope decision: ${err instanceof Error ? err.message : String(err)}`
      );
    }
  }

  private stopStream(): void {
    this.streamController?.abort();
    this.streamController = null;
  }

  private syncStream(status: TaskStatus, taskId: string): void {
    if (
      status === "QUEUED" ||
      status === "CONTEXT_READY" ||
      status === "PLANNED" ||
      status === "EXECUTING" ||
      status === "REPAIRING"
    ) {
      this.startStream(taskId);
    } else {
      this.stopStream();
    }
  }

  private async pullLatestTask(): Promise<void> {
    if (!this.session) {
      return;
    }

    const client = this.clientForSession();
    let task: TaskView;
    try {
      task = await client.getTask(this.session.taskId);
    } catch (error) {
      this.ui.showWarning(`Failed to refresh task state: ${formatError(error)}`);
      return;
    }

    this.latestTask = task;
    this.session = {
      ...this.session,
      status: task.status,
      updatedAt: this.now(),
    };
    await this.sessionStore.save(this.session);

    this.syncStream(task.status, task.taskId);

    if (shouldStopPolling(task.status)) {
      this.stopPolling();
    }

    if (shouldLoadResult(task.status)) {
      try {
        this.latestResult = await client.getTaskResult(this.session.taskId);
      } catch (error) {
        this.ui.showWarning(`Task result unavailable: ${formatError(error)}`);
      }
    }

    if (TERMINAL_STATUSES.has(task.status)) {
      this.ui.showInfo(`Task ${task.taskId} is ${task.status}.`);
    }
  }

  private startPolling(): void {
    this.stopPolling();

    if (!this.session) {
      return;
    }

    this.poller = new TaskPoller({
      intervalMs: this.settings.getPollIntervalMs(),
      poll: async () => this.clientForSession().getTask(this.session!.taskId),
      onUpdate: async (task) => {
        this.latestTask = task;
        this.session = {
          ...this.session!,
          status: task.status,
          updatedAt: this.now(),
        };
        await this.sessionStore.save(this.session);

        this.syncStream(task.status, task.taskId);

        if (shouldLoadResult(task.status)) {
          try {
            this.latestResult = await this.clientForSession().getTaskResult(this.session.taskId);
          } catch {
            // keep latest known result when retrieval fails
          }
        }
      },
      onError: (error) => {
        this.ui.showWarning(`Polling failed: ${formatError(error)}`);
      },
    });

    this.poller.start();
  }

  private stopPolling(): void {
    this.poller?.stop();
    this.poller = null;
  }

  private clientForChat(): BackendTaskClient {
    return this.createClient(this.settings.getBackendBaseUrl());
  }

  /**
   * The task id an action should target right now. Resume churns the task id
   * (parent→child); the card may have been rendered against an older id, so we
   * prefer the live state's current active_task_id and fall back to the card's.
   */
  private liveTaskIdOr(fallback: string): string {
    return this.latestLiveState?.activeTaskId ?? fallback;
  }

  /**
   * A 409 from a decision POST means the gate already moved on (resolved by a newer
   * poll, a timeout, or another client). It's benign — the next /live poll reconciles
   * the card — so swallow it rather than alarming the user with an error toast.
   */
  private isBenignConflict(err: unknown): boolean {
    return (
      typeof err === "object" &&
      err !== null &&
      (err as { status?: number }).status === 409
    );
  }

  /** Start polling the active thread's /live state. Idempotent. */
  private startLiveStatePolling(): void {
    if (this.liveStateTimer) {
      return;
    }
    const intervalMs = Math.max(this.settings.getPollIntervalMs(), 500);
    this.liveStateTimer = setInterval(() => {
      void this.pollThreadLiveState();
    }, intervalMs);
    void this.pollThreadLiveState();
  }

  private stopLiveStatePolling(): void {
    if (this.liveStateTimer) {
      clearInterval(this.liveStateTimer);
      this.liveStateTimer = null;
    }
  }

  /**
   * Reconcile the active thread's live cards from persisted backend state.
   * Renders exactly one gate card (per kind) and one plan card, replace-not-append,
   * removed when null. Signature-deduped so an unchanged poll causes no webview churn.
   * This is what makes gate/plan cards reappear after a webview reload.
   */
  async pollThreadLiveState(): Promise<void> {
    if (this.livePollInFlight) return;
    this.livePollInFlight = true;
    try {
    const threadId = this.activeThreadId;
    if (!threadId) {
      return;
    }
    let live: ThreadLiveState;
    try {
      live = await this.clientForChat().getThreadLiveState(threadId);
    } catch {
      // Transient backend/poll error — keep the last rendered cards; next tick retries.
      return;
    }
    this.latestLiveState = live;

    // Live-resume: a fresh webview (reload mid-turn) reconstructs the transcript from
    // the thread fetch, but the live overlay (streaming pills/chunks) died with the old
    // SSE. When /live reports an in-flight turn or a controller gate, re-subscribe to the
    // chat channel (subscribe-only — does NOT relaunch the turn) to resume the overlay
    // from the broadcaster's replay buffer onward. Idempotent via _liveResumeThreadId.
    //
    // CRITICAL: only resume when NO local turn stream is active (`turnAbort === null`).
    // During a normal sendChatMessage/mode-decision turn, turnAbort is set and /live also
    // reports turn_active=true — without this guard the 1s poll would open a SECOND
    // streamTurn on the same channel (the broadcaster replays its buffer to every new
    // subscriber), double-rendering every event and clobbering the live turnAbort. A
    // fresh webview after a reload has turnAbort === null, which is exactly when resume
    // is needed (the original stream died with the old SSE; the detached turn lives on).
    const channelActive = live.turnActive || live.pendingGate?.kind === "mode"
      || live.pendingGate?.kind === "edit";
    if (channelActive && this.turnAbort === null && this._liveResumeThreadId !== threadId) {
      this._liveResumeThreadId = threadId;
      void this.resumeLiveOverlay(threadId);
    } else if (!channelActive && this._liveResumeThreadId === threadId) {
      this._liveResumeThreadId = null; // turn ended — allow a future resume
    }

    const signature = JSON.stringify({
      taskId: live.activeTaskId,
      status: live.status,
      // turnActive drives composer enable/disable and is independent of the cards: a
      // controller chat turn has no task, so status/gate/plan stay null for the whole
      // turn. Omitting turnActive here lets the turn-end transition (true→false) get
      // deduped away, so sendLiveStatus(..., false) is never delivered and the composer
      // stays wedged on "Agent is working…". Same dedup-lock bug class as runSummary/
      // narrative/failure below.
      turnActive: live.turnActive,
      gate: live.pendingGate,
      plan: live.plan,
      // Durable telemetry is finalized server-side AFTER the READY_FOR_REVIEW/terminal
      // status save (the narrative is a later LLM call), so it arrives on a poll where
      // status is unchanged. Without these, the dedup locks on the first render and the
      // Review/Error card never picks up the run_summary/narrative until a reload.
      runSummary: live.runSummary,
      narrative: live.taskNarrative,
      failure: live.failureSummary,
      // INVARIANT (see CLAUDE.md /live dedup): the todo checklist is a durable signal
      // consumed after this gate — it MUST be in the signature, or a checklist change
      // (e.g. an item flipped to done) is deduped away and the card never updates.
      todos: live.todos,
    });
    if (signature === this.lastLiveSignature) {
      return; // dedup — nothing actionable changed
    }
    this.lastLiveSignature = signature;

    if (live.pendingGate) {
      this.ui.renderLiveGate({
        kind: live.pendingGate.kind,
        payload: live.pendingGate.payload,
        // Controller gates (mode/edit) have NO task — fall back to the thread id so
        // the gate still renders (the render guard previously required activeTaskId).
        taskId: live.activeTaskId ?? threadId,
      });
    } else {
      this.ui.clearLiveGate();
    }

    if (live.plan && live.activeTaskId) {
      this.ui.renderLivePlan({
        taskId: live.activeTaskId,
        planMarkdown: String(live.plan["plan_markdown"] ?? ""),
      });
    } else {
      this.ui.clearLivePlan();
    }

    if (live.status === "READY_FOR_REVIEW" && live.activeTaskId) {
      try {
        // Signature invariant: this block reads getTaskResult fields (modifiedFiles,
        // shadowWorkspacePath, plan) that are NOT in the dedup signature. That is safe
        // ONLY because they are immutable once status===READY_FOR_REVIEW — the result is
        // frozen at that terminal-ish state, so a same-status re-poll can't carry a newer
        // result. If a future change lets the result mutate at unchanged status, add a
        // result fingerprint to the signature (same class as the turnActive fix above).
        const result = await this.clientForChat().getTaskResult(live.activeTaskId);
        // Tier B: prefer the durable run_summary (survives reload) over extension-observed
        // ephemeral counts; fall back to the ephemeral values for live-feel before it lands.
        const rs = live.runSummary;
        const review = {
          taskId: live.activeTaskId,
          modifiedFiles: result.modifiedFiles,
          shadowWorkspacePath: result.shadowWorkspacePath ?? null,
          stepsCompleted: rs ? rs.stepsCompleted : (this.seenStepIds.size > 0 ? this.seenStepIds.size : null),
          stepsTotal: rs
            ? rs.stepsTotal
            : (result.plan && Array.isArray((result.plan as { steps?: unknown[] }).steps)
              ? ((result.plan as { steps: unknown[] }).steps.length)
              : null),
          deviations: rs
            ? rs.deviations
            : (this.deviationsTaskId === live.activeTaskId ? [...this.runDeviations] : []),
          ...(live.taskNarrative
            ? { narrative: { headline: live.taskNarrative.headline, points: live.taskNarrative.points } }
            : {}),
        };
        this.latestLiveReview = { taskId: review.taskId, shadowWorkspacePath: review.shadowWorkspacePath };
        this.ui.renderLiveReview(review);
      } catch {
        // result not ready yet — reset signature so the next poll retries instead of deduping
        this.lastLiveSignature = null;
      }
    } else {
      this.latestLiveReview = null;
      this.ui.clearLiveReview();
    }

    if ((live.status === "FAILED" || live.status === "ABORTED") && live.activeTaskId) {
      // Tier B: prefer the durable failure_summary (survives reload); fall back to the
      // extension-observed ephemeral step/patch detail for live-feel before it lands.
      const fs = live.failureSummary;
      const detailParts: string[] = [];
      if (fs) {
        if (fs.stepIndex != null) detailParts.push(`step ${fs.stepIndex}`);
        detailParts.push(`${fs.errorClass}: ${fs.message}`);
      } else {
        if (this.lastStepStarted) {
          detailParts.push(`${this.lastStepStarted.stepTitle} (step ${this.lastStepStarted.stepIndex} of ${this.lastStepStarted.totalSteps})`);
        }
        if (this.lastPatchError) detailParts.push(this.lastPatchError);
      }
      this.ui.renderLiveError({
        taskId: live.activeTaskId,
        status: live.status,
        ...(detailParts.length ? { detail: detailParts.join(" — ") } : {}),
        ...(live.taskNarrative
          ? { narrative: { headline: live.taskNarrative.headline, points: live.taskNarrative.points } }
          : {}),
      });
    } else {
      this.ui.clearLiveError();
    }

    if (live.todos && live.todos.length > 0) {
      this.ui.renderLiveTodos({ items: live.todos });
    } else {
      this.ui.clearLiveTodos();
    }

    this.ui.sendLiveStatus(live.status ?? null, live.turnActive ?? false);
    } finally {
      this.livePollInFlight = false;
    }
  }

  private clientForSession(): BackendTaskClient {
    if (!this.session) {
      throw new Error("No active session");
    }
    return this.createClient(this.session.backendBaseUrl);
  }
}

function isConflictError(error: unknown): boolean {
  return formatError(error).includes("(409 ");
}

function shouldLoadResult(status: TaskStatus): boolean {
  return status === "READY_FOR_REVIEW" || TERMINAL_STATUSES.has(status);
}

function formatError(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }
  return String(error);
}
