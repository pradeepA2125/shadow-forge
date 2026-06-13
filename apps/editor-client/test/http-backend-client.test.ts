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
            status: "AWAITING_PLAN_APPROVAL",
            modified_files: ["a.ts"],
            diagnostics: [],
            plan_markdown: "# Plan\n\n- Add route"
          }),
          { status: 200, headers: { "content-type": "application/json" } }
        )
    });

    const result = await client.getTask("task-123");
    expect(result.taskId).toBe("task-123");
    expect(result.modifiedFiles).toEqual(["a.ts"]);
    expect(result.planMarkdown).toBe("# Plan\n\n- Add route");
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
              steps: [
                {
                  id: "S1",
                  goal: "g",
                  targets: [{ path: "a.ts", intent: "existing" }],
                  risk: "low"
                }
              ],
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

  test("posts explicit null when approving a plan without feedback", async () => {
    let body = "";

    const client = new HttpBackendClient({
      baseUrl: "http://localhost:8000",
      fetchFn: async (_url, init) => {
        body = String(init?.body ?? "");
        return new Response(
          JSON.stringify({
            task_id: "task-123",
            goal: "goal",
            status: "AWAITING_PLAN_APPROVAL",
            modified_files: [],
            diagnostics: [],
            plan_markdown: "# Plan"
          }),
          { status: 200, headers: { "content-type": "application/json" } }
        );
      }
    });

    const result = await client.providePlanFeedback("task-123", null);

    expect(JSON.parse(body)).toEqual({ feedback: null });
    expect(result.planMarkdown).toBe("# Plan");
  });

  test("sendScopeDecision posts approve body and parses response", async () => {
    let url = "";
    let body = "";
    const client = new HttpBackendClient({
      baseUrl: "http://localhost:8000",
      fetchFn: async (input, init) => {
        url = String(input);
        body = String(init?.body ?? "");
        return new Response(
          JSON.stringify({ task_id: "task-1", status: "EXECUTING" }),
          { status: 200, headers: { "content-type": "application/json" } }
        );
      }
    });
    const result = await client.sendScopeDecision("task-1", {
      decision: "approve",
      files: ["tests/__init__.py"],
      remember: true
    });
    expect(url).toContain("/v1/tasks/task-1/scope-decision");
    expect(JSON.parse(body)).toEqual({
      decision: "approve",
      files: ["tests/__init__.py"],
      remember: true
    });
    expect(result.taskId).toBe("task-1");
    expect(result.status).toBe("EXECUTING");
  });

  test("sendScopeDecision throws on 409", async () => {
    const client = new HttpBackendClient({
      baseUrl: "http://localhost:8000",
      fetchFn: async () =>
        new Response(JSON.stringify({ detail: "not awaiting" }), { status: 409 })
    });
    await expect(
      client.sendScopeDecision("task-x", { decision: "approve", files: [], remember: false })
    ).rejects.toThrow();
  });

  test("sendValidationDecision posts decision and parses response", async () => {
    let url = "";
    let body = "";
    const client = new HttpBackendClient({
      baseUrl: "http://localhost:8000",
      fetchFn: async (input, init) => {
        url = String(input);
        body = String(init?.body ?? "");
        return new Response(
          JSON.stringify({ task_id: "task-1", status: "AWAITING_VALIDATION_DECISION" }),
          { status: 200, headers: { "content-type": "application/json" } }
        );
      }
    });
    const result = await client.sendValidationDecision("task-1", "accept");
    expect(url).toContain("/v1/tasks/task-1/validation-decision");
    expect(JSON.parse(body)).toEqual({ decision: "accept" });
    expect(result.taskId).toBe("task-1");
    expect(result.status).toBe("AWAITING_VALIDATION_DECISION");
  });

  test("sendValidationDecision throws on 409", async () => {
    const client = new HttpBackendClient({
      baseUrl: "http://localhost:8000",
      fetchFn: async () =>
        new Response(JSON.stringify({ detail: "not awaiting" }), { status: 409 })
    });
    await expect(client.sendValidationDecision("task-x", "accept")).rejects.toThrow();
  });

  // ── Chat API ──────────────────────────────────────────────────────────────

  test("createChatThread sends correct body and maps response", async () => {
    let capturedUrl = "";
    let capturedBody = "";
    const client = new HttpBackendClient({
      baseUrl: "http://localhost:8000",
      fetchFn: async (url, init) => {
        capturedUrl = String(url);
        capturedBody = String(init?.body ?? "");
        return new Response(
          JSON.stringify({
            thread_id: "chat-xyz",
            workspace_path: "/ws",
            title: "My thread",
            created_at: "2026-05-11T00:00:00Z",
          }),
          { status: 200, headers: { "content-type": "application/json" } }
        );
      },
    });
    const result = await client.createChatThread("/ws", "My thread");
    expect(capturedUrl).toContain("/v1/chat/threads");
    expect(JSON.parse(capturedBody)).toEqual({ workspace: "/ws", title: "My thread" });
    expect(result.threadId).toBe("chat-xyz");
    expect(result.createdAt).toBe("2026-05-11T00:00:00Z");
  });

  test("listChatThreads maps snake_case to camelCase", async () => {
    const client = new HttpBackendClient({
      baseUrl: "http://localhost:8000",
      fetchFn: async () =>
        new Response(
          JSON.stringify({
            threads: [
              {
                thread_id: "chat-abc123",
                workspace_path: "/ws",
                title: "My chat",
                created_at: "2026-05-11T00:00:00Z",
              },
            ],
          }),
          { status: 200, headers: { "content-type": "application/json" } }
        ),
    });
    const result = await client.listChatThreads("/ws");
    expect(result).toHaveLength(1);
    expect(result[0].threadId).toBe("chat-abc123");
    expect(result[0].title).toBe("My chat");
    expect(result[0].createdAt).toBe("2026-05-11T00:00:00Z");
  });

  test("getChatThread maps thread and messages", async () => {
    const client = new HttpBackendClient({
      baseUrl: "http://localhost:8000",
      fetchFn: async () =>
        new Response(
          JSON.stringify({
            thread_id: "chat-abc123",
            workspace_path: "/ws",
            title: "My chat",
            created_at: "2026-05-11T00:00:00Z",
            messages: [
              {
                role: "user",
                content: "hello",
                type: "text",
                task_id: null,
                timestamp: "2026-05-11T00:00:01Z",
                metadata: {},
              },
            ],
            touched_files: [],
          }),
          { status: 200, headers: { "content-type": "application/json" } }
        ),
    });
    const result = await client.getChatThread("chat-abc123");
    expect(result.threadId).toBe("chat-abc123");
    expect(result.messages).toHaveLength(1);
    expect(result.messages[0].role).toBe("user");
    expect(result.messages[0].content).toBe("hello");
  });

  test("getThreadLiveState maps a gate payload to camelCase", async () => {
    let capturedUrl = "";
    const client = new HttpBackendClient({
      baseUrl: "http://localhost:8000",
      fetchFn: async (url) => {
        capturedUrl = String(url);
        return new Response(
          JSON.stringify({
            active_task_id: "task-1",
            status: "AWAITING_COMMAND_DECISION",
            pending_gate: { kind: "command", payload: { command: "pytest" } },
            plan: null,
          }),
          { status: 200, headers: { "content-type": "application/json" } }
        );
      },
    });
    const live = await client.getThreadLiveState("chat-abc123");
    expect(capturedUrl).toContain("/v1/chat/threads/chat-abc123/live");
    expect(live.activeTaskId).toBe("task-1");
    expect(live.status).toBe("AWAITING_COMMAND_DECISION");
    expect(live.pendingGate?.kind).toBe("command");
    expect(live.pendingGate?.payload.command).toBe("pytest");
    expect(live.plan).toBeNull();
  });

  test("getThreadLiveState maps null gate/plan for an idle thread", async () => {
    const client = new HttpBackendClient({
      baseUrl: "http://localhost:8000",
      fetchFn: async () =>
        new Response(
          JSON.stringify({
            active_task_id: null,
            status: null,
            pending_gate: null,
            plan: null,
          }),
          { status: 200, headers: { "content-type": "application/json" } }
        ),
    });
    const live = await client.getThreadLiveState("chat-idle");
    expect(live.activeTaskId).toBeNull();
    expect(live.pendingGate).toBeNull();
    expect(live.plan).toBeNull();
  });

  test("sendChatMessage streams SSE events", async () => {
    const sseBody =
      'data: {"type":"intent_classified","payload":{"intent":"qa"}}\n\n' +
      'data: {"type":"chat_done","payload":{}}\n\n';
    const client = new HttpBackendClient({
      baseUrl: "http://localhost:8000",
      fetchFn: async () =>
        new Response(new TextEncoder().encode(sseBody), {
          status: 200,
          headers: { "content-type": "text/event-stream" },
        }),
    });
    const events: Array<{ type: string }> = [];
    for await (const event of client.sendChatMessage("chat-abc123", "hello")) {
      events.push(event);
    }
    expect(events[0].type).toBe("intent_classified");
    expect(events[1].type).toBe("chat_done");
  });

  // ── Tier B lifecycle control + durable telemetry ───────────────────────────

  test("abortTask posts {revert} and maps the TaskView", async () => {
    let url = "";
    let body = "";
    const client = new HttpBackendClient({
      baseUrl: "http://localhost:8000",
      fetchFn: async (input, init) => {
        url = String(input);
        body = String(init?.body ?? "");
        return new Response(
          JSON.stringify({ task_id: "task-1", status: "ABORTED", goal: "g", modified_files: [], diagnostics: [] }),
          { status: 200, headers: { "content-type": "application/json" } }
        );
      }
    });
    const view = await client.abortTask("task-1", { revert: true });
    expect(url).toContain("/v1/tasks/task-1/abort");
    expect(JSON.parse(body)).toEqual({ revert: true });
    expect(view.status).toBe("ABORTED");
  });

  test("setReviewPref posts {auto_accept} (snake_case wire)", async () => {
    let url = "";
    let body = "";
    const client = new HttpBackendClient({
      baseUrl: "http://localhost:8000",
      fetchFn: async (input, init) => {
        url = String(input);
        body = String(init?.body ?? "");
        return new Response(
          JSON.stringify({ task_id: "task-1", status: "EXECUTING", goal: "g", modified_files: [], diagnostics: [] }),
          { status: 200, headers: { "content-type": "application/json" } }
        );
      }
    });
    await client.setReviewPref("task-1", { autoAccept: false });
    expect(url).toContain("/v1/tasks/task-1/review-pref");
    expect(JSON.parse(body)).toEqual({ auto_accept: false });
  });

  test("getThreadLiveState maps failure_summary and run_summary to camelCase", async () => {
    const client = new HttpBackendClient({
      baseUrl: "http://localhost:8000",
      fetchFn: async () =>
        new Response(
          JSON.stringify({
            active_task_id: "task-1",
            status: "FAILED",
            pending_gate: null,
            plan: null,
            failure_summary: { step_id: "s3", step_index: 3, error_class: "VerifyPhaseExhausted", message: "boom" },
            run_summary: { steps_completed: 2, steps_total: 4, deviations: ["1 delta replan(s)"] },
          }),
          { status: 200, headers: { "content-type": "application/json" } }
        ),
    });
    const live = await client.getThreadLiveState("chat-abc123");
    expect(live.failureSummary?.errorClass).toBe("VerifyPhaseExhausted");
    expect(live.failureSummary?.stepIndex).toBe(3);
    expect(live.runSummary?.stepsCompleted).toBe(2);
    expect(live.runSummary?.deviations).toEqual(["1 delta replan(s)"]);
  });

  test("getTaskResult leaves summaries undefined when the wire omits them", async () => {
    const client = new HttpBackendClient({
      baseUrl: "http://localhost:8000",
      fetchFn: async () =>
        new Response(
          JSON.stringify({ task_id: "task-1", status: "SUCCEEDED", modified_files: [], diagnostics: [] }),
          { status: 200, headers: { "content-type": "application/json" } }
        ),
    });
    const result = await client.getTaskResult("task-1");
    expect(result.failureSummary).toBeUndefined();
    expect(result.runSummary).toBeUndefined();
  });
});
