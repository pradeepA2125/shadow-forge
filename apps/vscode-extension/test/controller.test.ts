import type {
  BackendTaskClient,
  ChatMessage,
  ChatThreadSummary,
  Diagnostic,
  TaskResult,
  TaskSubmission,
  TaskView,
} from "@ai-editor/editor-client";
import { describe, expect, test } from "vitest";

import {
  AiEditorController,
  type ControllerUI,
  type SettingsProvider,
} from "../src/controller.js";
import type { SessionStore } from "../src/session-store.js";
import type { ReviewFileEntry, ReviewPanelViewModel, TaskSessionState } from "../src/types.js";

class MemorySessionStore implements SessionStore {
  value: TaskSessionState | null = null;

  async load(): Promise<TaskSessionState | null> {
    return this.value;
  }

  async save(session: TaskSessionState): Promise<void> {
    this.value = session;
  }

  async clear(): Promise<void> {
    this.value = null;
  }
}

interface StubBackendState {
  submitPayloads: TaskSubmission[];
  getTaskCalls: string[];
  acceptCalls: string[];
  rejectCalls: Array<{ taskId: string; reason: string }>;
  getResultCalls: string[];
  planFeedbackCalls: Array<{ taskId: string; feedback: string | null }>;
}

function createStubBackend(state: StubBackendState): BackendTaskClient {
  return {
    submitTask: async (input) => {
      state.submitPayloads.push(input);
      return { taskId: "task-123" };
    },
    getTask: async (taskId) => {
      state.getTaskCalls.push(taskId);
      return {
        taskId,
        goal: "goal",
        status: "READY_FOR_REVIEW",
        modifiedFiles: ["src/main.py"],
        diagnostics: [] as Diagnostic[],
      };
    },
    getTaskResult: async (taskId) => {
      state.getResultCalls.push(taskId);
      return {
        taskId,
        status: "READY_FOR_REVIEW",
        plan: {
          analysis: "a",
          steps: [
            {
              id: "1",
              goal: "g",
              targets: [{ path: "src/main.py", intent: "existing" }],
              risk: "low"
            }
          ],
          expected_files: ["src/main.py"],
          stop_conditions: ["done"],
        },
        patch: {
          patch_ops: [
            {
              op: "create_file",
              file: "src/main.py",
              content: "print('x')\n",
              reason: "seed",
            },
          ],
        },
        modifiedFiles: ["src/main.py"],
        diagnostics: [] as Diagnostic[],
        shadowWorkspacePath: "/tmp/shadow",
      } as TaskResult;
    },
    cancelTask: async (taskId) => ({ taskId, status: "ABORTED" }),
    acceptPatch: async (taskId) => {
      state.acceptCalls.push(taskId);
      return {
        taskId,
        status: "SUCCEEDED",
        modifiedFiles: ["src/main.py"],
        diagnostics: [] as Diagnostic[],
        shadowWorkspacePath: null,
      } as TaskResult;
    },
    rejectPatch: async (taskId, reason) => {
      state.rejectCalls.push({ taskId, reason });
      return {
        taskId,
        status: "ABORTED",
        modifiedFiles: ["src/main.py"],
        diagnostics: [] as Diagnostic[],
        shadowWorkspacePath: null,
      } as TaskResult;
    },
    providePlanFeedback: async (taskId, feedback) => {
      state.planFeedbackCalls.push({ taskId, feedback });
      return {
        taskId,
        goal: "goal",
        status: "AWAITING_PLAN_APPROVAL",
        modifiedFiles: [],
        diagnostics: [] as Diagnostic[],
        planMarkdown: feedback ? "# Revised Plan" : "# Approved Plan",
      };
    },
    resumeTask: async (_taskId) => ({ taskId: "task-child", resumeOfTaskId: _taskId }),
    sendScopeDecision: async (taskId, _decision) => ({ taskId, status: "EXECUTING" }),
    streamPatch: async (_taskId, _onEvent, _signal) => {},
    streamPatchEvents: async function* (_taskId: string) {
      yield { type: "done" as const };
    },
    listChatThreads: async () => [],
    createChatThread: async (workspacePath: string, title?: string) => ({
      threadId: "chat-stub",
      workspacePath,
      title: title ?? "New Chat",
      createdAt: "2026-01-01T00:00:00Z",
    }),
    getChatThread: async (threadId: string) => ({
      threadId,
      workspacePath: "/tmp/workspace",
      title: "New Chat",
      messages: [],
      touchedFiles: [],
    }),
    sendChatMessage: async function* (_threadId: string, _message: string) {
      yield { type: "chat_done", payload: {} };
    },
  };
}

