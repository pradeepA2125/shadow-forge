export type TaskStatus =
  | "QUEUED"
  | "CONTEXT_READY"
  | "PLANNED"
  | "PATCHED"
  | "VALIDATING"
  | "REPAIRING"
  | "READY_FOR_REVIEW"
  | "PROMOTING"
  | "SUCCEEDED"
  | "FAILED"
  | "ABORTED";

export interface TaskBudget {
  maxIterations: number;
  maxFilesTouched: number;
  maxTokens: number;
  maxRuntimeMs: number;
}

export interface TaskUsage {
  iterations: number;
  tokensUsed: number;
}

export interface TaskEvent {
  at: string;
  from: TaskStatus;
  to: TaskStatus;
  reason: string;
}

export interface Diagnostic {
  source: string;
  file?: string;
  line?: number;
  column?: number;
  message: string;
  level: "error" | "warning";
}

export interface PlanStep {
  id: string;
  goal: string;
  targets: string[];
  risk: "low" | "med" | "high";
}

export interface PlanDocument {
  analysis: string;
  steps: PlanStep[];
  expected_files: string[];
  stop_conditions: string[];
}

export type PatchOperation =
  | {
      op: "replace_range";
      file: string;
      anchor: {
        start_line: number;
        end_line: number;
      };
      content: string;
      reason: string;
    }
  | {
      op: "insert_after_symbol";
      file: string;
      anchor: {
        symbol: string;
      };
      content: string;
      reason: string;
    }
  | {
      op: "create_file";
      file: string;
      content: string;
      reason: string;
    }
  | {
      op: "delete_file";
      file: string;
      reason: string;
    };

export interface TaskRecord {
  taskId: string;
  goal: string;
  status: TaskStatus;
  plan?: PlanDocument;
  completedStepIds: string[];
  modifiedFiles: string[];
  diagnostics: Diagnostic[];
  budget: TaskBudget;
  usage: TaskUsage;
  events: TaskEvent[];
  createdAt: string;
  updatedAt: string;
}

export interface ValidationResult {
  success: boolean;
  diagnostics: Diagnostic[];
  durationMs: number;
}

export interface PatchResult {
  touchedFiles: string[];
}
