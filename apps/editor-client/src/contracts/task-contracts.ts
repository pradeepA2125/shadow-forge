import { z } from "zod";
import { DiagnosticsSchema, PlanSchema } from "../domain/schemas.js";
import type { PatchOperation, PlanDocument, TaskRecord, TaskStatus } from "../domain/types.js";

export type { PatchOperation, PlanDocument, TaskRecord, TaskStatus };

export const TaskStatusSchema = z.enum([
  "QUEUED",
  "CONTEXT_READY",
  "AWAITING_PLAN_APPROVAL",
  "PLANNED",
  "EXECUTING",
  "AWAITING_SCOPE_DECISION",
  "AWAITING_STEP_REVIEW",
  "VALIDATING",
  "REPAIRING",
  "AWAITING_VALIDATION_DECISION",
  "AWAITING_COMMAND_DECISION",
  "VALIDATED",
  "READY_FOR_REVIEW",
  "PROMOTING",
  "SUCCEEDED",
  "FAILED",
  "ABORTED"
]);

export const TaskSubmissionSchema = z.object({
  goal: z.string().min(1),
  workspacePath: z.string().min(1),
  mode: z.enum(["inline", "file_edit", "project_edit", "autonomous"])
});

// Durable lifecycle telemetry (Tier B). camelCase mirror of backend FailureSummary/
// RunSummary; lets the Error/Review cards render from state on reload.
export const FailureSummarySchema = z.object({
  stepId: z.string().nullable().optional(),
  stepIndex: z.number().int().nullable().optional(),
  errorClass: z.string(),
  message: z.string()
});
export type FailureSummary = z.infer<typeof FailureSummarySchema>;

export const RunSummarySchema = z.object({
  stepsCompleted: z.number().int(),
  stepsTotal: z.number().int(),
  deviations: z.array(z.string()).default([])
});
export type RunSummary = z.infer<typeof RunSummarySchema>;

// LLM-authored narrative of the run (headline + points), for the Review/Error cards.
export const TaskNarrativeSchema = z.object({
  outcome: z.enum(["succeeded", "failed", "aborted"]),
  headline: z.string(),
  points: z.array(z.string()).default([])
});
export type TaskNarrative = z.infer<typeof TaskNarrativeSchema>;

export const TaskViewSchema = z.object({
  taskId: z.string().min(1),
  status: TaskStatusSchema,
  goal: z.string().min(1),
  modifiedFiles: z.array(z.string()),
  diagnostics: DiagnosticsSchema,
  planMarkdown: z.string().optional(),
  resumeOfTaskId: z.string().optional(),
  failureSummary: FailureSummarySchema.nullable().optional(),
  runSummary: RunSummarySchema.nullable().optional(),
  taskNarrative: TaskNarrativeSchema.nullable().optional()
});

export const TaskResultSchema = z.object({
  taskId: z.string().min(1),
  status: TaskStatusSchema,
  plan: PlanSchema.optional(),
  planMarkdown: z.string().optional(),
  patch: z.unknown().optional(),
  modifiedFiles: z.array(z.string()),
  diagnostics: DiagnosticsSchema,
  promotedAt: z.string().nullable().optional(),
  shadowWorkspacePath: z.string().nullable().optional(),
  resumeOfTaskId: z.string().optional(),
  failureSummary: FailureSummarySchema.nullable().optional(),
  runSummary: RunSummarySchema.nullable().optional(),
  taskNarrative: TaskNarrativeSchema.nullable().optional()
});

export const ResumeTaskRequestSchema = z.object({
  stage: z.enum(["plan", "feedback", "execute", "validate"]),
  budgetOverride: z.object({
    maxIterations: z.number().int().optional(),
    maxTokens: z.number().int().optional(),
    maxFilesTouched: z.number().int().optional(),
    maxRuntimeMs: z.number().int().optional()
  }).optional()
});

export const ResumeTaskResponseSchema = z.object({
  taskId: z.string().min(1),
  resumeOfTaskId: z.string().min(1)
});

export const ScopeDecisionRequestSchema = z.object({
  decision: z.enum(["approve", "reject"]),
  files: z.array(z.string()).default([]),
  remember: z.boolean().default(false)
});

export const StepDecisionRequestSchema = z.object({
  decision: z.enum(["accept", "discard"])
});

