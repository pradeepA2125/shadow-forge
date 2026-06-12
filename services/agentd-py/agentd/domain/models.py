from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, field_validator, model_validator


class TaskStatus(StrEnum):
    QUEUED = "QUEUED"
    CONTEXT_READY = "CONTEXT_READY"
    AWAITING_PLAN_APPROVAL = "AWAITING_PLAN_APPROVAL"
    PLANNED = "PLANNED"
    EXECUTING = "EXECUTING"
    AWAITING_SCOPE_DECISION = "AWAITING_SCOPE_DECISION"
    AWAITING_STEP_REVIEW = "AWAITING_STEP_REVIEW"
    VALIDATING = "VALIDATING"
    REPAIRING = "REPAIRING"
    AWAITING_VALIDATION_DECISION = "AWAITING_VALIDATION_DECISION"
    AWAITING_COMMAND_DECISION = "AWAITING_COMMAND_DECISION"
    VALIDATED = "VALIDATED"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    PROMOTING = "PROMOTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


_LEGACY_STATUS_ALIASES: dict[str, str] = {
    "PATCHED": "EXECUTING",
}


def normalize_task_status(value: object) -> TaskStatus:
    if isinstance(value, TaskStatus):
        return value
    if isinstance(value, str):
        return TaskStatus(_LEGACY_STATUS_ALIASES.get(value, value))
    msg = f"Unsupported task status value: {value!r}"
    raise TypeError(msg)


class PatchFailureCode(StrEnum):
    SCOPE_VIOLATION = "scope_violation"
    FILE_MISSING = "file_missing"
    FILE_EXISTS = "file_exists"
    RANGE_INVALID = "range_invalid"
    ANCHOR_MISSING = "anchor_missing"
    ANCHOR_AMBIGUOUS = "anchor_ambiguous"
    ORDER_CONFLICT = "order_conflict"
    PYTHON_UNSAFE_INSERT = "python_unsafe_insert"
    PATH_ESCAPE = "path_escape"
    POLICY_VIOLATION = "policy_violation"
    PARSER_UNAVAILABLE = "parser_unavailable"
    APPLY_ERROR = "apply_error"


class TaskBudget(BaseModel):
    max_iterations: int = 6
    max_files_touched: int = 20
    max_tokens: int = 120_000
    max_runtime_ms: int = 90 * 60 * 1000
    max_tool_calls_per_step: int = 50
    max_planning_tool_calls: int = 50
    max_revision_tool_calls: int = 50
    max_delta_replans: int = 3
    max_verify_calls_per_step: int = 8


class TaskUsage(BaseModel):
    iterations: int = 0
    tokens_used: int = 0
    tool_calls_used: int = 0


class ToolCall(BaseModel):
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    # The model's free-text reasoning for this call — surfaced in the persisted
    # chat tool pill; the live SSE event already carries it.
    thought: str | None = None


class ToolResult(BaseModel):
    call_id: str
    tool_name: str
    output: str
    is_error: bool = False


class AgentToolTrace(BaseModel):
    step_id: str
    calls: list[ToolCall] = Field(default_factory=list)
    results: list[ToolResult] = Field(default_factory=list)


class DeltaReplanRequest(BaseModel):
    requested_by_step_id: str
    reason: str
    evidence: str
    hinted_affected_steps: list[str]
    requested_at: datetime


class ScopePolicy(StrEnum):
    """How the engine handles patches that target files outside the step's scope."""
    STRICT = "strict"   # auto-reject (default — current behavior)
    ASK = "ask"         # pause + user gate via POST /scope-decision
    AUTO = "auto"       # auto-approve + audit log


class ScopeTrigger(StrEnum):
    """Which out-of-scope files trip the gate."""
    NEARBY = "nearby"   # same dir as a target OR conventional pattern (__init__.py, conftest.py)
    ANY = "any"         # every out-of-scope file


