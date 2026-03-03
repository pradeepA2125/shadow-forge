from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from agentd.domain.models import TaskRecord


class SQLiteTaskStore:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._conn = sqlite3.connect(self._database_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
              task_id TEXT PRIMARY KEY,
              goal TEXT NOT NULL,
              status TEXT NOT NULL,
              workspace_path TEXT NOT NULL,
              shadow_workspace_path TEXT,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS task_events (
              task_id TEXT NOT NULL,
              event_index INTEGER NOT NULL,
              at TEXT NOT NULL,
              from_status TEXT NOT NULL,
              to_status TEXT NOT NULL,
              reason TEXT NOT NULL,
              PRIMARY KEY (task_id, event_index),
              FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_task_events_task_id
            ON task_events(task_id);
            """
        )
        self._conn.commit()

    async def create(self, task: TaskRecord) -> TaskRecord:
        async with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO tasks (
                      task_id,
                      goal,
                      status,
                      workspace_path,
                      shadow_workspace_path,
                      payload_json,
                      created_at,
                      updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task.task_id,
                        task.goal,
                        task.status,
                        task.workspace_path,
                        task.shadow_workspace_path,
                        task.model_dump_json(),
                        task.created_at.isoformat(),
                        task.updated_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                msg = f"Task already exists: {task.task_id}"
                raise ValueError(msg) from exc

            self._replace_task_events(task)
            self._conn.commit()
            return task

    async def save(self, task: TaskRecord) -> TaskRecord:
        async with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE tasks
                SET goal = ?,
                    status = ?,
                    workspace_path = ?,
                    shadow_workspace_path = ?,
                    payload_json = ?,
                    created_at = ?,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (
                    task.goal,
                    task.status,
                    task.workspace_path,
                    task.shadow_workspace_path,
                    task.model_dump_json(),
                    task.created_at.isoformat(),
                    task.updated_at.isoformat(),
                    task.task_id,
                ),
            )

            if cursor.rowcount == 0:
                msg = f"Task not found: {task.task_id}"
                raise KeyError(msg)

            self._replace_task_events(task)
            self._conn.commit()
            return task

    async def get(self, task_id: str) -> TaskRecord:
        async with self._lock:
            cursor = self._conn.execute(
                "SELECT payload_json FROM tasks WHERE task_id = ?",
                (task_id,),
            )
            row = cursor.fetchone()
            if row is None:
                msg = f"Task not found: {task_id}"
                raise KeyError(msg)

            payload_json = str(row[0])
            return TaskRecord.model_validate_json(payload_json)

    def _replace_task_events(self, task: TaskRecord) -> None:
        self._conn.execute("DELETE FROM task_events WHERE task_id = ?", (task.task_id,))
        for index, event in enumerate(task.events):
            self._conn.execute(
                """
                INSERT INTO task_events (
                  task_id,
                  event_index,
                  at,
                  from_status,
                  to_status,
                  reason
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    index,
                    event.at.isoformat(),
                    event.from_status,
                    event.to_status,
                    event.reason,
                ),
            )