export const ScopeDecisionResponseSchema = z.object({
  taskId: z.string().min(1),
  status: TaskStatusSchema
});

export const ValidationDecisionRequestSchema = z.object({
  decision: z.enum(["accept", "reject"])
});

export const ValidationDecisionResponseSchema = z.object({
  taskId: z.string().min(1),
  status: TaskStatusSchema
});

export const CommandDecisionSchema = z.object({
  approve: z.boolean(),
  remember: z.boolean().default(false),
  scope: z.enum(["exact", "prefix", "binary"]).default("exact"),
  // For approve+remember: the rule value the UI chose (shlex-joined leading
  // tokens for "prefix", full shlex-join for "exact"; omitted for "binary",
  // engine derives basename). Mirrors backend CommandDecision.rule_value.
  ruleValue: z.string().optional(),
});

export const CommandDecisionResponseSchema = z.object({
  taskId: z.string().min(1),
  status: TaskStatusSchema
});

export type TaskSubmission = z.infer<typeof TaskSubmissionSchema>;
export type TaskView = z.infer<typeof TaskViewSchema>;
export type TaskResult = z.infer<typeof TaskResultSchema>;
export type ResumeTaskRequest = z.infer<typeof ResumeTaskRequestSchema>;
export type ResumeTaskResponse = z.infer<typeof ResumeTaskResponseSchema>;
export type ScopeDecisionRequest = z.infer<typeof ScopeDecisionRequestSchema>;
export type ScopeDecisionResponse = z.infer<typeof ScopeDecisionResponseSchema>;
export type StepDecisionRequest = z.infer<typeof StepDecisionRequestSchema>;
export type ValidationDecisionRequest = z.infer<typeof ValidationDecisionRequestSchema>;
export type ValidationDecisionResponse = z.infer<typeof ValidationDecisionResponseSchema>;
export type CommandDecision = z.infer<typeof CommandDecisionSchema>;
export type CommandDecisionResponse = z.infer<typeof CommandDecisionResponseSchema>;

export interface DiffEntry {
  path: string;
  additions: number;
  deletions: number;
  tempPath: string;
}

export type StreamEvent =
  | { type: "operation_success"; payload: { op_type: string; path: string } }
  | { type: "operation_error"; payload: { op_type: string; path: string; error: string } }
  // done carries {status} when the engine reaches a terminal/pause state (engine.py:1550) and {} on bare pause paths
  | { type: "done"; payload: { status?: string } }
  | { type: "tool_call"; payload: { tool: string; thought: string; iteration?: number; phase?: string; args?: Record<string, unknown>; call_index?: number } }
  | { type: "tool_result"; payload: { tool?: string; output: string; is_error: boolean; iteration?: number; call_index?: number } }
  | { type: "planning_tool_call"; payload: { tool: string; thought: string; iteration: number; args?: Record<string, unknown> } }
  | { type: "planning_tool_result"; payload: { tool: string; output: string; is_error: boolean; iteration: number } }
  | { type: "explore_tool_result"; payload: { tool: string; output: string; is_error: boolean } }
  | { type: "planning_thinking_chunk"; payload: { chunk: string; iteration: number } }
  | { type: "planning_complete"; payload: { files_examined: string[]; confidence: string } }
  | { type: "revision_needed"; payload: { step_id: string; reason: string; evidence: string } }
  | { type: "patch_applied"; payload: { step_id: string; phase: string; touched_files: string[] } }
  | { type: "patch_failed"; payload: { step_id: string; error: string } }
  | { type: "step_started"; payload: { step_id: string; step_title: string; step_index: number; total_steps: number } }
  | { type: "scope_extension_requested"; payload: { decision_id: string; files: string[]; reason: string; step_id: string } }
  | { type: "validation_decision_requested"; payload: { task_id: string; diagnostics: Array<{ source: string; message: string; level: string }> } }
  | { type: "command_approval_requested"; payload: { decision_id: string; command: string; args: string[]; cwd: string; step_id: string } }
  | { type: "tool_thinking_chunk"; payload: { chunk: string } }
  | { type: "chat_agent_thinking"; payload: { message: string } }
  | { type: "chat_agent_thinking_chunk"; payload: { chunk: string } }
  | { type: "explore_tool_call"; payload: { tool: string; args: Record<string, unknown>; thought?: string } }
  | { type: "intent_classified"; payload: { intent: string; rationale: string; likely_targets: string[] } }
  | { type: "chat_response"; payload: { chunk: string } }
  | { type: "chat_done"; payload: Record<string, never> }
  | { type: "task_card"; payload: { task_id: string } }
  | { type: "plan_card"; payload: { task_id: string; plan_markdown: string } }
  | { type: "task_status_changed"; payload: { task_id: string; status: string; plan_markdown?: string; message?: string } }
  | { type: "diff_ready"; payload: { task_id: string; diff_entries: DiffEntry[]; thinking_log: string[]; completed_steps: number; total_steps: number; resolved?: "applied" | "discarded" } }
  | { type: "thread_title_updated"; payload: { thread_id: string; title: string } }
  | { type: "step_review_requested"; payload: { step_id: string; step_title: string; diff_entries: DiffEntry[] } }
  | { type: "env_profile_building"; payload: { workspace_root: string } }
  | { type: "env_profile_built"; payload: { ecosystems_count: number; bootstrap_needed: boolean } }
  | { type: "env_install_running"; payload: { scope_key: string; command: string } }
  | { type: "env_install_done"; payload: { scope_key: string; exit_ok: boolean; tail: string } }
  | { type: "chat_breadcrumb"; payload: { text: string; task_id: string } };

