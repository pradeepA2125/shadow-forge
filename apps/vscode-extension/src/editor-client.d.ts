declare module "@ai-editor/editor-client" {
  export type TaskStatus =
    | "QUEUED"
    | "CONTEXT_READY"
    | "PLANNED"
    | "PATCHED"
    | "VALIDATING"
    | "REPAIRING"
    | "READY_FOR_REVIEW"
    | "PROMOTING"
    | "SUCCEEDED"
    | "FAILED"
    | "ABORTED";

  export interface Diagnostic {
    source: string;
    message: string;
    level: "error" | "warning";
    file?: string;
    line?: number;
    column?: number;
  }

  export interface TaskSubmission {
    goal: string;
    workspacePath: string;
    mode: "inline" | "file_edit" | "project_edit" | "autonomous";
  }

  export interface TaskView {
    taskId: string;
    goal: string;
    status: TaskStatus;
    modifiedFiles: string[];
    diagnostics: Diagnostic[];
  }

  export interface TaskResult {
    taskId: string;
    status: TaskStatus;
    plan?: unknown;
    patch?: unknown;
    modifiedFiles: string[];
    diagnostics: Diagnostic[];
    promotedAt?: string | null;
    shadowWorkspacePath?: string | null;
  }

  export interface BackendTaskClient {
    submitTask(input: TaskSubmission): Promise<{ taskId: string }>;
    getTask(taskId: string): Promise<TaskView>;
    getTaskResult(taskId: string): Promise<TaskResult>;
    cancelTask(taskId: string): Promise<{ taskId: string; status: TaskStatus }>;
    acceptPatch(taskId: string): Promise<TaskResult>;
    rejectPatch(taskId: string, reason: string): Promise<TaskResult>;
  }

  export class HttpBackendClient implements BackendTaskClient {
    constructor(options: { baseUrl: string; fetchFn?: (...args: unknown[]) => unknown });
    submitTask(input: TaskSubmission): Promise<{ taskId: string }>;
    getTask(taskId: string): Promise<TaskView>;
    getTaskResult(taskId: string): Promise<TaskResult>;
    cancelTask(taskId: string): Promise<{ taskId: string; status: TaskStatus }>;
    acceptPatch(taskId: string): Promise<TaskResult>;
    rejectPatch(taskId: string, reason: string): Promise<TaskResult>;
  }
}
