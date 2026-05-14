import { HttpBackendClient } from "@ai-editor/editor-client";
import * as vscode from "vscode";

import { ChatPanel } from "./chat-panel.js";
import {
  AiEditorController,
  type BackendClientFactory,
  type ControllerUI,
} from "./controller.js";
import { openReviewDiff } from "./review-diff.js";
import { ReviewPanel } from "./review-panel.js";
import { VscodeSessionStore } from "./vscode-session-store.js";
import { checkBackendHealth, VscodeSettingsProvider } from "./settings.js";

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  const settings = new VscodeSettingsProvider();
  const sessionStore = new VscodeSessionStore(context.workspaceState);

  let controller: AiEditorController;

  const chatPanel = new ChatPanel(
    (message) => controller.sendChatMessage(message),
    (taskId, action, feedback) => controller.handlePlanCardAction(taskId, action, feedback),
    () => controller.newChatThread(),
    (threadId) => controller.switchChatThread(threadId)
  );

  const panel = new ReviewPanel({
    onOpenDiff: (relativePath) => {
      void controller.openDiffForFile(relativePath);
    },
    onRefresh: () => {
      void controller.refreshTask();
    },
    onAccept: () => {
      void controller.acceptPatch();
    },
    onReject: () => {
      void controller.rejectPatch();
    },
    onProvidePlanFeedback: (feedback) => {
      void controller.providePlanFeedback(feedback);
    },
  });

  const ui: ControllerUI = {
    getWorkspacePath: () => vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? null,
    promptForGoal: () =>
      vscode.window.showInputBox({
        prompt: "Describe what you want AI Editor to do",
        placeHolder: "Example: Refactor auth middleware to support refresh tokens",
        ignoreFocusOut: true,
      }),
    promptForRejectReason: () =>
      vscode.window.showInputBox({
        prompt: "Why are you rejecting this patch?",
        value: "Needs revision",
        ignoreFocusOut: true,
      }),
    showInfo: (message) => {
      void vscode.window.showInformationMessage(message);
    },
    showWarning: (message) => {
      void vscode.window.showWarningMessage(message);
    },
    showError: (message) => {
      void vscode.window.showErrorMessage(message);
    },
    updatePanel: (model) => {
      panel.update(model);
    },
    promptForResumeStage: () =>
      vscode.window.showQuickPick(
        ["plan", "feedback", "execute"] as const,
        { placeHolder: "Select stage to resume from" },
      ) as Promise<"plan" | "feedback" | "execute" | undefined>,
    promptForMaxIterationsOverride: async () => {
      const value = await vscode.window.showInputBox({
        prompt: "Override max iterations? (leave blank to keep current)",
        placeHolder: "e.g. 10",
        validateInput: (v) =>
          v === "" || /^\d+$/.test(v) ? null : "Enter a positive integer or leave blank",
      });
      return value === "" || value === undefined ? undefined : parseInt(value, 10);
    },
    promptForTaskId: () =>
      vscode.window.showInputBox({
        prompt: "Enter the task ID to attach to",
        placeHolder: "task-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        ignoreFocusOut: true,
      }),
    promptForScopeDecision: async ({ files, reason, stepId }) => {
      const fileList = files.length === 1 ? files[0] : `${files.length} files (${files.join(", ")})`;
      const choice = await vscode.window.showInformationMessage(
        `[Step ${stepId}] Agent wants to also modify ${fileList}.\n\nReason: ${reason}`,
        { modal: true },
        "Approve",
        "Approve & Remember",
        "Reject"
      );
      if (!choice) return undefined;
      return {
        decision: choice.startsWith("Approve") ? "approve" : "reject",
        remember: choice === "Approve & Remember",
      };
    },
    openChatPanel: () => {
      chatPanel.show();
    },
    appendChatMessage: (message) => {
      chatPanel.appendMessage(message);
    },
    appendChatChunk: (chunk) => {
      chatPanel.appendChunk(chunk);
    },
    showChatThinking: (message) => {
      chatPanel.showThinking(message);
    },
    updateChatThinking: (message) => {
      chatPanel.updateThinking(message);
    },
    hideChatThinking: () => {
      chatPanel.hideThinking();
    },
    setChatInputEnabled: (enabled) => {
      chatPanel.setInputEnabled(enabled);
    },
    renderChatThreadList: (threads, activeThreadId) => {
      chatPanel.renderThreadList(threads, activeThreadId);
    },
    clearChatThread: () => {
      chatPanel.clearThread();
    },
  };

  const clientFactory: BackendClientFactory = (baseUrl) => new HttpBackendClient({ baseUrl });

  controller = new AiEditorController(clientFactory, sessionStore, settings, ui, {
    openDiff: openReviewDiff,
  });

  context.subscriptions.push(
    vscode.commands.registerCommand("aiEditor.startTask", async () => {
      await controller.startTask();
      panel.show();
    })
  );
  context.subscriptions.push(
    vscode.commands.registerCommand("aiEditor.openReviewPanel", () => {
      panel.show();
      controller.openReviewPanel();
    })
  );
  context.subscriptions.push(
    vscode.commands.registerCommand("aiEditor.acceptPatch", () => controller.acceptPatch())
  );
  context.subscriptions.push(
    vscode.commands.registerCommand("aiEditor.rejectPatch", () => controller.rejectPatch())
  );
  context.subscriptions.push(
    vscode.commands.registerCommand("aiEditor.refreshTask", () => controller.refreshTask())
  );
  context.subscriptions.push(
    vscode.commands.registerCommand("aiEditor.attachToTask", async () => {
      await controller.attachToTask();
      panel.show();
    })
  );
  context.subscriptions.push(
    vscode.commands.registerCommand("aiEditor.openChat", () => {
      void controller.openChat();
    })
  );
  context.subscriptions.push({
    dispose: () => {
      controller.dispose();
      panel.dispose();
    },
  });

  const backendBaseUrl = settings.getBackendBaseUrl();
  const healthy = await checkBackendHealth(backendBaseUrl);
  if (!healthy) {
    void vscode.window.showWarningMessage(
      `AI Editor backend is not reachable at ${backendBaseUrl}. Start agentd-py, then run \"AI Editor: Start Task\".`
    );
  }

  await controller.initialize();
}

export function deactivate(): void {
  // disposal is handled through extension subscriptions.
}