// Backward-compat alias
export type PatchStreamEvent = StreamEvent;

// ── Chat types ────────────────────────────────────────────────────────────

export const ChatMessageSchema = z.object({
  role: z.enum(["user", "agent"]),
  content: z.string(),
  type: z.enum(["text", "plan_card", "diff_card", "diff_summary", "task_card", "scope_card", "validation_card", "command_card"]).default("text"),
  taskId: z.string().nullable().optional(),
  timestamp: z.string(),
  metadata: z.record(z.unknown()).default({}),
});
export type ChatMessage = z.infer<typeof ChatMessageSchema>;

export const ChatThreadSummarySchema = z.object({
  threadId: z.string(),
  workspacePath: z.string(),
  title: z.string(),
  createdAt: z.string(),
  // Enriched list fields (chat UI v2 Tier A) -- optional so a bare summary
  // (e.g. POST /chat/threads response) still parses.
  updatedAt: z.string().optional(),
  messageCount: z.number().optional(),
  status: z.enum(["running", "review", "done", "failed"]).nullable().optional(),
});
export type ChatThreadSummary = z.infer<typeof ChatThreadSummarySchema>;

export const ChatThreadSchema = z.object({
  threadId: z.string(),
  workspacePath: z.string(),
  title: z.string(),
  messages: z.array(ChatMessageSchema),
  touchedFiles: z.array(z.string()),
});
export type ChatThread = z.infer<typeof ChatThreadSchema>;

export const ChatEventSchema = z.object({
  type: z.string(),
  payload: z.record(z.unknown()).default({}),
});
export type ChatEvent = z.infer<typeof ChatEventSchema>;

// The single gate a thread's current task is waiting on (mirrors backend PendingGate).
// "mode"/"edit"/"clarify" are the agentic chat-controller gates (no task) — the Zod enum
// is the RUNTIME gate: a kind missing here makes ThreadLiveStateSchema.parse() throw, which
// pollThreadLiveState swallows, so the gate silently never renders.
export const PendingGateSchema = z.object({
  kind: z.enum(["command", "step", "scope", "validation", "mode", "edit", "clarify"]),
  payload: z.record(z.unknown()).default({}),
});
export type PendingGate = z.infer<typeof PendingGateSchema>;

// One item of the controller's live todo checklist (the write_todos ledger).
export const TodoItemSchema = z.object({
  title: z.string(),
  status: z.enum(["pending", "in_progress", "done", "blocked", "cancelled"]),
  note: z.string().optional().default(""),
});
export type TodoItem = z.infer<typeof TodoItemSchema>;

// A thread's current actionable state — what the UI polls and renders from.
// Resolved server-side from the thread's active task (GET /chat/threads/{id}/live),
// so reloads and resume task-id churn self-heal on the next poll.
export const ThreadLiveStateSchema = z.object({
  activeTaskId: z.string().nullable(),
  status: z.string().nullable(),
  pendingGate: PendingGateSchema.nullable(),
  plan: z.record(z.unknown()).nullable(),
  // True while a controller turn / held-open controller gate is in flight (durable
  // input-disable signal that survives a webview reload). Absent on legacy payloads → false.
  turnActive: z.boolean().default(false),
  // Durable lifecycle telemetry (Tier B): drives the Error/Review cards from poll state.
  failureSummary: FailureSummarySchema.nullable().optional(),
  runSummary: RunSummarySchema.nullable().optional(),
  taskNarrative: TaskNarrativeSchema.nullable().optional(),
  todos: z.array(TodoItemSchema).nullable().optional(),
});
export type ThreadLiveState = z.infer<typeof ThreadLiveStateSchema>;

