import type { TaskBudget, TaskRecord, TaskStatus } from "./types.js";

const transitions: Record<TaskStatus, ReadonlySet<TaskStatus>> = {
  QUEUED: new Set(["CONTEXT_READY", "FAILED", "ABORTED"]),
  CONTEXT_READY: new Set(["PLANNED", "FAILED", "ABORTED"]),
  PLANNED: new Set(["PATCHED", "FAILED", "ABORTED"]),
  PATCHED: new Set(["VALIDATING", "FAILED", "ABORTED"]),
  VALIDATING: new Set(["READY_FOR_REVIEW", "REPAIRING", "FAILED", "ABORTED"]),
  REPAIRING: new Set(["PATCHED", "FAILED", "ABORTED"]),
  READY_FOR_REVIEW: new Set(["PROMOTING", "FAILED", "ABORTED"]),
  PROMOTING: new Set(["SUCCEEDED", "FAILED", "ABORTED"]),
  SUCCEEDED: new Set(),
  FAILED: new Set(),
  ABORTED: new Set()
};

export function createTaskRecord(input: {
  taskId: string;
  goal: string;
  budget: TaskBudget;
}): TaskRecord {
  const now = new Date().toISOString();
  return {
    taskId: input.taskId,
    goal: input.goal,
    status: "QUEUED",
    completedStepIds: [],
    modifiedFiles: [],
    diagnostics: [],
    budget: input.budget,
    usage: {
      iterations: 0,
      tokensUsed: 0
    },
    events: [],
    createdAt: now,
    updatedAt: now
  };
}

export function canTransition(from: TaskStatus, to: TaskStatus): boolean {
  return transitions[from].has(to);
}

export function transitionTask(
  record: TaskRecord,
  to: TaskStatus,
  reason: string
): TaskRecord {
  if (!canTransition(record.status, to)) {
    throw new Error(`Invalid transition: ${record.status} -> ${to}`);
  }

  const now = new Date().toISOString();
  return {
    ...record,
    status: to,
    updatedAt: now,
    events: [
      ...record.events,
      {
        at: now,
        from: record.status,
        to,
        reason
      }
    ]
  };
}

export function bumpUsage(record: TaskRecord, input: { tokensUsed?: number }): TaskRecord {
  return {
    ...record,
    usage: {
      iterations: record.usage.iterations + 1,
      tokensUsed: record.usage.tokensUsed + (input.tokensUsed ?? 0)
    },
    updatedAt: new Date().toISOString()
  };
}

export function assertBudget(record: TaskRecord, startedAtMs: number): void {
  if (record.usage.iterations > record.budget.maxIterations) {
    throw new Error("Iteration budget exceeded");
  }

  if (record.usage.tokensUsed > record.budget.maxTokens) {
    throw new Error("Token budget exceeded");
  }

  if (Date.now() - startedAtMs > record.budget.maxRuntimeMs) {
    throw new Error("Runtime budget exceeded");
  }

  if (record.modifiedFiles.length > record.budget.maxFilesTouched) {
    throw new Error("Modified file budget exceeded");
  }
}