class ScopeRemember(StrEnum):
    """How long an approval persists."""
    TASK = "task"       # remember within current task
    NONE = "none"       # ask every time


class ScopeExtensionRequest(BaseModel):
    """Persisted on the task while the engine waits for a scope decision."""
    decision_id: str
    files: list[str]
    reason: str
    step_id: str


class ScopeDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    files: list[str] = Field(default_factory=list)
    remember: bool = False


class ScopeDecisionResponse(BaseModel):
    task_id: str
    status: "TaskStatus"


class ValidationDecisionRequest(BaseModel):
    decision: Literal["accept", "reject"]


class ValidationDecisionResponse(BaseModel):
    task_id: str
    status: "TaskStatus"


class CommandDecisionResponse(BaseModel):
    task_id: str
    status: "TaskStatus"


class ShellPolicy(StrEnum):
    """How run_command is gated. Default ASK — every command surfaced for
    Accept-once / Accept-and-remember / Reject. ALLOW_ALL skips the gate."""
    ASK = "ask"
    ALLOW_ALL = "allow_all"


class CommandApprovalRequest(BaseModel):
    """Persisted on the task while the engine waits for a command decision."""
    decision_id: str
    command: str
    args: list[str] = Field(default_factory=list)
    cwd: str = ""
    step_id: str


class CommandDecision(BaseModel):
    approve: bool
    remember: bool = False
    scope: Literal["exact", "prefix", "binary"] = "exact"
    # For approve+remember: the rule value the UI chose (e.g. "python -c").
    # When omitted the engine derives it from the request per `scope`.
    rule_value: str | None = None


class CommandRule(BaseModel):
    """A persisted user-approved shell command rule (workspace store + per-task set)."""
    type: Literal["exact", "prefix", "binary"]
    value: str
    added_at: str


class StepReviewPayload(BaseModel):
    """Persisted on the task while the engine waits for a per-step accept/discard decision."""
    step_id: str
    step_title: str
    diff_entries: list[dict[str, Any]]  # serialized DiffEntry objects


class StepDecisionRequest(BaseModel):
    decision: Literal["accept", "discard"]


class TaskExecutionState(BaseModel):
    current_step_id: str | None = None
    step_checkpoints: dict[str, str] = Field(default_factory=dict)
    delta_replan_requests: list[DeltaReplanRequest] = Field(default_factory=list)
    delta_replans_used: int = 0
    auto_approved_scope_files: list[str] = Field(default_factory=list)
    pending_scope_request: ScopeExtensionRequest | None = None
    pending_step_review: StepReviewPayload | None = None
    pending_command_request: CommandApprovalRequest | None = None
    approved_commands: list[CommandRule] = Field(default_factory=list)
    pending_install_for_scope: str | None = None  # ecosystem scope_key needing setup_env before next run_command
    # Validation gate payload (parity with the other pending_* gates) so the
    # chat-thread live-state can surface it without a separate lookup.
    pending_validation: dict[str, Any] | None = None


class EnvEcosystemEntry(BaseModel):
    """One ecosystem-scope in an EnvProfile.

    Identified by (ecosystem, subdir). The scope_key property is the
    deterministic key used by manifest-write auto-sync.
    """

    ecosystem: Literal["python", "node", "rust", "go"]
    subdir: str  # relative to workspace; "" = root
    manifest_path: str  # relative to workspace
    package_manager: str  # "uv" | "pip" | "npm" | "yarn" | "pnpm" | "cargo" | "go"
    install_command: str  # ready for setup_env (e.g. "uv sync")
    interpreter_or_runner: str | None  # rel path (e.g. ".venv/bin/python")
    test_command: str | None  # rel cmd used with subdir as cwd (e.g. "pytest")
    declared_dependencies_top: list[str] = Field(default_factory=list)  # top ~20 manifest deps verbatim
    notes: str | None = None  # LLM-supplied quirks

    @property
    def scope_key(self) -> str:
        return f"{self.ecosystem}:{self.subdir}"


