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
  renderLiveReview(review: { taskId: string; modifiedFiles: string[]; shadowWorkspacePath: string | null; stepsCompleted: number | null; stepsTotal: number | null; deviations: string[] }): void;
  clearLiveReview(): void;
  renderLiveError(error: { taskId: string; status: "FAILED" | "ABORTED"; detail?: string }): void;
  clearLiveError(): void;
  sendLiveStatus(status: string | null): void;
}

export interface LiveGateView {
  kind: "command" | "step" | "scope" | "validation";
  payload: Record<string, unknown>;
  taskId: string;
}

export interface LivePlanView {
  taskId: string;
  planMarkdown: string;
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
    for (const message of thread.messages) {
      this.ui.appendChatMessage(message);
    }
    this.startLiveStatePolling();
  }

  async sendChatMessage(text: string): Promise<void> {
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
    let currentTaskId: string | undefined;
    try {
      this.openToolEvent = {}; // defensive: clear any stale ids from a previous turn
      for await (const event of client.sendChatMessage(threadId, text, this.turnAbort.signal)) {
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
        } else if (event.type === "diff_ready") {
          const taskId = (event.payload["task_id"] as string) ?? "";
          this.ui.appendChatMessage({
            role: "agent",
            content: taskId,
            type: "diff_card",
            taskId,
            timestamp: this.now(),
            metadata: {
              diff_entries: event.payload["diff_entries"],
              thinking_log: event.payload["thinking_log"] ?? [],
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

  async streamTaskIntoChatThread(taskId: string): Promise<void> {
    const client = this.createClient(this.settings.getBackendBaseUrl());
    this.ui.setChatInputEnabled(false);
    this.ui.appendChatThinkingEntry("Generating execution plan…");
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
      this.ui.showInfo("Task closed — applied changes were kept.");
      this.appendBreadcrumbMessage(
        taskId,
        "✗ Task closed without finishing — applied changes kept; task marked aborted.",
      );
    } catch (error) {
      if (this.isBenignConflict(error)) return;
      this.ui.showError(`Failed to close task: ${formatError(error)}`);
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
    try {
      const response = await this.clientForChat().resumeTask(taskId, { stage });
      this.ui.showInfo(`Resumed as ${response.taskId}`);
      // the child task is a fresh run — never let the parent's step/error history describe it
      this.seenStepIds.clear();
      this.stepTrackingTaskId = null;
      this.lastStepStarted = null;
      this.lastPatchError = null;
      this.runDeviations = [];
      this.deviationsTaskId = null;
      // Force the next poll to re-render against the child task (signature reset).
      this.lastLiveSignature = null;
      void this.pollThreadLiveState();
    } catch (error) {
      this.ui.showError(`Failed to resume: ${formatError(error)}`);
    }
  }

  dispose(): void {
    this.stopPolling();
    this.stopStream();
    this.stopLiveStatePolling();
  }

  stopActiveTurn(): void {
    this.turnAbort?.abort();
  }

  private forwardToolCall(source: "explore" | "execution" | "planning", payload: Record<string, unknown>): void {
    const id = ++this.toolEventSeq;
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

    const signature = JSON.stringify({
      taskId: live.activeTaskId,
      status: live.status,
      gate: live.pendingGate,
      plan: live.plan,
    });
    if (signature === this.lastLiveSignature) {
      return; // dedup — nothing actionable changed
    }
    this.lastLiveSignature = signature;

    if (live.pendingGate && live.activeTaskId) {
      this.ui.renderLiveGate({
        kind: live.pendingGate.kind,
        payload: live.pendingGate.payload,
        taskId: live.activeTaskId,
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
        const result = await this.clientForChat().getTaskResult(live.activeTaskId);
        const review = {
          taskId: live.activeTaskId,
          modifiedFiles: result.modifiedFiles,
          shadowWorkspacePath: result.shadowWorkspacePath ?? null,
          stepsCompleted: this.seenStepIds.size > 0 ? this.seenStepIds.size : null,
          stepsTotal: result.plan && Array.isArray((result.plan as { steps?: unknown[] }).steps)
            ? ((result.plan as { steps: unknown[] }).steps.length)
            : null,
          deviations: this.deviationsTaskId === live.activeTaskId ? [...this.runDeviations] : [],
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
      const detailParts: string[] = [];
      if (this.lastStepStarted) {
        detailParts.push(`${this.lastStepStarted.stepTitle} (step ${this.lastStepStarted.stepIndex} of ${this.lastStepStarted.totalSteps})`);
      }
      if (this.lastPatchError) detailParts.push(this.lastPatchError);
      this.ui.renderLiveError({
        taskId: live.activeTaskId,
        status: live.status,
        ...(detailParts.length ? { detail: detailParts.join(" — ") } : {}),
      });
    } else {
      this.ui.clearLiveError();
    }

    this.ui.sendLiveStatus(live.status ?? null);
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
