from agentd.domain.models import TaskRecord, TaskStatus
from agentd.domain.state_machine import can_transition


def test_valid_transition_path() -> None:
    task = TaskRecord(task_id="t1", goal="goal", workspace_path=".")
    assert task.status == TaskStatus.QUEUED
    assert can_transition(TaskStatus.QUEUED, TaskStatus.CONTEXT_READY)
    assert can_transition(TaskStatus.READY_FOR_REVIEW, TaskStatus.PROMOTING)
