import fs from "node:fs";
import path from "node:path";

import { describe, expect, test } from "vitest";

import { buildReviewFileEntries } from "../src/review-files.js";

describe("buildReviewFileEntries", () => {
  test("maps modified files to real/shadow paths with existence flags", () => {
    const root = fs.mkdtempSync(path.join(process.cwd(), "tmp-review-files-"));
    const workspace = path.join(root, "workspace");
    const shadow = path.join(root, "shadow");

    fs.mkdirSync(path.join(workspace, "src"), { recursive: true });
    fs.mkdirSync(path.join(shadow, "src"), { recursive: true });

    fs.writeFileSync(path.join(workspace, "src", "a.ts"), "const a = 1;\n", "utf-8");
    fs.writeFileSync(path.join(shadow, "src", "b.ts"), "const b = 1;\n", "utf-8");

    const entries = buildReviewFileEntries(workspace, shadow, ["src/a.ts", "src/b.ts"]);

    expect(entries).toEqual([
      {
        relativePath: "src/a.ts",
        realPath: path.join(workspace, "src", "a.ts"),
        shadowPath: path.join(shadow, "src", "a.ts"),
        existsReal: true,
        existsShadow: false,
      },
      {
        relativePath: "src/b.ts",
        realPath: path.join(workspace, "src", "b.ts"),
        shadowPath: path.join(shadow, "src", "b.ts"),
        existsReal: false,
        existsShadow: true,
      },
    ]);
  });
});