class EnvProfile(BaseModel):
    """Workspace-level env conventions persisted at <workspace>/.agentd/env_profile.json."""

    workspace_root: str
    built_at: datetime
    bootstrap_needed: bool = False  # probe found nothing usable; agent falls back to find_binary/init_workspace
    ecosystems: list[EnvEcosystemEntry] = Field(default_factory=list)
    conventions_notes: str | None = None  # short free-form summary from the LLM
    diagnostics: list[str] = Field(default_factory=list)  # probe warnings


class RevisedStep(BaseModel):
    step_id: str
    goal: str
    targets: list[dict[str, str]]
    implementation_details: str
    edge_cases: str = ""
    testing_strategy: str = ""
    risk: str = "low"
    test_command: str | None = None


class PlanRevisionResult(BaseModel):
    revised_steps: list[RevisedStep]
    reverted_step_ids: list[str]
    revision_summary: str
    tool_trace: AgentToolTrace


class PlanningResult(BaseModel):
    plan_markdown: str
    files_examined: list[str]
    confidence: Literal["high", "medium", "low"]
    tool_trace: AgentToolTrace
    # Verbatim planning conversation the loop ended with. Persisted on the task so a
    # later feedback round can REPLAY it (not re-digest it) with the new feedback
    # appended as the final turn — keeping the llama-server prompt-prefix cache warm.
    conversation_history: list[dict[str, object]] = Field(default_factory=list)


class TaskEvent(BaseModel):
    at: datetime
    from_status: TaskStatus
    to_status: TaskStatus
    reason: str

    @field_validator("from_status", "to_status", mode="before")
    @classmethod
    def _normalize_status(cls, value: object) -> TaskStatus:
        return normalize_task_status(value)


class Diagnostic(BaseModel):
    source: str
    message: str
    level: Literal["error", "warning"]
    file: str | None = None
    line: int | None = None
    column: int | None = None


class PlanTargetIntent(StrEnum):
    EXISTING = "existing"
    NEW = "new"


class PlanTarget(BaseModel):
    path: str
    intent: PlanTargetIntent


