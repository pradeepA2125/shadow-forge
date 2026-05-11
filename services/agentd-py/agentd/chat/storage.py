from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agentd.chat.models import ChatMessage, ChatThread


class ChatThreadStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS chat_threads (
                thread_id TEXT PRIMARY KEY,
                workspace_path TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT 'New Chat',
                created_at TEXT NOT NULL,
                messages_json TEXT NOT NULL DEFAULT '[]',
                touched_files_json TEXT NOT NULL DEFAULT '[]'
            );
        """)
        self._conn.commit()

    def create_thread(self, workspace_path: str, title: str = "New Chat") -> ChatThread:
        thread_id = f"chat-{uuid.uuid4().hex[:12]}"
        created_at = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT INTO chat_threads (thread_id, workspace_path, title, created_at) VALUES (?, ?, ?, ?)",
            (thread_id, workspace_path, title, created_at),
        )
        self._conn.commit()
        return ChatThread(thread_id=thread_id, workspace_path=workspace_path, title=title)

    def list_threads(self, workspace_path: str) -> list[ChatThread]:
        rows = self._conn.execute(
            "SELECT * FROM chat_threads WHERE workspace_path = ? ORDER BY created_at DESC",
            (workspace_path,),
        ).fetchall()
        return [
            ChatThread(
                thread_id=row["thread_id"],
                workspace_path=row["workspace_path"],
                title=row["title"],
                messages=[ChatMessage.model_validate(m) for m in json.loads(row["messages_json"])],
                touched_files=json.loads(row["touched_files_json"]),
            )
            for row in rows
        ]

    def get_thread(self, thread_id: str) -> ChatThread | None:
        row = self._conn.execute(
            "SELECT * FROM chat_threads WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        if row is None:
            return None
        return ChatThread(
            thread_id=row["thread_id"],
            workspace_path=row["workspace_path"],
            title=row["title"],
            messages=[ChatMessage.model_validate(m) for m in json.loads(row["messages_json"])],
            touched_files=json.loads(row["touched_files_json"]),
        )

    def append_message(self, thread_id: str, message: ChatMessage) -> None:
        row = self._conn.execute(
            "SELECT messages_json FROM chat_threads WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        messages = json.loads(row["messages_json"])
        messages.append(message.model_dump(mode="json"))
        self._conn.execute(
            "UPDATE chat_threads SET messages_json = ? WHERE thread_id = ?",
            (json.dumps(messages), thread_id),
        )
        self._conn.commit()

    def update_title(self, thread_id: str, title: str) -> None:
        self._conn.execute(
            "UPDATE chat_threads SET title = ? WHERE thread_id = ?", (title, thread_id)
        )
        self._conn.commit()

    def add_touched_file(self, thread_id: str, file_path: str) -> None:
        row = self._conn.execute(
            "SELECT touched_files_json FROM chat_threads WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        files: list[str] = json.loads(row["touched_files_json"])
        if file_path not in files:
            files.append(file_path)
        self._conn.execute(
            "UPDATE chat_threads SET touched_files_json = ? WHERE thread_id = ?",
            (json.dumps(files), thread_id),
        )
        self._conn.commit()
