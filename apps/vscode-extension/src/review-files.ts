import fs from "node:fs";
import path from "node:path";

import type { ReviewFileEntry } from "./types.js";

export function buildReviewFileEntries(
  workspacePath: string,
  shadowWorkspacePath: string,
  modifiedFiles: string[]
): ReviewFileEntry[] {
  const workspaceRoot = path.resolve(workspacePath);
  const shadowRoot = path.resolve(shadowWorkspacePath);

  return modifiedFiles.map((relativePath) => {
    const normalizedRelative = normalizeRelativePath(relativePath);
    const realPath = path.resolve(workspaceRoot, normalizedRelative);
    const shadowPath = path.resolve(shadowRoot, normalizedRelative);

    return {
      relativePath: normalizedRelative,
      realPath,
      shadowPath,
      existsReal: fs.existsSync(realPath),
      existsShadow: fs.existsSync(shadowPath),
    };
  });
}

function normalizeRelativePath(relativePath: string): string {
  return relativePath.replaceAll("\\", "/").replace(/^\.\//, "");
}
