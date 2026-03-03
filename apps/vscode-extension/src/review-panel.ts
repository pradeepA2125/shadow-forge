import * as vscode from "vscode";

import type { ReviewPanelViewModel } from "./types.js";

export interface ReviewPanelHandlers {
  onOpenDiff: (relativePath: string) => void;
  onRefresh: () => void;
  onAccept: () => void;
  onReject: () => void;
}

interface PanelMessage {
  type?: string;
  relativePath?: string;
}

export class ReviewPanel {
  private panel: vscode.WebviewPanel | null = null;
  private lastModel: ReviewPanelViewModel = {
    session: null,
    task: null,
    result: null,
    reviewFiles: [],
  };

  constructor(private readonly handlers: ReviewPanelHandlers) {}

  show(): void {
    const panel = this.ensurePanel();
    panel.reveal(vscode.ViewColumn.One);
  }

  update(model: ReviewPanelViewModel): void {
    this.lastModel = model;
    const panel = this.ensurePanel();
    panel.webview.html = renderPanelHtml(model);
  }

  dispose(): void {
    this.panel?.dispose();
    this.panel = null;
  }

  private ensurePanel(): vscode.WebviewPanel {
    if (this.panel) {
      return this.panel;
    }

    const panel = vscode.window.createWebviewPanel(
      "aiEditorReview",
      "AI Editor Review",
      vscode.ViewColumn.One,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
      }
    );

    panel.onDidDispose(() => {
      this.panel = null;
    });

    panel.webview.onDidReceiveMessage((rawMessage: unknown) => {
      const message = rawMessage as PanelMessage;
      if (message.type === "openDiff" && message.relativePath) {
        this.handlers.onOpenDiff(message.relativePath);
        return;
      }
      if (message.type === "refresh") {
        this.handlers.onRefresh();
        return;
      }
      if (message.type === "accept") {
        this.handlers.onAccept();
        return;
      }
      if (message.type === "reject") {
        this.handlers.onReject();
      }
    });

    this.panel = panel;
    panel.webview.html = renderPanelHtml(this.lastModel);
    return panel;
  }
}

function renderPanelHtml(model: ReviewPanelViewModel): string {
  const status = model.task?.status ?? model.session?.status ?? "No active task";
  const taskId = model.session?.taskId ?? "None";
  const workspacePath = model.session?.workspacePath ?? "N/A";
  const canReview = model.task?.status === "READY_FOR_REVIEW";
  const diagnostics = model.task?.diagnostics ?? [];

  const fileRows = model.reviewFiles
    .map((entry) => {
      const flags = [
        entry.existsReal ? "real" : "real missing",
        entry.existsShadow ? "shadow" : "shadow missing",
      ].join(" | ");

      return `<li><code>${escapeHtml(entry.relativePath)}</code> <button data-open-diff="${escapeHtml(
        entry.relativePath
      )}">Open Diff</button> <span>${escapeHtml(flags)}</span></li>`;
    })
    .join("\n");

  const diagnosticRows = diagnostics
    .map((item) => {
      const location = [item.file, item.line, item.column].filter(Boolean).join(":");
      const head = location ? `${location} - ` : "";
      return `<li><strong>${escapeHtml(item.level)}</strong>: ${escapeHtml(head + item.message)}</li>`;
    })
    .join("\n");

  const planJson = model.result?.plan ? JSON.stringify(model.result.plan, null, 2) : "null";
  const patchJson = model.result?.patch ? JSON.stringify(model.result.patch, null, 2) : "null";

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <style>
    body { font-family: sans-serif; padding: 16px; }
    .toolbar { display: flex; gap: 8px; margin-bottom: 12px; }
    .meta { margin: 8px 0; }
    ul { padding-left: 20px; }
    code { background: #f4f4f4; padding: 1px 4px; border-radius: 4px; }
    pre { white-space: pre-wrap; word-break: break-word; }
  </style>
</head>
<body>
  <h2>AI Editor Review</h2>
  <div class="toolbar">
    <button data-action="refresh">Refresh</button>
    <button data-action="accept" ${canReview ? "" : "disabled"}>Accept Patch</button>
    <button data-action="reject" ${canReview ? "" : "disabled"}>Reject Patch</button>
  </div>

  <div class="meta"><strong>Status:</strong> ${escapeHtml(status)}</div>
  <div class="meta"><strong>Task:</strong> ${escapeHtml(taskId)}</div>
  <div class="meta"><strong>Workspace:</strong> ${escapeHtml(workspacePath)}</div>

  <h3>Modified Files</h3>
  <ul>${fileRows || "<li>No modified files yet.</li>"}</ul>

  <h3>Diagnostics</h3>
  <ul>${diagnosticRows || "<li>No diagnostics.</li>"}</ul>

  <details>
    <summary>Plan JSON</summary>
    <pre>${escapeHtml(planJson)}</pre>
  </details>

  <details>
    <summary>Patch JSON</summary>
    <pre>${escapeHtml(patchJson)}</pre>
  </details>

  <script>
    const vscode = acquireVsCodeApi();
    document.querySelector('[data-action="refresh"]').addEventListener('click', () => {
      vscode.postMessage({ type: 'refresh' });
    });
    document.querySelector('[data-action="accept"]').addEventListener('click', () => {
      vscode.postMessage({ type: 'accept' });
    });
    document.querySelector('[data-action="reject"]').addEventListener('click', () => {
      vscode.postMessage({ type: 'reject' });
    });
    for (const button of document.querySelectorAll('[data-open-diff]')) {
      button.addEventListener('click', () => {
        vscode.postMessage({ type: 'openDiff', relativePath: button.dataset.openDiff });
      });
    }
  </script>
</body>
</html>`;
}

function escapeHtml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}
