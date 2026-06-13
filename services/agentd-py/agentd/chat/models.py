from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from agentd.domain.models import FailureSummary, RunSummary, TaskNarrative


class IntentType(StrEnum):
    QA = "qa"
    SMALL_CHANGE = "small_change"
    LARGE_CHANGE = "large_change"
    RESUME = "resume"
    CLARIFY = "clarify"


class IntentClassification(BaseModel):
    intent: IntentType
    rationale: str
    files_examined: list[str] = Field(default_factory=list)
    likely_targets: list[str] = Field(default_factory=list)
    answer: str | None = None
    clarify_question: str | None = None


class ChatMessage(BaseModel):
    role: Literal["user", "agent"]
    content: str
    type: Literal["text", "plan_card", "diff_card", "diff_summary", "task_card", "scope_card"] = "text"
    task_id: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChatThread(BaseModel):
    thread_id: str
    workspace_path: str
    title: str = "New Chat"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    messages: list[ChatMessage] = Field(default_factory=list)
    touched_files: list[str] = Field(default_factory=list)
    # The thread's current task. Set when a task is created or resumed from the
    # thread; resume updates it to the child id. The durable thread->task link
    # that lets the UI follow task-id churn without losing the gate/plan view.
    active_task_id: str | None = None


class ChatEvent(BaseModel):
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class PendingGate(BaseModel):
    """The one gate a thread's current task is waiting on, if any."""
    kind: Literal["command", "step", "scope", "validation"]
    payload: dict[str, Any] = Field(default_factory=dict)


class ThreadLiveState(BaseModel):
    """Everything the chat UI needs to render a thread's current actionable state.

    Resolved from the thread's active task: its status, the single active gate
    (if waiting), and the current actionable plan (only at AWAITING_PLAN_APPROVAL).
    The UI renders from this (state-driven), so reloads and resume task-id churn
    self-heal on the next poll.
    """
    active_task_id: str | None = None
    status: str | None = None
    pending_gate: PendingGate | None = None
    plan: dict[str, Any] | None = None
    # Durable lifecycle telemetry (Tier B): failure_summary only at FAILED/ABORTED,
    # run_summary whenever present. Lets the Error/Review cards render from state on reload.
    failure_summary: FailureSummary | None = None
    run_summary: RunSummary | None = None
    task_narrative: TaskNarrative | None = None
