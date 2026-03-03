import { describe, expect, test, vi } from "vitest";

import { shouldStopPolling, TaskPoller } from "../src/task-poller.js";

describe("TaskPoller", () => {
  test("stops polling once task reaches READY_FOR_REVIEW", async () => {
    vi.useFakeTimers();

    const statuses = ["QUEUED", "PLANNED", "READY_FOR_REVIEW", "SUCCEEDED"] as const;
    const updates: string[] = [];
    let index = 0;

    const poller = new TaskPoller({
      intervalMs: 1000,
      poll: async () => ({
        taskId: "task-1",
        goal: "goal",
        status: statuses[Math.min(index++, statuses.length - 1)],
        modifiedFiles: [],
        diagnostics: [],
      }),
      onUpdate: async (task) => {
        updates.push(task.status);
      },
    });

    poller.start();
    await vi.advanceTimersByTimeAsync(3_500);

    expect(updates).toEqual(["QUEUED", "PLANNED", "READY_FOR_REVIEW"]);
    expect(poller.isRunning()).toBe(false);

    vi.useRealTimers();
  });

  test("stop-status helper matches review and terminal states", () => {
    expect(shouldStopPolling("READY_FOR_REVIEW")).toBe(true);
    expect(shouldStopPolling("SUCCEEDED")).toBe(true);
    expect(shouldStopPolling("FAILED")).toBe(true);
    expect(shouldStopPolling("ABORTED")).toBe(true);
    expect(shouldStopPolling("PATCHED")).toBe(false);
  });
});
