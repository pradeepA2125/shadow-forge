import type {
  BackendTaskClient,
  ChatMessage,
  ChatThreadSummary,
  CommandDecision,
  Diagnostic,
  TaskResult,
  TaskSubmission,
  TaskView,
  ThreadLiveState,
} from "@ai-editor/editor-client";
import { describe, expect, test } from "vitest";

import {
  AiEditorController,
  type ControllerUI,
  type LiveGateView,
  type LivePlanView,
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
  liveResponse?: ThreadLiveState;
  liveCalls?: string[];
}

const NULL_LIVE_STATE: ThreadLiveState = {
  activeTaskId: null,
  status: null,
  pendingGate: null,
  plan: null,
};

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
    sendValidationDecision: async (taskId, _decision) => ({ taskId, status: "AWAITING_VALIDATION_DECISION" as const }),
    sendCommandDecision: async (taskId, _decision) => ({ taskId, status: "EXECUTING" as const }),
    streamPatch: async (_taskId, _onEvent, _signal) => {},
    streamPatchEvents: async function* (_taskId: string) {
      yield { type: "done" as const, payload: {} as Record<string, never> };
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
    getThreadLiveState: async (threadId: string) => {
      state.liveCalls?.push(threadId);
      return state.liveResponse ?? NULL_LIVE_STATE;
    },
    sendChatMessage: async function* (_threadId: string, _message: string, _signal?: AbortSignal) {
      yield { type: "chat_done" as const, payload: {} as Record<string, never> };
    },
    applyInlineChange: async (_inlineTaskId: string) => {},
    discardInlineChange: async (_inlineTaskId: string) => {},
    sendStepDecision: async (_taskId: string, _decision: "accept" | "discard") => {},
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
    resolveInlineChangeCard: (_taskId: string, _resolution: "applied" | "discarded") => {},
    updateThreadTitle: (_threadId: string, _title: string) => {},
    appendChatThinkingEntry: (_text: string) => {},
    appendChatThinkingChunk: (_chunk: string) => {},
    finalizeAgentMessage: () => {},
    showStepReview: () => {},
    renderLiveGate: () => {},
    clearLiveGate: () => {},
    renderLivePlan: () => {},
    clearLivePlan: () => {},
    appendToolEvent: () => {},
    appendToolResult: () => {},
    updateWorkbar: () => {},
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
      sendChatMessage: async function* (_threadId: string, _message: string, _signal?: AbortSignal) {
        yield { type: "chat_agent_thinking" as const, payload: { message: "Exploring…" } };
        yield { type: "intent_classified" as const, payload: { intent: "qa", rationale: "", likely_targets: [] } };
        yield { type: "chat_response" as const, payload: { chunk: "The answer is 42." } };
        yield { type: "chat_done" as const, payload: {} as Record<string, never> };
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

  test("thinking entries: chat_agent_thinking appends entry; explore_tool_call forwards structured tool event, finally hides indicator", async () => {
    const thinkingEntries: string[] = [];
    const toolEvents: Array<{ id: number; tool: string; source: string }> = [];
    let hideCalled = false;

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
      sendChatMessage: async function* (_threadId: string, _message: string, _signal?: AbortSignal) {
        yield { type: "chat_agent_thinking" as const, payload: { message: "Exploring workspace…" } };
        yield { type: "explore_tool_call" as const, payload: { tool: "search_code", args: { pattern: "auth" }, thought: "Looking for auth handling code" } };
        yield { type: "intent_classified" as const, payload: { intent: "qa", rationale: "", likely_targets: [] } };
        yield { type: "chat_response" as const, payload: { chunk: "It handles auth." } };
        yield { type: "chat_done" as const, payload: {} as Record<string, never> };
      },
    };

    const store = new MemorySessionStore();
    const controller = new AiEditorController(
      () => chatBackend,
      store,
      createSettings(),
      createUi({
        appendChatThinkingEntry: (t) => thinkingEntries.push(t),
        appendToolEvent: (e) => toolEvents.push({ id: e.id, tool: e.tool, source: e.source }),
        hideChatThinking: () => { hideCalled = true; },
      }),
      { openDiff: async (_entry: ReviewFileEntry) => {} },
      () => "2026-05-11T00:00:00.000Z"
    );

    await controller.sendChatMessage("What does auth do?");
    controller.dispose();

    // chat_agent_thinking still appends a thinking entry
    expect(thinkingEntries[0]).toBe("Exploring workspace…");
    // explore_tool_call is now forwarded as a structured tool event, not a thinking entry
    expect(toolEvents).toHaveLength(1);
    expect(toolEvents[0].tool).toBe("search_code");
    expect(toolEvents[0].source).toBe("explore");
    expect(hideCalled).toBe(true);
  });
});

