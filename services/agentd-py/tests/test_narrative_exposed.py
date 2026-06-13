from agentd.chat.live_state import resolve_live_state
from agentd.domain.models import TaskBudget, TaskNarrative, TaskRecord, TaskStatus


def test_live_state_surfaces_task_narrative():
    task = TaskRecord(task_id="t", goal="g", workspace_path="/w", budget=TaskBudget(),
                      status=TaskStatus.READY_FOR_REVIEW,
                      task_narrative=TaskNarrative(outcome="succeeded", headline="Did X", points=["a"]))
    live = resolve_live_state(task.task_id, lambda _id: task)
    assert live.task_narrative is not None
    assert live.task_narrative.headline == "Did X"
