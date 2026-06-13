import {
  TaskStatusSchema,
  TaskResultSchema,
  TaskSubmissionSchema,
  TaskViewSchema,
  ResumeTaskResponseSchema,
  ScopeDecisionResponseSchema,
  ValidationDecisionResponseSchema,
  CommandDecisionResponseSchema,
  ChatThreadSummarySchema,
  ChatThreadSchema,
  ChatEventSchema,
  ThreadLiveStateSchema,
  type BackendTaskClient,
  type ThreadLiveState,
  type PatchStreamEvent,
  type TaskResult,
  type TaskSubmission,
  type TaskView,
  type ResumeTaskRequest,
  type ResumeTaskResponse,
  type ScopeDecisionRequest,
  type ScopeDecisionResponse,
  type CommandDecision,
  type CommandDecisionResponse,
  type ValidationDecisionResponse,
  type ChatThreadSummary,
  type ChatThread,
  type StreamEvent,
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

  async abortTask(taskId: string, options: { revert: boolean }): Promise<TaskView> {
    const response = await this.fetchJson(`/v1/tasks/${encodeURIComponent(taskId)}/abort`, {
      method: "POST",
      body: JSON.stringify({ revert: options.revert })
    });
    return this.toTaskView(response);
  }

  async setReviewPref(taskId: string, options: { autoAccept: boolean }): Promise<TaskView> {
    const response = await this.fetchJson(
      `/v1/tasks/${encodeURIComponent(taskId)}/review-pref`,
      {
        method: "POST",
        body: JSON.stringify({ auto_accept: options.autoAccept })
      }
    );
    return this.toTaskView(response);
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

  async providePlanFeedback(taskId: string, feedback: string | null): Promise<TaskView> {
    const response = await this.fetchJson(`/v1/tasks/${encodeURIComponent(taskId)}/plan/feedback`, {
      method: "POST",
      body: JSON.stringify({ feedback })
    });
    return this.toTaskView(response);
  }

  async sendScopeDecision(
    taskId: string,
    decision: ScopeDecisionRequest
  ): Promise<ScopeDecisionResponse> {
    const response = await this.fetchJson(
      `/v1/tasks/${encodeURIComponent(taskId)}/scope-decision`,
      {
        method: "POST",
        body: JSON.stringify({
          decision: decision.decision,
          files: decision.files ?? [],
          remember: decision.remember ?? false
        })
      }
    );
    return ScopeDecisionResponseSchema.parse({
      taskId: this.readString(response, "taskId", "task_id"),
      status: this.readString(response, "status")
    });
  }

  async sendValidationDecision(
    taskId: string,
    decision: "accept" | "reject"
  ): Promise<ValidationDecisionResponse> {
    const response = await this.fetchJson(
      `/v1/tasks/${encodeURIComponent(taskId)}/validation-decision`,
      {
        method: "POST",
        body: JSON.stringify({ decision })
      }
    );
    return ValidationDecisionResponseSchema.parse({
      taskId: this.readString(response, "taskId", "task_id"),
      status: this.readString(response, "status")
    });
  }

  async sendCommandDecision(
    taskId: string,
    decision: CommandDecision
  ): Promise<CommandDecisionResponse> {
    // camelCase → snake_case for the backend wire (ruleValue → rule_value).
    const body: Record<string, unknown> = {
      approve: decision.approve,
      remember: decision.remember,
      scope: decision.scope,
    };
    if (decision.ruleValue !== undefined) body.rule_value = decision.ruleValue;
    const response = await this.fetchJson(
      `/v1/tasks/${encodeURIComponent(taskId)}/command-decision`,
      {
        method: "POST",
        body: JSON.stringify(body)
      }
    );
    return CommandDecisionResponseSchema.parse({
      taskId: this.readString(response, "taskId", "task_id"),
      status: this.readString(response, "status")
    });
  }

  async sendStepDecision(taskId: string, decision: "accept" | "discard"): Promise<void> {
    await this.fetchJson(
      `/v1/tasks/${encodeURIComponent(taskId)}/step-decision`,
      {
        method: "POST",
        body: JSON.stringify({ decision })
      }
    );
  }

  async resumeTask(taskId: string, options?: ResumeTaskRequest): Promise<ResumeTaskResponse> {
    const body: Record<string, unknown> = { stage: options?.stage ?? "execute" };
    if (options?.budgetOverride) {
      body["budget_override"] = {
        max_iterations: options.budgetOverride.maxIterations,
        max_tokens: options.budgetOverride.maxTokens,
        max_files_touched: options.budgetOverride.maxFilesTouched,
        max_runtime_ms: options.budgetOverride.maxRuntimeMs
      };
    }
    const response = await this.fetchJson(
      `/v1/tasks/${encodeURIComponent(taskId)}/resume`,
      { method: "POST", body: JSON.stringify(body) }
    );
    return ResumeTaskResponseSchema.parse({
      taskId: this.readString(response, "taskId", "task_id"),
      resumeOfTaskId: this.readString(response, "resumeOfTaskId", "resume_of_task_id")
    });
  }

  async streamPatch(
    taskId: string,
    onEvent: (event: PatchStreamEvent) => void,
    signal?: AbortSignal
  ): Promise<void> {
    const response = await this.fetchFn(
      `${this.options.baseUrl}/v1/tasks/${encodeURIComponent(taskId)}/stream-patch`,
      { signal: signal ?? null, headers: { accept: "text/event-stream" } }
    );
    if (!response.ok) {
      throw new Error(`Stream failed (${response.status}) for task ${taskId}`);
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
          if (!line.startsWith("data: ")) continue;
          try {
            const event = JSON.parse(line.slice(6)) as PatchStreamEvent;
            onEvent(event);
            if (event.type === "done") return;
          } catch {
            // skip malformed SSE line
          }
        }
      }
    } finally {
      reader.cancel().catch(() => {});
    }
  }

  async *streamPatchEvents(taskId: string): AsyncIterable<PatchStreamEvent> {
    // Wraps the callback-based streamPatch as an async iterable so callers
    // can use for-await without holding an AbortController.
    const events: PatchStreamEvent[] = [];
    let notify: (() => void) | null = null;
    let streamDone = false;

    const push = (event: PatchStreamEvent) => {
      events.push(event);
      notify?.();
      notify = null;
    };

    const streamPromise = this.streamPatch(taskId, push);
    streamPromise.finally(() => {
      streamDone = true;
      notify?.();
      notify = null;
    });

    while (true) {
      if (events.length === 0 && !streamDone) {
        await new Promise<void>((resolve) => { notify = resolve; });
      }
      while (events.length > 0) {
        const event = events.shift()!;
        yield event;
        if (event.type === "done") return;
      }
      if (streamDone) return;
    }
  }

  async listChatThreads(workspacePath: string): Promise<ChatThreadSummary[]> {
    const raw = await this.fetchJson(
      `/v1/chat/threads?workspace=${encodeURIComponent(workspacePath)}`
    ) as Record<string, unknown>;
    const threads = Array.isArray(raw["threads"]) ? raw["threads"] : [];
    return (threads as Record<string, unknown>[]).map((t) =>
      ChatThreadSummarySchema.parse({
        threadId: t["thread_id"],
        workspacePath: t["workspace_path"],
        title: t["title"],
        createdAt: t["created_at"],
        updatedAt: t["updated_at"] ?? undefined,
        messageCount: t["message_count"] ?? undefined,
        status: t["status"] ?? null,
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
      messages: (messages as Record<string, unknown>[]).map((m) => ({
        role: m["role"],
        content: m["content"],
        type: m["type"] ?? "text",
        taskId: m["task_id"] ?? null,
        timestamp: typeof m["timestamp"] === "string"
          ? m["timestamp"]
          : new Date(m["timestamp"] as string).toISOString(),
        metadata: (typeof m["metadata"] === "object" && m["metadata"] !== null)
          ? m["metadata"]
          : {},
      })),
      touchedFiles: Array.isArray(raw["touched_files"]) ? raw["touched_files"] : [],
    });
  }

  async getThreadLiveState(threadId: string): Promise<ThreadLiveState> {
    const raw = await this.fetchJson(
      `/v1/chat/threads/${encodeURIComponent(threadId)}/live`
    ) as Record<string, unknown>;
    const gate = raw["pending_gate"] as Record<string, unknown> | null;
    return ThreadLiveStateSchema.parse({
      activeTaskId: raw["active_task_id"] ?? null,
      status: raw["status"] ?? null,
      pendingGate: gate
        ? { kind: gate["kind"], payload: gate["payload"] ?? {} }
        : null,
      plan: raw["plan"] ?? null,
      failureSummary: this.toFailureSummary(raw),
      runSummary: this.toRunSummary(raw),
    });
  }

  async *sendChatMessage(threadId: string, message: string, signal?: AbortSignal, options?: { stepReview?: boolean }): AsyncIterable<StreamEvent> {
    const response = await this.fetchFn(
      `${this.options.baseUrl}/v1/chat/threads/${encodeURIComponent(threadId)}/message`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          content: message,
          ...(options?.stepReview !== undefined ? { step_review: options.stepReview } : {}),
        }),
        signal: signal ?? null,
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
            yield ChatEventSchema.parse(JSON.parse(line.slice(5).trim())) as StreamEvent;
          } catch {
            // skip malformed SSE line
          }
        }
      }
    } finally {
      reader.cancel().catch(() => {});
    }
  }

  async applyInlineChange(inlineTaskId: string): Promise<void> {
    await this.fetchJson(
      `/v1/chat/inline-changes/${encodeURIComponent(inlineTaskId)}/promote`,
      { method: "POST" }
    );
  }

  async discardInlineChange(inlineTaskId: string): Promise<void> {
    await this.fetchFn(
      `${this.options.baseUrl}/v1/chat/inline-changes/${encodeURIComponent(inlineTaskId)}`,
      { method: "DELETE", headers: { "content-type": "application/json" } }
    );
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
      const error = new Error(
        `Backend request failed (${response.status} ${response.statusText}) for ${path}`
      ) as Error & { status?: number };
      // Surface the status so callers can treat a benign 409 (gate already moved on)
      // differently from a real failure, without parsing the message string.
      error.status = response.status;
      throw error;
    }

    return response.json();
  }

  // Tier B durable telemetry: map the snake_case wire summaries to camelCase, or
  // undefined when absent/null so the optional schema fields pass.
  private toFailureSummary(raw: unknown): unknown | undefined {
    const value = this.readOptionalUnknown(raw, "failureSummary", "failure_summary");
    if (!value || typeof value !== "object") return undefined;
    const r = value as Record<string, unknown>;
    return {
      stepId: r["stepId"] ?? r["step_id"] ?? null,
      stepIndex: r["stepIndex"] ?? r["step_index"] ?? null,
      errorClass: r["errorClass"] ?? r["error_class"],
      message: r["message"]
    };
  }

  private toRunSummary(raw: unknown): unknown | undefined {
    const value = this.readOptionalUnknown(raw, "runSummary", "run_summary");
    if (!value || typeof value !== "object") return undefined;
    const r = value as Record<string, unknown>;
    return {
      stepsCompleted: r["stepsCompleted"] ?? r["steps_completed"],
      stepsTotal: r["stepsTotal"] ?? r["steps_total"],
      deviations: r["deviations"] ?? []
    };
  }

  private toTaskView(raw: unknown): TaskView {
    return TaskViewSchema.parse({
      taskId: this.readString(raw, "taskId", "task_id"),
      status: this.readUnknown(raw, "status"),
      goal: this.readString(raw, "goal"),
      modifiedFiles: this.readArray(raw, "modifiedFiles", "modified_files"),
      diagnostics: this.normalizeDiagnostics(raw),
      planMarkdown: this.readOptionalString(raw, "planMarkdown", "plan_markdown"),
      resumeOfTaskId: this.readOptionalString(raw, "resumeOfTaskId", "resume_of_task_id"),
      failureSummary: this.toFailureSummary(raw),
      runSummary: this.toRunSummary(raw)
    });
  }

  private toTaskResult(raw: unknown): TaskResult {
    return TaskResultSchema.parse({
      taskId: this.readString(raw, "taskId", "task_id"),
      status: this.readUnknown(raw, "status"),
      plan: this.readOptionalUnknown(raw, "plan"),
      planMarkdown: this.readOptionalString(raw, "planMarkdown", "plan_markdown"),
      patch: this.readOptionalUnknown(raw, "patch"),
      modifiedFiles: this.readArray(raw, "modifiedFiles", "modified_files"),
      diagnostics: this.normalizeDiagnostics(raw),
      promotedAt: this.readOptionalNullableString(raw, "promotedAt", "promoted_at"),
      shadowWorkspacePath: this.readOptionalNullableString(
        raw,
        "shadowWorkspacePath",
        "shadow_workspace_path"
      ),
      resumeOfTaskId: this.readOptionalString(raw, "resumeOfTaskId", "resume_of_task_id"),
      failureSummary: this.toFailureSummary(raw),
      runSummary: this.toRunSummary(raw)
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

  private readOptionalString(raw: unknown, key: string, fallbackKey?: string): string | undefined {
    const value = this.readOptionalUnknown(raw, key, fallbackKey);
    if (value === undefined || value === null) {
      return undefined;
    }
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