describe("AiEditorController — command-decision", () => {
  test("handleCommandDecisionFromChat posts the decision to the backend", async () => {
    const sent: Array<{ taskId: string; decision: CommandDecision }> = [];
    const backend: BackendTaskClient = {
      ...createStubBackend({
        submitPayloads: [], getTaskCalls: [], acceptCalls: [],
        rejectCalls: [], getResultCalls: [], planFeedbackCalls: [],
      }),
      sendCommandDecision: async (taskId, decision) => {
        sent.push({ taskId, decision });
        return { taskId, status: "EXECUTING" as const };
      },
    };
    const store = new MemorySessionStore();
    store.value = {
      taskId: "task-1",
      status: "AWAITING_COMMAND_DECISION",
      workspacePath: "/tmp/workspace",
      backendBaseUrl: "http://127.0.0.1:8000",
      updatedAt: "2026-05-28T00:00:00.000Z",
    };
    const controller = new AiEditorController(
      () => backend,
      store,
      createSettings(),
      createUi(),
      { openDiff: async (_entry: ReviewFileEntry) => {} },
      () => "2026-05-11T00:00:00.000Z"
    );
    await controller.initialize();

    await controller.handleCommandDecisionFromChat("task-1", {
      approve: true, remember: true, scope: "prefix", ruleValue: "python -c",
    });
    controller.dispose();

    expect(sent).toEqual([{
      taskId: "task-1",
      decision: { approve: true, remember: true, scope: "prefix", ruleValue: "python -c" },
    }]);
  });

  test("pollThreadLiveState renders one gate card, dedups, and removes on null", async () => {
    const state: StubBackendState = {
      submitPayloads: [], getTaskCalls: [], acceptCalls: [], rejectCalls: [],
      getResultCalls: [], planFeedbackCalls: [], liveCalls: [],
      liveResponse: NULL_LIVE_STATE,
    };
    const backend = createStubBackend(state);

    const gateRenders: LiveGateView[] = [];
    const planRenders: LivePlanView[] = [];
    let gateClears = 0;
    let planClears = 0;
    const ui = createUi({
      renderLiveGate: (gate) => { gateRenders.push(gate); },
      clearLiveGate: () => { gateClears += 1; },
      renderLivePlan: (plan) => { planRenders.push(plan); },
      clearLivePlan: () => { planClears += 1; },
    });

    const controller = new AiEditorController(
      () => backend, new MemorySessionStore(), createSettings(), ui,
      { openDiff: async (_entry: ReviewFileEntry) => {} },
      () => "2026-05-11T00:00:00.000Z"
    );

    // Establish an active thread, then stop the auto-poll timer so we drive ticks by hand.
    await controller.switchChatThread("chat-1");
    await Promise.resolve();
    controller.dispose();
    gateRenders.length = 0; planRenders.length = 0; gateClears = 0; planClears = 0;

    // Poll #1: a command gate appears → exactly one gate card rendered.
    state.liveResponse = {
      activeTaskId: "task-1",
      status: "AWAITING_COMMAND_DECISION",
      pendingGate: { kind: "command", payload: { command: "pytest" } },
      plan: null,
    };
    await controller.pollThreadLiveState();
    expect(gateRenders).toHaveLength(1);
    expect(gateRenders[0].kind).toBe("command");
    expect(gateRenders[0].taskId).toBe("task-1");
    expect(gateRenders[0].payload.command).toBe("pytest");

    // Poll #2: identical state → dedup, no second render (replace-not-append stays one card).
    await controller.pollThreadLiveState();
    expect(gateRenders).toHaveLength(1);

    // Poll #3: gate resolved (null) → card removed.
    state.liveResponse = NULL_LIVE_STATE;
    await controller.pollThreadLiveState();
    expect(gateClears).toBe(1);
  });

  test("pollThreadLiveState surfaces a plan card at AWAITING_PLAN_APPROVAL", async () => {
    const state: StubBackendState = {
      submitPayloads: [], getTaskCalls: [], acceptCalls: [], rejectCalls: [],
      getResultCalls: [], planFeedbackCalls: [], liveCalls: [],
      liveResponse: {
        activeTaskId: "task-9",
        status: "AWAITING_PLAN_APPROVAL",
        pendingGate: null,
        plan: { task_id: "task-9", plan_markdown: "# Plan\n- step" },
      },
    };
    const backend = createStubBackend(state);
    const planRenders: LivePlanView[] = [];
    const ui = createUi({ renderLivePlan: (plan) => { planRenders.push(plan); } });

    const controller = new AiEditorController(
      () => backend, new MemorySessionStore(), createSettings(), ui,
      { openDiff: async (_entry: ReviewFileEntry) => {} },
      () => "2026-05-11T00:00:00.000Z"
    );
    await controller.switchChatThread("chat-1");
    await controller.pollThreadLiveState();
    controller.dispose();

    expect(planRenders.length).toBeGreaterThanOrEqual(1);
    expect(planRenders[planRenders.length - 1].taskId).toBe("task-9");
    expect(planRenders[planRenders.length - 1].planMarkdown).toContain("# Plan");
  });
});
