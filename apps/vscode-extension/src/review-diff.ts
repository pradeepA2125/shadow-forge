import fs from "node:fs";
import path from "node:path";

import * as vscode from "vscode";

import type { ReviewFileEntry } from "./types.js";

const LANGUAGE_BY_EXT: Record<string, string> = {
  ".ts": "typescript",
  ".tsx": "typescriptreact",
  ".js": "javascript",
  ".jsx": "javascriptreact",
  ".py": "python",
  ".rs": "rust",
  ".json": "json",
  ".md": "markdown",
};

export async function openReviewDiff(entry: ReviewFileEntry): Promise<void> {
  const leftDoc = await openDocumentOrEmpty(entry.realPath, entry.relativePath);
  const rightDoc = await openDocumentOrEmpty(entry.shadowPath, entry.relativePath);

  const leftLabel = entry.existsReal ? "real" : "real missing";
  const rightLabel = entry.existsShadow ? "shadow" : "shadow missing";

  await vscode.commands.executeCommand(
    "vscode.diff",
    leftDoc.uri,
    rightDoc.uri,
    `${entry.relativePath} (${leftLabel} ↔ ${rightLabel})`
  );
}

async function openDocumentOrEmpty(filePath: string, relativePath: string): Promise<vscode.TextDocument> {
  if (fs.existsSync(filePath)) {
    return vscode.workspace.openTextDocument(vscode.Uri.file(filePath));
  }

  const extension = path.extname(relativePath).toLowerCase();
  const language = LANGUAGE_BY_EXT[extension] ?? "plaintext";

  return vscode.workspace.openTextDocument({
    language,
    content: "",
  });
}
