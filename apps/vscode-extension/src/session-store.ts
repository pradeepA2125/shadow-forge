import type { TaskSessionState } from "./types.js";

export const SESSION_KEYS = {
  activeTaskId: "aiEditor.activeTaskId",
  backendBaseUrl: "aiEditor.backendBaseUrl",
  lastKnownStatus: "aiEditor.lastKnownStatus",
  workspacePath: "aiEditor.workspacePath",
  updatedAt: "aiEditor.updatedAt",
} as const;

export interface SessionStore {
  load(): Promise<TaskSessionState | null>;
  save(session: TaskSessionState): Promise<void>;
  clear(): Promise<void>;
}
