from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from agentd.chat.classifier import IntentClassifier
from agentd.chat.models import ChatMessage, IntentType
from agentd.chat.storage import ChatThreadStore
from agentd.orchestrator.broadcaster import EventBroadcaster
from agentd.planning.registry import PlanningToolRegistry

logger = logging.getLogger(__name__)

_EXPLORE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["tool_call", "done"]},
        "tool": {"type": "string",
                 "enum": ["search_code", "list_directory", "read_file", "search_semantic"]},
        "args": {"type": "object"},
    },
    "required": ["action"],
}

_EXPLORE_PROMPT = """\
You are exploring a codebase to gather context before classifying a user request.
conversation_history contains recent turns — if the answer is already in history, emit action=done immediately without calling any tools.
Only use tools to find information that is not already covered by history or prior tool_results.

Strategy:
1. Use search_code or list_directory to locate the relevant files.
2. Once you find the file most likely to be changed, READ IT with read_file so the content is available for the change.
3. Emit action=done when you have read the key file(s).

If the request involves modifying a specific file (e.g. adding an endpoint, changing a function), you MUST call read_file on that file before emitting done.

Tools: search_code (ripgrep), list_directory, read_file, search_semantic.
Cap: you will be stopped after a fixed number of calls regardless.
Never modify files.
"""

_QA_PROMPT = """\
You are an expert code assistant. Answer the user's question about the codebase.
Use the workspace context below — files and search results already gathered.
Be concise and specific. Name files and functions explicitly.
"""

_DRAFT_PLAN_PROMPT = """\
You are drafting a brief implementation plan for a small code change.
Based on the explored context and the user's request, write 2-4 bullet points
describing exactly what files will be changed and what each change will do.
Be concrete — name specific files, functions, or lines. No fluff.
Output plain markdown (bullet list). Maximum 150 words.
"""


class ChatAgent:
    def __init__(
        self,
        *,
        workspace_path: str,
        transport: Any,
        model: str,
        thread_store: ChatThreadStore,
        orchestrator: Any | None,
        broadcaster: EventBroadcaster,
        max_explore_calls: int = 5,
    ) -> None:
        self._workspace_path = workspace_path
        self._transport = transport
        self._model = model
        self._store = thread_store
        self._orchestrator = orchestrator
        self._broadcaster = broadcaster
        self._max_explore_calls = max_explore_calls
        self._registry = PlanningToolRegistry(real_path=Path(workspace_path))
        self._classifier = IntentClassifier(transport=transport, model=model)

    async def handle_message(self, thread_id: str, message: str, channel_id: str) -> None:
        """Process a chat message and broadcast all events to channel_id.

        Replaces the old async-generator form. The route background-tasks this
        coroutine and streams events from the broadcaster to the SSE client.
        """
        thread = self._store.get_thread(thread_id)
        if thread is None:
            raise ValueError(f"Thread {thread_id!r} not found")

        user_msg = ChatMessage(role="user", content=message)
        self._store.append_message(thread_id, user_msg)

        history = [{"role": m.role, "content": m.content} for m in thread.messages]

        # Explore phase
        context: list[dict[str, Any]] = []
        files_examined: list[str] = []

        self._broadcaster.broadcast(channel_id, {
            "type": "chat_agent_thinking",
            "payload": {"message": "Exploring workspace…"},
        })

        for _ in range(self._max_explore_calls):
            try:
                step = await self._transport.generate_json(
                    model=self._model,
                    schema_name="explore_step",
                    schema=_EXPLORE_SCHEMA,
                    system_instructions=_EXPLORE_PROMPT,
                    user_payload={
                        "message": message,
                        "workspace_path": self._workspace_path,
                        "conversation_history": history[-6:],
                        "tool_results": context,
                    },
                )
            except Exception:
                logger.exception("Explore step failed — stopping early")
                break

            if step.get("action") == "done":
                break

            tool_name = step.get("tool", "")
            args = step.get("args") or {}

            self._broadcaster.broadcast(channel_id, {
                "type": "explore_tool_call",
                "payload": {"tool": tool_name, "args": args},
            })

            try:
                tool_output = await self._registry.execute(tool_name, args)
                context.append({"tool": tool_name, "args": args, "result": tool_output.output, "is_error": tool_output.is_error})
            except Exception as exc:
                context.append({"tool": tool_name, "args": args, "result": str(exc), "is_error": True})

            if tool_name in ("read_file", "list_directory"):
                path = args.get("path", "")
                if path and path not in files_examined:
                    files_examined.append(str(path))

        classification = await self._classifier.classify(
            message, context=context, history=history
        )
        self._broadcaster.broadcast(channel_id, {
            "type": "intent_classified",
            "payload": {
                "intent": classification.intent,
                "rationale": classification.rationale,
                "likely_targets": classification.likely_targets,
                "files_examined": files_examined,
            },
        })

        if classification.intent == IntentType.QA:
            if classification.answer:
                response_text = classification.answer
            else:
                try:
                    response_text = await self._transport.generate_text(
                        model=self._model,
                        system_instructions=_QA_PROMPT,
                        user_payload={
                            "workspace_path": self._workspace_path,
                            "conversation_history": history[-10:],
                            "workspace_context": context,
                            "question": message,
                        },
                    )
                except Exception:
                    logger.exception("Q&A LLM call failed")
                    response_text = "Sorry, I couldn't answer that. Please try again."

            self._store.append_message(
                thread_id, ChatMessage(role="agent", content=response_text)
            )
            self._broadcaster.broadcast(channel_id, {
                "type": "chat_response",
                "payload": {"chunk": response_text},
            })
            self._broadcaster.broadcast(channel_id, {"type": "chat_done", "payload": {}})

        elif classification.intent == IntentType.SMALL_CHANGE:
            if self._orchestrator is not None:
                plan_md = await self._draft_plan_markdown(message, context)
                await self._orchestrator.run_inline_change(
                    thread_id=thread_id,
                    goal=message,
                    workspace_path=self._workspace_path,
                    plan_markdown=plan_md,
                    explore_context=context,
                    likely_targets=classification.likely_targets,
                    channel_id=channel_id,
                    store=self._store,
                )
            else:
                self._broadcaster.broadcast(channel_id, {
                    "type": "chat_response",
                    "payload": {"chunk": "[small_change: no orchestrator configured]"},
                })
                self._broadcaster.broadcast(channel_id, {"type": "chat_done", "payload": {}})

        else:  # large_change
            if self._orchestrator is not None:
                task_id = await self._orchestrator.create_task_from_chat(
                    thread_id=thread_id,
                    goal=message,
                    workspace_path=self._workspace_path,
                    explore_context=context,
                    store=self._store,
                )
                self._broadcaster.broadcast(channel_id, {
                    "type": "task_card",
                    "payload": {"task_id": task_id},
                })
            else:
                self._broadcaster.broadcast(channel_id, {
                    "type": "chat_response",
                    "payload": {"chunk": "[large_change: no orchestrator configured]"},
                })
            self._broadcaster.broadcast(channel_id, {"type": "chat_done", "payload": {}})

    async def _draft_plan_markdown(
        self,
        goal: str,
        explore_context: list[dict[str, Any]],
    ) -> str:
        """Generate a short bullet-list plan for display before inline change runs."""
        try:
            return await self._transport.generate_text(
                model=self._model,
                system_instructions=_DRAFT_PLAN_PROMPT,
                user_payload={
                    "goal": goal,
                    "workspace_path": self._workspace_path,
                    "explore_context": explore_context,
                },
            )
        except Exception:
            logger.exception("_draft_plan_markdown failed — using goal as fallback")
            return f"- {goal}"
