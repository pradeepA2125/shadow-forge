from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, model_validator


class TaskStatus(StrEnum):
    QUEUED = "QUEUED"
    CONTEXT_READY = "CONTEXT_READY"
    PLANNED = "PLANNED"
    PATCHED = "PATCHED"
    VALIDATING = "VALIDATING"
    REPAIRING = "REPAIRING"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    PROMOTING = "PROMOTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


class TaskBudget(BaseModel):
    max_iterations: int = 6
    max_files_touched: int = 20
    max_tokens: int = 120_000
    max_runtime_ms: int = 20 * 60 * 1000


class TaskUsage(BaseModel):
    iterations: int = 0
    tokens_used: int = 0


class TaskEvent(BaseModel):
    at: datetime
    from_status: TaskStatus
    to_status: TaskStatus
    reason: str


class Diagnostic(BaseModel):
    source: str
    message: str
    level: Literal["error", "warning"]
    file: str | None = None
    line: int | None = None
    column: int | None = None


class PlanStep(BaseModel):
    id: str
    goal: str
    targets: list[str]
    risk: Literal["low", "med", "high"]


class PlanDocument(BaseModel):
    analysis: str
    steps: list[PlanStep]
    expected_files: list[str]
    stop_conditions: list[str]


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
    latest_patch: PatchDocument | None = None
    promoted_at: datetime | None = None
    completed_step_ids: list[str] = Field(default_factory=list)
    modified_files: list[str] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    budget: TaskBudget = Field(default_factory=TaskBudget)
    usage: TaskUsage = Field(default_factory=TaskUsage)
    events: list[TaskEvent] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TaskCreateRequest(BaseModel):
    goal: str
    workspace_path: str
    mode: Literal["inline", "file_edit", "project_edit", "autonomous"] = "project_edit"
    budget: TaskBudget = Field(default_factory=TaskBudget)


class TaskCreateResponse(BaseModel):
    task_id: str


class TaskView(BaseModel):
    task_id: str
    goal: str
    status: TaskStatus
    modified_files: list[str]
    diagnostics: list[Diagnostic]


class TaskResult(BaseModel):
    task_id: str
    goal: str
    status: TaskStatus
    plan: PlanDocument | None = None
    patch: PatchDocument | None = None
    modified_files: list[str]
    diagnostics: list[Diagnostic]
    promoted_at: datetime | None = None
    shadow_workspace_path: str | None = None


class RejectPatchRequest(BaseModel):
    reason: str
