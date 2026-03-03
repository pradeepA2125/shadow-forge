from __future__ import annotations

from typing import Protocol

from agentd.domain.models import TaskRecord


class TaskStore(Protocol):
    async def create(self, task: TaskRecord) -> TaskRecord: ...

    async def save(self, task: TaskRecord) -> TaskRecord: ...

    async def get(self, task_id: str) -> TaskRecord: ...
