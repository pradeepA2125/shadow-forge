import type * as vscode from "vscode";

import { SESSION_KEYS, SessionStore } from "./session-store.js";
import type { TaskSessionState } from "./types.js";

export class VscodeSessionStore implements SessionStore {
  constructor(private readonly state: vscode.Memento) {}

  async load(): Promise<TaskSessionState | null> {
    const taskId = this.state.get<string>(SESSION_KEYS.activeTaskId);
    const backendBaseUrl = this.state.get<string>(SESSION_KEYS.backendBaseUrl);
    const status = this.state.get<TaskSessionState["status"]>(SESSION_KEYS.lastKnownStatus);
    const workspacePath = this.state.get<string>(SESSION_KEYS.workspacePath);
    const updatedAt = this.state.get<string>(SESSION_KEYS.updatedAt);

    if (!taskId || !backendBaseUrl || !status || !workspacePath || !updatedAt) {
      return null;
    }

    return {
      taskId,
      backendBaseUrl,
      status,
      workspacePath,
      updatedAt,
    };
  }

  async save(session: TaskSessionState): Promise<void> {
    await this.state.update(SESSION_KEYS.activeTaskId, session.taskId);
    await this.state.update(SESSION_KEYS.backendBaseUrl, session.backendBaseUrl);
    await this.state.update(SESSION_KEYS.lastKnownStatus, session.status);
    await this.state.update(SESSION_KEYS.workspacePath, session.workspacePath);
    await this.state.update(SESSION_KEYS.updatedAt, session.updatedAt);
  }

  async clear(): Promise<void> {
    await this.state.update(SESSION_KEYS.activeTaskId, undefined);
    await this.state.update(SESSION_KEYS.backendBaseUrl, undefined);
    await this.state.update(SESSION_KEYS.lastKnownStatus, undefined);
    await this.state.update(SESSION_KEYS.workspacePath, undefined);
    await this.state.update(SESSION_KEYS.updatedAt, undefined);
  }
}
