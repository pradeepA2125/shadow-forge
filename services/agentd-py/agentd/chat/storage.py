from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agentd.chat.models import ChatMessage, ChatThread, PendingGate


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
        # active_task_id was added after the table shipped; ALTER existing DBs.
        # IF NOT EXISTS on the table create above won't add a column to pre-existing rows.
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(chat_threads)")}
        if "active_task_id" not in existing:
            self._conn.execute("ALTER TABLE chat_threads ADD COLUMN active_task_id TEXT")
        # Controller-turn gate (mode/edit), added after the table shipped.
        if "controller_gate_json" not in existing:
            self._conn.execute("ALTER TABLE chat_threads ADD COLUMN controller_gate_json TEXT")
        # Durable controller conversation history (seed_history substrate), added later.
        if "controller_history_json" not in existing:
            self._conn.execute("ALTER TABLE chat_threads ADD COLUMN controller_history_json TEXT")
        # Pinned retrieval seed (cache-prefix head), added later.
        if "controller_seed_json" not in existing:
            self._conn.execute("ALTER TABLE chat_threads ADD COLUMN controller_seed_json TEXT")
        self._conn.commit()

    @staticmethod
    def _history_from_row(row: sqlite3.Row) -> list[dict] | None:
        raw = row["controller_history_json"]
        return json.loads(raw) if raw else None

    @staticmethod
    def _seed_from_row(row: sqlite3.Row) -> dict | None:
        raw = row["controller_seed_json"]
        return json.loads(raw) if raw else None

    @staticmethod
    def _gate_from_row(row: sqlite3.Row) -> PendingGate | None:
        raw = row["controller_gate_json"]
        return PendingGate.model_validate_json(raw) if raw else None

    def create_thread(self, workspace_path: str, title: str = "New Chat") -> ChatThread:
        thread_id = f"chat-{uuid.uuid4().hex[:12]}"
        created_at = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT INTO chat_threads (thread_id, workspace_path, title, created_at) VALUES (?, ?, ?, ?)",
            (thread_id, workspace_path, title, created_at),
        )
        self._conn.commit()
        return ChatThread(
            thread_id=thread_id,
            workspace_path=workspace_path,
            title=title,
            created_at=datetime.fromisoformat(created_at),
        )

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
                created_at=datetime.fromisoformat(row["created_at"]),
                messages=[ChatMessage.model_validate(m) for m in json.loads(row["messages_json"])],
                touched_files=json.loads(row["touched_files_json"]),
                active_task_id=row["active_task_id"],
                pending_controller_gate=self._gate_from_row(row),
                controller_conversation_history=self._history_from_row(row),
                controller_retrieval_seed=self._seed_from_row(row),
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
            created_at=datetime.fromisoformat(row["created_at"]),
            messages=[ChatMessage.model_validate(m) for m in json.loads(row["messages_json"])],
            touched_files=json.loads(row["touched_files_json"]),
            active_task_id=row["active_task_id"],
            pending_controller_gate=self._gate_from_row(row),
            controller_conversation_history=self._history_from_row(row),
            controller_retrieval_seed=self._seed_from_row(row),
        )

    def set_controller_seed(self, thread_id: str, seed: dict | None) -> None:
        """Pin the thread's retrieval seed (frozen cache-prefix head). Written once on
        first compute; replayed verbatim thereafter so the KV prefix survives restart."""
        raw = json.dumps(seed) if seed else None
        self._conn.execute(
            "UPDATE chat_threads SET controller_seed_json = ? WHERE thread_id = ?",
            (raw, thread_id),
        )
        self._conn.commit()

    def set_controller_history(
        self, thread_id: str, history: list[dict] | None
    ) -> None:
        """Persist the controller loop's verbatim turn history. Mirrors
        set_controller_gate: an in-place durable update the next turn rehydrates
        seed_history from (parity with TaskRecord.planning_conversation_history)."""
        raw = json.dumps(history) if history else None
        self._conn.execute(
            "UPDATE chat_threads SET controller_history_json = ? WHERE thread_id = ?",
            (raw, thread_id),
        )
        self._conn.commit()

    def set_controller_gate(self, thread_id: str, gate: PendingGate | None) -> None:
        """Set (or clear, with None) the thread's controller-turn gate. Mirrors
        set_active_task: an in-place durable update the /live poll renders from."""
        raw = gate.model_dump_json() if gate is not None else None
        self._conn.execute(
            "UPDATE chat_threads SET controller_gate_json = ? WHERE thread_id = ?",
            (raw, thread_id),
        )
        self._conn.commit()

    def set_active_task(self, thread_id: str, task_id: str) -> None:
        """Point the thread at its current task. Resume churns the id (parent→child);
        this is the durable link the UI follows so gate/plan views survive that churn."""
        self._conn.execute(
            "UPDATE chat_threads SET active_task_id = ? WHERE thread_id = ?",
            (task_id, thread_id),
        )
        self._conn.commit()

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

    def append_plan_card(self, thread_id: str, task_id: str, plan_markdown: str) -> bool:
        """Append a plan version to a task's transcript, building a version history.

        Each feedback round produces a new plan; appending (rather than replacing)
        preserves the evolution — old plan, then ``↻ feedback`` breadcrumb, then the
        new plan. To honour "no duplicate", a write identical to the task's CURRENT
        latest plan_card is skipped (collapses the double-writer / re-presentation).
        Returns True only when a card was actually appended — lets the caller
        broadcast the live append exactly once per version.
        """
        row = self._conn.execute(
            "SELECT messages_json FROM chat_threads WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        if row is None:
            return False
        messages: list[dict] = json.loads(row["messages_json"])
        latest = None
        for msg in messages:
            if msg.get("type") == "plan_card" and msg.get("task_id") == task_id:
                latest = msg
        if latest is not None and latest.get("content") == plan_markdown:
            return False  # identical to the current latest version — no duplicate
        messages.append(
            ChatMessage(
                role="agent", content=plan_markdown, type="plan_card", task_id=task_id,
                metadata={"taskId": task_id, "plan_markdown": plan_markdown},
            ).model_dump(mode="json")
        )
        self._conn.execute(
            "UPDATE chat_threads SET messages_json = ? WHERE thread_id = ?",
            (json.dumps(messages), thread_id),
        )
        self._conn.commit()
        return True

    def update_title(self, thread_id: str, title: str) -> None:
        self._conn.execute(
            "UPDATE chat_threads SET title = ? WHERE thread_id = ?", (title, thread_id)
        )
        self._conn.commit()

    def resolve_diff_card(self, inline_task_id: str, resolution: str) -> None:
        """Mark a diff_card message as resolved (applied/discarded) across all threads."""
        rows = self._conn.execute(
            "SELECT thread_id, messages_json FROM chat_threads"
        ).fetchall()
        for row in rows:
            messages: list[dict] = json.loads(row["messages_json"])
            changed = False
            for msg in messages:
                if msg.get("type") == "diff_card" and msg.get("task_id") == inline_task_id:
                    msg.setdefault("metadata", {})["resolved"] = resolution
                    changed = True
            if changed:
                self._conn.execute(
                    "UPDATE chat_threads SET messages_json = ? WHERE thread_id = ?",
                    (json.dumps(messages), row["thread_id"]),
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
