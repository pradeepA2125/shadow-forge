import { describe, expect, test } from "vitest";
import { HttpBackendClient } from "../src/client/http-backend-client.js";

describe("HttpBackendClient", () => {
  test("maps snake_case backend payload to camelCase task view", async () => {
    const client = new HttpBackendClient({
      baseUrl: "http://localhost:8000",
      fetchFn: async () =>
        new Response(
          JSON.stringify({
            task_id: "task-123",
            goal: "goal",
            status: "QUEUED",
            modified_files: ["a.ts"],
            diagnostics: []
          }),
          { status: 200, headers: { "content-type": "application/json" } }
        )
    });

    const result = await client.getTask("task-123");
    expect(result.taskId).toBe("task-123");
    expect(result.modifiedFiles).toEqual(["a.ts"]);
  });

  test("accepts diagnostics with null file/line/column fields from backend", async () => {
    const client = new HttpBackendClient({
      baseUrl: "http://localhost:8000",
      fetchFn: async () =>
        new Response(
          JSON.stringify({
            task_id: "task-124",
            goal: "goal",
            status: "VALIDATING",
            modified_files: ["main.py"],
            diagnostics: [
              {
                source: "validator:python-compileall",
                message: "failed",
                level: "error",
                file: null,
                line: null,
                column: null
              }
            ]
          }),
          { status: 200, headers: { "content-type": "application/json" } }
        )
    });

    const result = await client.getTask("task-124");
    expect(result.taskId).toBe("task-124");
    expect(result.diagnostics).toEqual([
      {
        source: "validator:python-compileall",
        message: "failed",
        level: "error"
      }
    ]);
  });

  test("maps snake_case backend payload to camelCase task result", async () => {
    const client = new HttpBackendClient({
      baseUrl: "http://localhost:8000",
      fetchFn: async () =>
        new Response(
          JSON.stringify({
            task_id: "task-123",
            status: "READY_FOR_REVIEW",
            plan: {
              analysis: "a",
              steps: [{ id: "S1", goal: "g", targets: ["a.ts"], risk: "low" }],
              expected_files: ["a.ts"],
              stop_conditions: ["done"]
            },
            patch: {
              patch_ops: [
                {
                  op: "create_file",
                  file: "a.ts",
                  content: "x",
                  reason: "init"
                }
              ]
            },
            modified_files: ["a.ts"],
            diagnostics: [],
            promoted_at: null,
            shadow_workspace_path: "/tmp/shadow/task-123"
          }),
          { status: 200, headers: { "content-type": "application/json" } }
        )
    });

    const result = await client.getTaskResult("task-123");
    expect(result.taskId).toBe("task-123");
    expect(result.modifiedFiles).toEqual(["a.ts"]);
    expect(result.shadowWorkspacePath).toBe("/tmp/shadow/task-123");
  });

  test("sends workspace_path to backend when creating task", async () => {
    let body = "";

    const client = new HttpBackendClient({
      baseUrl: "http://localhost:8000",
      fetchFn: async (_url, init) => {
        body = String(init?.body ?? "");
        return new Response(JSON.stringify({ task_id: "task-999" }), {
          status: 200,
          headers: { "content-type": "application/json" }
        });
      }
    });

    await client.submitTask({
      goal: "goal",
      workspacePath: "/tmp/repo",
      mode: "project_edit"
    });

    expect(JSON.parse(body)).toEqual({
      goal: "goal",
      workspace_path: "/tmp/repo",
      mode: "project_edit"
    });
  });
});
