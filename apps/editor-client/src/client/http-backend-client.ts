import {
  TaskStatusSchema,
  TaskResultSchema,
  TaskSubmissionSchema,
  TaskViewSchema,
  type BackendTaskClient,
  type TaskResult,
  type TaskSubmission,
  type TaskView
} from "../contracts/task-contracts.js";
import type { TaskStatus } from "../domain/types.js";

interface FetchLike {
  (input: string, init?: RequestInit): Promise<Response>;
}

interface HttpBackendClientOptions {
  baseUrl: string;
  fetchFn?: FetchLike;
}

export class HttpBackendClient implements BackendTaskClient {
  private readonly fetchFn: FetchLike;

  constructor(private readonly options: HttpBackendClientOptions) {
    this.fetchFn = options.fetchFn ?? fetch;
  }

  async submitTask(input: TaskSubmission): Promise<{ taskId: string }> {
    const payload = TaskSubmissionSchema.parse(input);
    const response = await this.fetchJson("/v1/tasks", {
      method: "POST",
      body: JSON.stringify({
        goal: payload.goal,
        workspace_path: payload.workspacePath,
        mode: payload.mode
      })
    });
    return { taskId: this.readString(response, "taskId", "task_id") };
  }

  async getTask(taskId: string): Promise<TaskView> {
    const response = await this.fetchJson(`/v1/tasks/${encodeURIComponent(taskId)}`);
    return this.toTaskView(response);
  }

  async getTaskResult(taskId: string): Promise<TaskResult> {
    const response = await this.fetchJson(`/v1/tasks/${encodeURIComponent(taskId)}/result`);
    return this.toTaskResult(response);
  }

  async cancelTask(taskId: string): Promise<{ taskId: string; status: TaskStatus }> {
    const response = await this.fetchJson(`/v1/tasks/${encodeURIComponent(taskId)}/cancel`, {
      method: "POST"
    });

    const view = this.toTaskView(response);
    return {
      taskId: view.taskId,
      status: TaskStatusSchema.parse(view.status)
    };
  }

  async acceptPatch(taskId: string): Promise<TaskResult> {
    const response = await this.fetchJson(`/v1/tasks/${encodeURIComponent(taskId)}/accept`, {
      method: "POST"
    });
    return this.toTaskResult(response);
  }

  async rejectPatch(taskId: string, reason: string): Promise<TaskResult> {
    const response = await this.fetchJson(`/v1/tasks/${encodeURIComponent(taskId)}/reject`, {
      method: "POST",
      body: JSON.stringify({ reason })
    });
    return this.toTaskResult(response);
  }

  private async fetchJson(path: string, init: RequestInit = {}): Promise<unknown> {
    const response = await this.fetchFn(`${this.options.baseUrl}${path}`, {
      ...init,
      headers: {
        "content-type": "application/json",
        ...(init.headers ?? {})
      }
    });

    if (!response.ok) {
      throw new Error(`Backend request failed (${response.status} ${response.statusText}) for ${path}`);
    }

    return response.json();
  }

  private toTaskView(raw: unknown): TaskView {
    return TaskViewSchema.parse({
      taskId: this.readString(raw, "taskId", "task_id"),
      status: this.readUnknown(raw, "status"),
      goal: this.readString(raw, "goal"),
      modifiedFiles: this.readArray(raw, "modifiedFiles", "modified_files"),
      diagnostics: this.normalizeDiagnostics(raw)
    });
  }

  private toTaskResult(raw: unknown): TaskResult {
    return TaskResultSchema.parse({
      taskId: this.readString(raw, "taskId", "task_id"),
      status: this.readUnknown(raw, "status"),
      plan: this.readOptionalUnknown(raw, "plan"),
      patch: this.readOptionalUnknown(raw, "patch"),
      modifiedFiles: this.readArray(raw, "modifiedFiles", "modified_files"),
      diagnostics: this.normalizeDiagnostics(raw),
      promotedAt: this.readOptionalNullableString(raw, "promotedAt", "promoted_at"),
      shadowWorkspacePath: this.readOptionalNullableString(
        raw,
        "shadowWorkspacePath",
        "shadow_workspace_path"
      )
    });
  }

  private normalizeDiagnostics(raw: unknown): unknown[] {
    const diagnostics = this.readArray(raw, "diagnostics");
    return diagnostics.map((item) => this.normalizeDiagnostic(item));
  }

  private normalizeDiagnostic(raw: unknown): Record<string, unknown> {
    const record = this.readRecord(raw);
    const normalized: Record<string, unknown> = {
      source: this.readString(record, "source"),
      message: this.readString(record, "message"),
      level: this.readString(record, "level")
    };

    this.assignNullableString(record, normalized, "file");
    this.assignNullableInteger(record, normalized, "line");
    this.assignNullableInteger(record, normalized, "column");
    return normalized;
  }

  private assignNullableString(
    source: Record<string, unknown>,
    target: Record<string, unknown>,
    key: string
  ): void {
    const value = source[key];
    if (value === undefined || value === null) {
      return;
    }
    target[key] = String(value);
  }

  private assignNullableInteger(
    source: Record<string, unknown>,
    target: Record<string, unknown>,
    key: string
  ): void {
    const value = source[key];
    if (value === undefined || value === null) {
      return;
    }
    target[key] = value;
  }

  private readRecord(raw: unknown): Record<string, unknown> {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
      throw new Error("Unexpected backend payload shape");
    }
    return raw as Record<string, unknown>;
  }

  private readUnknown(raw: unknown, key: string, fallbackKey?: string): unknown {
    const record = this.readRecord(raw);
    if (key in record) {
      return record[key];
    }
    if (fallbackKey && fallbackKey in record) {
      return record[fallbackKey];
    }
    throw new Error(`Missing required field '${key}' from backend payload`);
  }

  private readOptionalUnknown(raw: unknown, key: string, fallbackKey?: string): unknown | undefined {
    const record = this.readRecord(raw);
    if (key in record) {
      return record[key];
    }
    if (fallbackKey && fallbackKey in record) {
      return record[fallbackKey];
    }
    return undefined;
  }

  private readString(raw: unknown, key: string, fallbackKey?: string): string {
    const value = this.readUnknown(raw, key, fallbackKey);
    return String(value);
  }

  private readArray(raw: unknown, key: string, fallbackKey?: string): unknown[] {
    const value = this.readUnknown(raw, key, fallbackKey);
    if (!Array.isArray(value)) {
      throw new Error(`Field '${key}' must be an array`);
    }
    return value;
  }

  private readOptionalNullableString(
    raw: unknown,
    key: string,
    fallbackKey?: string
  ): string | null | undefined {
    const value = this.readOptionalUnknown(raw, key, fallbackKey);
    if (value === undefined || value === null) {
      return value as null | undefined;
    }
    return String(value);
  }
}