class PlanStep(BaseModel):
    id: str
    goal: str
    targets: list[PlanTarget]
    risk: Literal["low", "med", "high"]
    
    # Rich detail fields for enhanced patch generation (generated by LLM during plan creation)
    implementation_details: str | None = None      # Specific code changes and implementation strategy
    edge_cases: str | None = None                  # Edge cases to handle in implementation
    testing_strategy: str | None = None           # Verification approach and testing criteria
    design_rationale: str | None = None            # Technical considerations and constraints
    test_command: str | None = None               # Runnable test command (e.g. "pytest tests/test_auth.py::test_login"); only set when test file is verified or is a NEW target in this step

    @model_validator(mode="before")
    @classmethod
    def _normalize_targets(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        raw_targets = data.get("targets")
        if not isinstance(raw_targets, list):
            return data

        intents_raw = data.get("target_intents")
        intents_map = intents_raw if isinstance(intents_raw, dict) else {}
        normalized_targets: list[object] = []

        for entry in raw_targets:
            if isinstance(entry, str):
                normalized_targets.append(
                    {
                        "path": entry,
                        "intent": intents_map.get(entry, PlanTargetIntent.EXISTING.value),
                    }
                )
                continue

            if isinstance(entry, (tuple, list)):
                if not entry:
                    normalized_targets.append(entry)
                    continue
                path = entry[0]
                intent = entry[1] if len(entry) > 1 else PlanTargetIntent.EXISTING.value
                normalized_targets.append({"path": path, "intent": intent})
                continue

            if isinstance(entry, dict):
                path = entry.get("path", entry.get("file"))
                intent = entry.get("intent")
                if isinstance(path, str):
                    target_entry: dict[str, object] = {"path": path}
                    if intent is not None:
                        target_entry["intent"] = intent
                    elif path in intents_map:
                        target_entry["intent"] = intents_map[path]
                    normalized_targets.append(target_entry)
                    continue

            normalized_targets.append(entry)

        normalized_data = dict(data)
        normalized_data["targets"] = normalized_targets
        normalized_data.pop("target_intents", None)
        return normalized_data

    @model_validator(mode="after")
    def _validate_targets(self) -> "PlanStep":
        target_paths = [target.path for target in self.targets]
        if len(target_paths) != len(set(target_paths)):
            msg = f"duplicate target paths are not allowed in a plan step: {target_paths}"
            raise ValueError(msg)
        return self

    def target_intent_for(self, target: str) -> PlanTargetIntent | None:
        for plan_target in self.targets:
            if plan_target.path == target:
                return plan_target.intent
        return None

    def target_paths(self) -> list[str]:
        return [target.path for target in self.targets]


class PlanDocument(BaseModel):
    analysis: str
    steps: list[PlanStep]
    expected_files: list[str]
    stop_conditions: list[str]


class PlanEvidenceFile(BaseModel):
    path: str
    excerpt: str
    rationale: str | None = None
    line_start: int | None = None
    line_end: int | None = None


class PlanEvidenceSymbol(BaseModel):
    name: str
    kind: str
    file: str
    line: int | None = None
    snippet: str | None = None


class PlanEvidenceCategoryFacts(BaseModel):
    routes: list[str] = Field(default_factory=list)
    models: list[str] = Field(default_factory=list)
    storage: list[str] = Field(default_factory=list)


class PlanEvidencePack(BaseModel):
    workspace_files_index: list[str] = Field(default_factory=list)
    evidence_files: list[PlanEvidenceFile] = Field(default_factory=list)
    evidence_symbols: list[PlanEvidenceSymbol] = Field(default_factory=list)
    evidence_routes_models_storage: PlanEvidenceCategoryFacts = Field(
        default_factory=PlanEvidenceCategoryFacts
    )
    diagnostics_excerpt: list[str] = Field(default_factory=list)
    confidence_notes: list[str] = Field(default_factory=list)


class PlanCritiqueCode(StrEnum):
    INVENTED_FILE = "invented_file"
    INVENTED_SYMBOL = "invented_symbol"
    SCHEMA_MISMATCH = "schema_mismatch"
    REDUNDANT_CHANGE = "redundant_change"
    EXISTING_CAPABILITY_IGNORED = "existing_capability_ignored"
    VERIFICATION_MISMATCH = "verification_mismatch"
    PATH_PREFIX_MISMATCH = "path_prefix_mismatch"
    TEST_SCOPE_MISMATCH = "test_scope_mismatch"
    # Step-detail quality codes
    INSUFFICIENT_IMPLEMENTATION_DETAILS = "insufficient_implementation_details"
    INCOMPLETE_EDGE_CASES = "incomplete_edge_cases"
    VAGUE_TESTING_STRATEGY = "vague_testing_strategy"
    MISSING_DESIGN_RATIONALE = "missing_design_rationale"


class PlanCritiqueIssue(BaseModel):
    code: PlanCritiqueCode
    message: str
    file: str | None = None
    symbol: str | None = None
    evidence: str | None = None


class PlanCritiqueResult(BaseModel):
    verdict: Literal["pass", "revise"] = "pass"
    issues: list[PlanCritiqueIssue] = Field(default_factory=list)


class RangeAnchor(BaseModel):
    start_line: int = Field(gt=0)
    end_line: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_bounds(self) -> "RangeAnchor":
        if self.end_line < self.start_line:
            msg = "end_line must be >= start_line"
            raise ValueError(msg)
        return self


class SymbolAnchor(BaseModel):
    symbol: str


class ReplaceRangeOp(BaseModel):
    op: Literal["replace_range"]
    file: str
    anchor: RangeAnchor
    content: str
    reason: str


class InsertAfterSymbolOp(BaseModel):
    op: Literal["insert_after_symbol"]
    file: str
    anchor: SymbolAnchor
    content: str
    reason: str


class CreateFileOp(BaseModel):
    op: Literal["create_file"]
    file: str
    content: str
    reason: str


class DeleteFileOp(BaseModel):
    op: Literal["delete_file"]
    file: str
    reason: str


PatchOperation = Annotated[
    Union[ReplaceRangeOp, InsertAfterSymbolOp, CreateFileOp, DeleteFileOp],
    Field(discriminator="op"),
]


class PatchDocument(BaseModel):
    patch_ops: list[PatchOperation] = Field(min_length=1)


class NodeSelector(BaseModel):
    kind: Literal["symbol"] = "symbol"
    value: str = Field(min_length=1)
    match: Literal["exact", "contains"] = "exact"


class ReplaceNodeOpV2(BaseModel):
    op: Literal["replace_node"]
    file: str
    language: Literal["python", "typescript", "rust"]
    selector: NodeSelector
    content: str
    reason: str


class InsertAfterNodeOpV2(BaseModel):
    op: Literal["insert_after_node"]
    file: str
    language: Literal["python", "typescript", "rust"]
    selector: NodeSelector
    content: str
    reason: str


class CreateFileOpV2(BaseModel):
    op: Literal["create_file"]
    file: str
    content: str
    reason: str


class DeleteFileOpV2(BaseModel):
    op: Literal["delete_file"]
    file: str
    reason: str

class SearchReplaceOpV2(BaseModel):
    """Apply search/replace patch to a file.
    
    Fast apply engine: O(N) text search and replace.
    Ideal for precise, targeted edits with exact anchors.
    Inspired by Aider's search/replace format.
    """
    op: Literal["search_replace"]
    file: str
    search: str = Field(min_length=1)
    replace: str
    reason: str
    
    @model_validator(mode="after")
    def validate_search_not_empty(self) -> "SearchReplaceOpV2":
        """Ensure search text is not empty."""
        if not self.search.strip():
            raise ValueError("search text cannot be empty")
        return self


class ApplyDiffOpV2(BaseModel):
    """Apply a unified diff patch to a file.
    
    Supports standard unified diff format with @@ hunks.
    Ideal for multi-section edits and LLM-generated patches.
    Compatible with Git diff format patches.
    """
    op: Literal["apply_diff"]
    file: str
    diff: str = Field(min_length=1)
    reason: str
    
    @model_validator(mode="after")
    def validate_diff_format(self) -> "ApplyDiffOpV2":
        """Ensure diff contains valid hunk headers.
        
        Supports both standard Git-style headers and Aider-style headers (@@ ... @@).
        """
        import re
        if not re.search(r'@@\s+.*\s+@@', self.diff):
            raise ValueError("diff must contain valid hunk headers (e.g., @@ -1,1 +1,1 @@ or @@ ... @@)")
        return self



PatchOperationV2 = Annotated[
    Union[
        ReplaceNodeOpV2,
        InsertAfterNodeOpV2,
        SearchReplaceOpV2,
        ApplyDiffOpV2,
        ReplaceRangeOp,
        CreateFileOpV2,
        DeleteFileOpV2,
    ],
    Field(discriminator="op"),
]


class PatchCandidateV2(BaseModel):
    candidate_id: str = Field(min_length=1)
    patch_ops: list[PatchOperationV2] = Field(min_length=1)


class PatchDocumentV2(BaseModel):
    candidates: list[PatchCandidateV2] = Field(min_length=1)


class CandidateScoreBreakdown(BaseModel):
    preflight_pass: bool
    validation_pass: bool
    changed_lines: int = 0
    op_count: int = 0
    new_file_count: int = 0
    score: float = 0.0
    selected: bool = False


class CheckpointManifest(BaseModel):
    task_id: str
    step_id: str
    attempt: int
    candidate_id: str | None = None
    checkpoint_id: str
    checkpoint_path: str
    shadow_path: str
    file_hashes_before: dict[str, str] = Field(default_factory=dict)
    file_hashes_after: dict[str, str] = Field(default_factory=dict)
    preflight_report_path: str | None = None
    validation_report_path: str | None = None
    ranking_report_path: str | None = None


class PatchPreflightIssue(BaseModel):
    op_index: int | None = None
    code: PatchFailureCode
    file: str | None = None
    message: str


class PatchPreflightReport(BaseModel):
    success: bool
    issues: list[PatchPreflightIssue] = Field(default_factory=list)


class StepExecutionTrace(BaseModel):
    step_id: str
    attempt: int
    status: Literal[
        "preflight_failed",
        "patch_applied",
        "validation_failed",
        "step_completed",
        "step_exhausted",
    ]
    issues: list[PatchPreflightIssue] = Field(default_factory=list)
    message: str | None = None
    candidate_id: str | None = None
    checkpoint_id: str | None = None
    score: float | None = None
    preflight_summary: dict[str, Any] | None = None
    validation_summary: dict[str, Any] | None = None
    artifacts: dict[str, str] = Field(default_factory=dict)


class StepRunResult(BaseModel):
    step_id: str
    outcome: Literal["step_completed", "attempts_exhausted"]
    validation_result: Literal["validation_passed", "validation_failed"]
    attempts_used: int
    selected_candidate_id: str | None = None
    touched_files: list[str] = Field(default_factory=list)
    patch_ops_by_file: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    trace_entries: list[StepExecutionTrace] = Field(default_factory=list)
    checkpoint_manifests: list[CheckpointManifest] = Field(default_factory=list)
    last_failure: dict[str, Any] | None = None


class StepProgress(BaseModel):
    total_steps: int
    completed_steps: int
    remaining_steps: int
    current_step_id: str | None = None


class ValidationResult(BaseModel):
    success: bool
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    duration_ms: int


class TaskRecord(BaseModel):
    task_id: str
    goal: str
    workspace_path: str
    status: TaskStatus = TaskStatus.QUEUED
    mode: Literal["inline", "file_edit", "project_edit", "autonomous"] = "project_edit"
    shadow_workspace_path: str | None = None
    plan: PlanDocument | None = None
    plan_markdown: str | None = None
    latest_patch: PatchDocument | None = None
    latest_patch_v2: PatchDocumentV2 | None = None
    selected_candidate_id: str | None = None
    promoted_at: datetime | None = None
    resume_of_task_id: str | None = None
    plan_approval_snapshot: TaskMilestoneSnapshot | None = None
    completed_step_ids: list[str] = Field(default_factory=list)
    modified_files: list[str] = Field(default_factory=list)
    # Normalized fingerprints of diagnostics present BEFORE this task's work began,
    # captured at the start of _execute_plan. Persisted so a `validate`-stage resume can
    # reuse the ORIGINAL baseline instead of re-collecting it on the already-mutated shadow.
    baseline_error_fingerprints: list[str] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    budget: TaskBudget = Field(default_factory=TaskBudget)
    usage: TaskUsage = Field(default_factory=TaskUsage)
    events: list[TaskEvent] = Field(default_factory=list)
    execution_trace: list[StepExecutionTrace] = Field(default_factory=list)
    checkpoints: list[CheckpointManifest] = Field(default_factory=list)
    execution_state: TaskExecutionState = Field(default_factory=TaskExecutionState)
    artifacts_root_path: str | None = None
    is_inline_change: bool = False
    step_review_auto_accept: bool = True
    chat_channel_id: str | None = None
    initial_explore_context: list[dict[str, object]] | None = None
    # The planner's own exploration is kept as the verbatim conversation it produced
    # (replaces the lossy planning_explore_context digest). A feedback round seeds the
    # planning loop with this and appends the feedback as the final turn, so the KV
    # prefix is reused instead of reprefilled.
    planning_conversation_history: list[dict[str, object]] | None = None
    # The exact initial_context the first planning loop used. Pinned and reused on
    # every feedback round so the payload prefix BEFORE conversation_history stays
    # byte-identical (recomputing retrieval could otherwise diverge it and defeat the cache).
    planning_initial_context: dict[str, object] | None = None
    shell_policy: ShellPolicy | None = None  # per-task override; engine resolves task.shell_policy or self._shell_policy
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("status", mode="before")
    @classmethod
    def _normalize_status(cls, value: object) -> TaskStatus:
        return normalize_task_status(value)


class TaskCreateRequest(BaseModel):
    goal: str
    workspace_path: str
    mode: Literal["inline", "file_edit", "project_edit", "autonomous"] = "project_edit"
    budget: TaskBudget = Field(default_factory=TaskBudget)
    initial_explore_context: list[dict[str, object]] | None = None
    # `None` = use AI_EDITOR_STEP_REVIEW_AUTO_ACCEPT env default (default: true).
    step_review_auto_accept: bool | None = None
    shell_policy: ShellPolicy | None = None  # per-task override of AI_EDITOR_SHELL_POLICY


class TaskCreateResponse(BaseModel):
    task_id: str


class TaskView(BaseModel):
    task_id: str
    goal: str
    status: TaskStatus
    modified_files: list[str]
    diagnostics: list[Diagnostic]
    plan_markdown: str | None = None
    resume_of_task_id: str | None = None


class PlanFeedbackRequest(BaseModel):
    feedback: str | None = None


class TaskResult(BaseModel):
    task_id: str
    goal: str
    status: TaskStatus
    plan: PlanDocument | None = None
    patch: PatchDocument | PatchCandidateV2 | None = None
    patch_candidates: list[PatchCandidateV2] = Field(default_factory=list)
    selected_candidate_id: str | None = None
    modified_files: list[str]
    diagnostics: list[Diagnostic]
    promoted_at: datetime | None = None
    shadow_workspace_path: str | None = None
    step_progress: StepProgress | None = None
    execution_trace: list[StepExecutionTrace] = Field(default_factory=list)
    artifacts_root_path: str | None = None
    plan_markdown: str | None = None
    resume_of_task_id: str | None = None


class RejectPatchRequest(BaseModel):
    reason: str


class TaskMilestoneSnapshot(BaseModel):
    """Full task state captured at a key lifecycle milestone for exact rollback."""
    captured_at: datetime
    task_state: dict[str, object]  # task.model_dump(mode="json") at capture time


class BudgetOverride(BaseModel):
    max_iterations: int | None = None
    max_tokens: int | None = None
    max_files_touched: int | None = None
    max_runtime_ms: int | None = None


class ResumeTaskRequest(BaseModel):
    stage: Literal["plan", "feedback", "execute", "validate"]
    budget_override: BudgetOverride | None = None


class ResumeTaskResponse(BaseModel):
    task_id: str
    resume_of_task_id: str


class TaskArtifactEntry(BaseModel):
    relative_path: str
    kind: Literal["checkpoint", "preflight", "validation", "ranking", "plan", "patch", "other"]
    step_id: str | None = None
    attempt: int | None = None
    candidate_id: str | None = None


class TaskArtifactsResponse(BaseModel):
    task_id: str
    artifacts_root_path: str | None = None
    entries: list[TaskArtifactEntry] = Field(default_factory=list)


@dataclass
class DiffEntry:
    path: str
    additions: int
    deletions: int
    temp_path: str
    # Capped unified diff text for in-card rendering (chat UI v2 Tier A; the
    # full diff stays available via the native editor diff against temp_path).
    unified_diff: str = ""


@dataclass
class InlineChangeResult:
    task_id: str
    diff_entries: list[DiffEntry]
    plan_document: dict[str, Any]
