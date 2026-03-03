import type { TaskResult, TaskStatus, TaskView } from "@ai-editor/editor-client";

export type TaskMode = "inline" | "file_edit" | "project_edit" | "autonomous";

export interface TaskSessionState {
  taskId: string;
  status: TaskStatus;
  workspacePath: string;
  backendBaseUrl: string;
  updatedAt: string;
}

export interface ReviewFileEntry {
  relativePath: string;
  realPath: string;
  shadowPath: string;
  existsReal: boolean;
  existsShadow: boolean;
}

export interface ReviewPanelViewModel {
  session: TaskSessionState | null;
  task: TaskView | null;
  result: TaskResult | null;
  reviewFiles: ReviewFileEntry[];
}