// Backend feature-flag capabilities (GET /v1/config) — drives task-path UI gating.
export const BackendConfigSchema = z.object({
  taskSubsystemEnabled: z.boolean(),
  chatControllerEnabled: z.boolean(),
});
export type BackendConfig = z.infer<typeof BackendConfigSchema>;

export interface BackendTaskClient {
  submitTask(input: TaskSubmission): Promise<{ taskId: string }>;
  getTask(taskId: string): Promise<TaskView>;
  getTaskResult(taskId: string): Promise<TaskResult>;
  cancelTask(taskId: string): Promise<{ taskId: string; status: TaskStatus }>;
  // Cooperative Stop for a running task: revert rolls the workspace back, otherwise keeps
  // the changes applied so far (Tier B).
  abortTask(taskId: string, options: { revert: boolean }): Promise<TaskView>;
  // Live-mutable "Review each step" preference for a running task (Tier B).
  setReviewPref(taskId: string, options: { autoAccept: boolean }): Promise<TaskView>;
  acceptPatch(taskId: string): Promise<TaskResult>;
  rejectPatch(taskId: string, reason: string): Promise<TaskResult>;
  providePlanFeedback(taskId: string, feedback: string | null): Promise<TaskView>;
  resumeTask(taskId: string, options?: ResumeTaskRequest): Promise<ResumeTaskResponse>;
  sendScopeDecision(taskId: string, decision: ScopeDecisionRequest): Promise<ScopeDecisionResponse>;
  sendValidationDecision(taskId: string, decision: "accept" | "reject"): Promise<ValidationDecisionResponse>;
  sendCommandDecision(taskId: string, decision: CommandDecision): Promise<CommandDecisionResponse>;
  sendStepDecision(taskId: string, decision: "accept" | "discard"): Promise<void>;
  streamPatch(taskId: string, onEvent: (event: StreamEvent) => void, signal?: AbortSignal): Promise<void>;
  streamPatchEvents(taskId: string): AsyncIterable<StreamEvent>;
  listChatThreads(workspacePath: string): Promise<ChatThreadSummary[]>;
  createChatThread(workspacePath: string, title?: string): Promise<ChatThreadSummary>;
  getChatThread(threadId: string): Promise<ChatThread>;
  getThreadLiveState(threadId: string): Promise<ThreadLiveState>;
  getConfig(): Promise<BackendConfig>;
  sendChatMessage(threadId: string, message: string, signal?: AbortSignal, options?: { stepReview?: boolean }): AsyncIterable<StreamEvent>;
  // Controller gates (Phase F): the mode gate is a STREAMED dispatch (edit/create_task
  // produce live events); the per-edit gate is a plain JSON ack (its continuation rides
  // the already-open message stream).
  postModeDecision(threadId: string, mode: string): AsyncIterable<StreamEvent>;
  // Controller clarify gate: a STREAMED dispatch (the answer re-enters the loop).
  postClarifyDecision(threadId: string, answer: string): AsyncIterable<StreamEvent>;
  postEditDecision(threadId: string, decision: "accept" | "reject", reason?: string): Promise<void>;
  // Controller run_command gate: a plain JSON ack (continuation rides the open message stream).
  postChatCommandDecision(threadId: string, decision: CommandDecision): Promise<void>;
  // Stop a detached controller turn (POST /chat/threads/{id}/stop). ok=false is benign.
  stopChatTurn(threadId: string): Promise<{ ok: boolean }>;
  // Subscribe-only SSE to any broadcaster channel (GET /v1/channels/{id}/stream). Used
  // to resume the live overlay for a controller turn after a webview reload (chat:{id}).
  streamChannel(channelId: string): AsyncIterable<StreamEvent>;
  applyInlineChange(inlineTaskId: string): Promise<void>;
  discardInlineChange(inlineTaskId: string): Promise<void>;
}