function createUi(overrides?: Partial<ControllerUI>): ControllerUI {
  return {
    getWorkspacePath: () => "/tmp/workspace",
    promptForGoal: async () => "Ship the feature",
    promptForTaskId: async () => undefined,
    promptForRejectReason: async () => "Needs changes",
    promptForResumeStage: async () => undefined,
    promptForMaxIterationsOverride: async () => undefined,
    promptForScopeDecision: async () => undefined,
    showInfo: () => {},
    showWarning: () => {},
    showError: () => {},
    updatePanel: (_model: ReviewPanelViewModel) => {},
    openChatPanel: () => {},
    appendChatMessage: (_msg: ChatMessage) => {},
    appendChatChunk: (_chunk: string) => {},
    showChatThinking: (_message: string) => {},
    updateChatThinking: (_message: string) => {},
    hideChatThinking: () => {},
    setChatInputEnabled: (_enabled: boolean) => {},
    renderChatThreadList: (_threads: ChatThreadSummary[], _activeThreadId: string) => {},
    clearChatThread: () => {},
    ...overrides,
  };
}

function createSettings(): SettingsProvider {
  return {
    getBackendBaseUrl: () => "http://127.0.0.1:8000",
    getDefaultMode: () => "project_edit",
    getPollIntervalMs: () => 10_000,
  };
}

describe("AiEditorController", () => {
  test("startTask submits expected workspace path and mode", async () => {
    const state: StubBackendState = {
      submitPayloads: [],
      getTaskCalls: [],
      acceptCalls: [],
      rejectCalls: [],
      getResultCalls: [],
      planFeedbackCalls: [],
    };
    const backend = createStubBackend(state);
    const store = new MemorySessionStore();

    const controller = new AiEditorController(
      () => backend,
      store,
      createSettings(),
      createUi(),
      { openDiff: async (_entry: ReviewFileEntry) => {} },
      () => "2026-03-03T00:00:00.000Z"
    );

    await controller.startTask();
    controller.dispose();

    expect(state.submitPayloads).toEqual([
      {
        goal: "Ship the feature",
        workspacePath: "/tmp/workspace",
        mode: "project_edit",
      },
    ]);
    expect(store.value?.taskId).toBe("task-123");
  });

  test("accept/reject call backend endpoints and refresh task state", async () => {
    const state: StubBackendState = {
      submitPayloads: [],
      getTaskCalls: [],
      acceptCalls: [],
      rejectCalls: [],
      getResultCalls: [],
      planFeedbackCalls: [],
    };
    const backend = createStubBackend(state);
    const store = new MemorySessionStore();
    store.value = {
      taskId: "task-xyz",
      status: "READY_FOR_REVIEW",
      workspacePath: "/tmp/workspace",
      backendBaseUrl: "http://127.0.0.1:8000",
      updatedAt: "2026-03-03T00:00:00.000Z",
    };

    const controller = new AiEditorController(
      () => backend,
      store,
      createSettings(),
      createUi(),
      { openDiff: async (_entry: ReviewFileEntry) => {} },
      () => "2026-03-03T00:00:00.000Z"
    );

    await controller.initialize();
    await controller.acceptPatch();
    await controller.rejectPatch();
    controller.dispose();

    expect(state.acceptCalls).toEqual(["task-xyz"]);
    expect(state.rejectCalls).toEqual([{ taskId: "task-xyz", reason: "Needs changes" }]);
    expect(state.getTaskCalls.length).toBeGreaterThanOrEqual(3);
  });

  test("accept handles 409 conflict by refreshing task state", async () => {
    const state: StubBackendState = {
      submitPayloads: [],
      getTaskCalls: [],
      acceptCalls: [],
      rejectCalls: [],
      getResultCalls: [],
      planFeedbackCalls: [],
    };

    const warnings: string[] = [];
    const store = new MemorySessionStore();
    store.value = {
      taskId: "task-409",
      status: "READY_FOR_REVIEW",
      workspacePath: "/tmp/workspace",
      backendBaseUrl: "http://127.0.0.1:8000",
      updatedAt: "2026-03-03T00:00:00.000Z",
    };

    const backend: BackendTaskClient = {
      ...createStubBackend(state),
      acceptPatch: async () => {
        throw new Error("Backend request failed (409 Conflict) for /v1/tasks/task-409/accept");
      },
    };

    const controller = new AiEditorController(
      () => backend,
      store,
      createSettings(),
      createUi({
        showWarning: (message) => warnings.push(message),
      }),
      { openDiff: async (_entry: ReviewFileEntry) => {} },
      () => "2026-03-03T00:00:00.000Z"
    );

    await controller.initialize();
    await controller.acceptPatch();
    controller.dispose();

    expect(warnings.some((message) => message.includes("Refreshing state"))).toBe(true);
    expect(state.getTaskCalls.length).toBeGreaterThanOrEqual(2);
  });

  test("providePlanFeedback sends null on approval and text on regeneration", async () => {
    const state: StubBackendState = {
      submitPayloads: [],
      getTaskCalls: [],
      acceptCalls: [],
      rejectCalls: [],
      getResultCalls: [],
      planFeedbackCalls: [],
    };
    const infos: string[] = [];
    const backend = createStubBackend(state);
    const store = new MemorySessionStore();
    store.value = {
      taskId: "task-plan",
      status: "AWAITING_PLAN_APPROVAL",
      workspacePath: "/tmp/workspace",
      backendBaseUrl: "http://127.0.0.1:8000",
      updatedAt: "2026-03-03T00:00:00.000Z",
    };

    const controller = new AiEditorController(
      () => backend,
      store,
      createSettings(),
      createUi({
        showInfo: (message) => infos.push(message),
      }),
      { openDiff: async (_entry: ReviewFileEntry) => {} },
      () => "2026-03-03T00:00:00.000Z"
    );

    await controller.initialize();
    await controller.providePlanFeedback("   ");
    await controller.providePlanFeedback("Please scope this to the API layer");
    controller.dispose();

    expect(state.planFeedbackCalls).toEqual([
      { taskId: "task-plan", feedback: null },
      { taskId: "task-plan", feedback: "Please scope this to the API layer" },
    ]);
    expect(infos.some((message) => message.includes("Plan approved"))).toBe(true);
    expect(infos.some((message) => message.includes("Submitted plan feedback"))).toBe(true);
  });

});

