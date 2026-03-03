import type { TaskStatus, TaskView } from "@ai-editor/editor-client";

const STOP_STATUSES: ReadonlySet<TaskStatus> = new Set([
  "READY_FOR_REVIEW",
  "SUCCEEDED",
  "FAILED",
  "ABORTED",
]);

interface TaskPollerOptions {
  intervalMs: number;
  poll: () => Promise<TaskView>;
  onUpdate: (task: TaskView) => Promise<void> | void;
  onError?: (error: unknown) => void;
}

export function shouldStopPolling(status: TaskStatus): boolean {
  return STOP_STATUSES.has(status);
}

export class TaskPoller {
  private timer: ReturnType<typeof setInterval> | null = null;
  private inFlight = false;

  constructor(private readonly options: TaskPollerOptions) {}

  start(): void {
    if (this.timer) {
      return;
    }

    this.timer = setInterval(() => {
      void this.tick();
    }, Math.max(this.options.intervalMs, 100));

    void this.tick();
  }

  stop(): void {
    if (!this.timer) {
      return;
    }
    clearInterval(this.timer);
    this.timer = null;
  }

  isRunning(): boolean {
    return this.timer !== null;
  }

  private async tick(): Promise<void> {
    if (this.inFlight) {
      return;
    }

    this.inFlight = true;
    try {
      const task = await this.options.poll();
      await this.options.onUpdate(task);
      if (shouldStopPolling(task.status)) {
        this.stop();
      }
    } catch (error) {
      this.options.onError?.(error);
    } finally {
      this.inFlight = false;
    }
  }
}
