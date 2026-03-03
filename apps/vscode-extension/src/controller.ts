import type {
  BackendTaskClient,
  TaskResult,
  TaskStatus,
  TaskSubmission,
  TaskView,
} from "@ai-editor/editor-client";

import { buildReviewFileEntries } from "./review-files.js";
import { SessionStore } from "./session-store.js";
import { shouldStopPolling, TaskPoller } from "./task-poller.js";
import type {
  ReviewFileEntry,
  ReviewPanelViewModel,
  TaskMode,
  TaskSessionState,
} from "./types.js";

const TERMINAL_STATUSES: ReadonlySet<TaskStatus> = new Set(["SUCCEEDED", "FAILED", "ABORTED"]);

export interface SettingsProvider {
  getBackendBaseUrl(): string;
  getDefaultMode(): TaskMode;
  getPollIntervalMs(): number;
}

export interface ControllerUI {
  getWorkspacePath(): string | null;
  promptForGoal(): Promise<string | undefined>;
  promptForRejectReason(): Promise<string | undefined>;
  showInfo(message: string): void;
  showWarning(message: string): void;
  showError(message: string): void;
  updatePanel(model: ReviewPanelViewModel): void;
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
      this.pushPanel();
      return;
    }

    this.session = restored;
    this.pushPanel();
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

    await this.sessionStore.save(this.session);
    this.pushPanel();
    this.startPolling();
    this.ui.showInfo(`Started AI Editor task ${submission.taskId}`);
  }

  openReviewPanel(): void {
    this.pushPanel();
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

  dispose(): void {
    this.stopPolling();
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

    this.pushPanel();
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

        if (shouldLoadResult(task.status)) {
          try {
            this.latestResult = await this.clientForSession().getTaskResult(this.session.taskId);
          } catch {
            // keep latest known result when retrieval fails
          }
        }

        this.pushPanel();
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

  private clientForSession(): BackendTaskClient {
    if (!this.session) {
      throw new Error("No active session");
    }
    return this.createClient(this.session.backendBaseUrl);
  }

  private pushPanel(): void {
    this.ui.updatePanel(this.buildViewModel());
  }

  private buildViewModel(): ReviewPanelViewModel {
    const shadowWorkspacePath = this.latestResult?.shadowWorkspacePath;
    const reviewFiles =
      this.session && shadowWorkspacePath
        ? buildReviewFileEntries(
            this.session.workspacePath,
            shadowWorkspacePath,
            this.latestResult?.modifiedFiles ?? []
          )
        : [];

    return {
      session: this.session,
      task: this.latestTask,
      result: this.latestResult,
      reviewFiles,
    };
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