describe("AiEditorController — chat", () => {
  test("sendChatMessage appends user message and streams agent response", async () => {
    const appendedMessages: Array<{ role: string; content: string }> = [];
    const chunks: string[] = [];

    const chatBackend: BackendTaskClient = {
      ...createStubBackend({
        submitPayloads: [], getTaskCalls: [], acceptCalls: [],
        rejectCalls: [], getResultCalls: [], planFeedbackCalls: [],
      }),
      createChatThread: async (workspacePath) => ({
        threadId: "chat-new",
        workspacePath,
        title: "New Chat",
        createdAt: "2026-05-11T00:00:00Z",
      }),
      listChatThreads: async () => [],
      getChatThread: async (threadId) => ({
        threadId,
        workspacePath: "/tmp/workspace",
        title: "New Chat",
        messages: [],
        touchedFiles: [],
      }),
      sendChatMessage: async function* () {
        yield { type: "chat_agent_thinking", payload: { message: "Exploring…" } };
        yield { type: "intent_classified", payload: { intent: "qa" } };
        yield { type: "chat_response", payload: { chunk: "The answer is 42." } };
        yield { type: "chat_done", payload: {} };
      },
    };

    const store = new MemorySessionStore();
    const controller = new AiEditorController(
      () => chatBackend,
      store,
      createSettings(),
      createUi({
        appendChatMessage: (m) => appendedMessages.push({ role: m.role, content: m.content }),
        appendChatChunk: (c) => chunks.push(c),
      }),
      { openDiff: async (_entry: ReviewFileEntry) => {} },
      () => "2026-05-11T00:00:00.000Z"
    );

    await controller.sendChatMessage("What is the answer?");
    controller.dispose();

    expect(appendedMessages[0].role).toBe("user");
    expect(appendedMessages[0].content).toBe("What is the answer?");
    expect(chunks).toContain("The answer is 42.");
  });

  test("thinking indicator: show on chat_agent_thinking, update on explore_tool_call, hide in finally", async () => {
    const thinkingMessages: string[] = [];

    const chatBackend: BackendTaskClient = {
      ...createStubBackend({
        submitPayloads: [], getTaskCalls: [], acceptCalls: [],
        rejectCalls: [], getResultCalls: [], planFeedbackCalls: [],
      }),
      createChatThread: async (workspacePath) => ({
        threadId: "chat-th",
        workspacePath,
        title: "New Chat",
        createdAt: "2026-05-11T00:00:00Z",
      }),
      listChatThreads: async () => [],
      getChatThread: async (threadId) => ({
        threadId, workspacePath: "/tmp/workspace",
        title: "New Chat", messages: [], touchedFiles: [],
      }),
      sendChatMessage: async function* () {
        yield { type: "chat_agent_thinking", payload: { message: "Exploring workspace…" } };
        yield { type: "explore_tool_call", payload: { tool: "search_code", args: { pattern: "auth" } } };
        yield { type: "intent_classified", payload: { intent: "qa" } };
        yield { type: "chat_response", payload: { chunk: "It handles auth." } };
        yield { type: "chat_done", payload: {} };
      },
    };

    const store = new MemorySessionStore();
    const controller = new AiEditorController(
      () => chatBackend,
      store,
      createSettings(),
      createUi({
        showChatThinking: (m) => thinkingMessages.push(`show:${m}`),
        updateChatThinking: (m) => thinkingMessages.push(`update:${m}`),
        hideChatThinking: () => thinkingMessages.push("hide"),
      }),
      { openDiff: async (_entry: ReviewFileEntry) => {} },
      () => "2026-05-11T00:00:00.000Z"
    );

    await controller.sendChatMessage("What does auth do?");
    controller.dispose();

    expect(thinkingMessages[0]).toBe("show:Exploring workspace…");
    expect(thinkingMessages[1]).toBe("update:search_code: auth");
    expect(thinkingMessages).toContain("hide");
  });
});
