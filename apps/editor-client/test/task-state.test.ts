import { describe, expect, test } from "vitest";
import { canTransition, createTaskRecord, transitionTask } from "../src/domain/task-state.js";

describe("task state machine", () => {
  test("allows valid transition sequence", () => {
    const task = createTaskRecord({
      taskId: "t1",
      goal: "goal",
      budget: {
        maxIterations: 3,
        maxFilesTouched: 3,
        maxTokens: 1000,
        maxRuntimeMs: 10_000
      }
    });

    const t1 = transitionTask(task, "CONTEXT_READY", "context ready");
    const t2 = transitionTask(t1, "PLANNED", "planned");
    expect(t2.status).toBe("PLANNED");
    expect(t2.events).toHaveLength(2);
    expect(canTransition("PLANNED", "PATCHED")).toBe(true);
  });

  test("supports review and promotion transitions", () => {
    const task = createTaskRecord({
      taskId: "t-review",
      goal: "goal",
      budget: {
        maxIterations: 3,
        maxFilesTouched: 3,
        maxTokens: 1000,
        maxRuntimeMs: 10_000
      }
    });

    const contextReady = transitionTask(task, "CONTEXT_READY", "context");
    const planned = transitionTask(contextReady, "PLANNED", "planned");
    const patched = transitionTask(planned, "PATCHED", "patched");
    const validating = transitionTask(patched, "VALIDATING", "validating");
    const review = transitionTask(validating, "READY_FOR_REVIEW", "ready");
    const promoting = transitionTask(review, "PROMOTING", "promoting");
    const succeeded = transitionTask(promoting, "SUCCEEDED", "done");

    expect(succeeded.status).toBe("SUCCEEDED");
  });

  test("rejects invalid transition", () => {
    const task = createTaskRecord({
      taskId: "t2",
      goal: "goal",
      budget: {
        maxIterations: 3,
        maxFilesTouched: 3,
        maxTokens: 1000,
        maxRuntimeMs: 10_000
      }
    });

    expect(() => transitionTask(task, "PLANNED", "invalid")).toThrow(
      "Invalid transition"
    );
  });
});
