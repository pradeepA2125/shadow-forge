from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agentd.domain.models import TaskRecord, TaskStatus
from agentd.domain.state_machine import transition
from agentd.storage.sqlite_store import SQLiteTaskStore


@pytest.mark.asyncio
async def test_sqlite_store_persists_tasks_and_events(tmp_path: Path) -> None:
    database_path = tmp_path / "agentd.sqlite3"
    store = SQLiteTaskStore(database_path=database_path)

    task = TaskRecord(task_id="task-1", goal="goal", workspace_path=str(tmp_path))
    await store.create(task)

    loaded = await store.get("task-1")
    assert loaded.task_id == "task-1"
    assert loaded.status == TaskStatus.QUEUED

    task = transition(task, TaskStatus.CONTEXT_READY, "context assembled")
    await store.save(task)

    reloaded = await store.get("task-1")
    assert reloaded.status == TaskStatus.CONTEXT_READY
    assert len(reloaded.events) == 1

    conn = sqlite3.connect(database_path)
    try:
        row = conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ?", ("task-1",)).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert int(row[0]) == 1
