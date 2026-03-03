from __future__ import annotations

import asyncio

from agentd.domain.models import TaskRecord
from agentd.storage.base import TaskStore


class InMemoryTaskStore(TaskStore):
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._tasks: dict[str, TaskRecord] = {}

    async def create(self, task: TaskRecord) -> TaskRecord:
        async with self._lock:
            if task.task_id in self._tasks:
                msg = f"Task already exists: {task.task_id}"
                raise ValueError(msg)
            self._tasks[task.task_id] = task
            return task

    async def save(self, task: TaskRecord) -> TaskRecord:
        async with self._lock:
            if task.task_id not in self._tasks:
                msg = f"Task not found: {task.task_id}"
                raise KeyError(msg)
            self._tasks[task.task_id] = task
            return task

    async def get(self, task_id: str) -> TaskRecord:
        async with self._lock:
            try:
                return self._tasks[task_id]
            except KeyError as exc:
                msg = f"Task not found: {task_id}"
                raise KeyError(msg) from exc
