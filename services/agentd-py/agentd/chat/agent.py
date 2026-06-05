from __future__ import annotations

import logging
from collections.abc import Callable
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
        "thought": {"type": "string", "description": "Reasoning before this action (1-2 sentences)"},
        "action": {"type": "string", "enum": ["tool_call", "done"]},
        "tool": {"type": "string",
                 "enum": ["search_code", "list_directory", "read_file", "search_semantic",
                          "query_graph"]},
        "args": {"type": "object"},
    },
    "required": ["thought", "action"],
}

_EXPLORE_PROMPT = """\
You are exploring a codebase to gather context before classifying a user request.
conversation_history contains recent turns — if the answer is already in history, emit action=done immediately without calling any tools.
Only use tools to find information that is not already covered by history or prior tool_results.

Strategy:
1. Use search_code or list_directory to locate the relevant files.
2. Once you find the file most likely to be changed, READ IT with read_file so the content is available for the change.
3. Use query_graph to map structure cheaply: query_graph(node="<file>") lists the files it connects to (depends-on vs used-by); query_graph(node="<file>:<Symbol>") lists what a symbol calls and who calls it (with line numbers). Use it to find the next file to read instead of grepping the whole repo.
4. Emit action=done when you have read the key file(s).

If the request involves modifying a specific file (e.g. adding an endpoint, changing a function), you MUST call read_file on that file before emitting done.

Tools: search_code (ripgrep), list_directory, read_file, search_semantic, query_graph (symbol-graph navigation).
Cap: you will be stopped after a fixed number of calls regardless.
Never modify files.

Always respond with valid JSON matching this schema:
  tool call:  {{"thought": "<1-2 sentence reasoning>", "action": "tool_call", "tool": "<name>", "args": {{...}}}}
  done:       {{"thought": "<why you have enough context>", "action": "done"}}
"""

_QA_PROMPT = """\
You are an expert code assistant. Answer the user's question about the codebase.
Use the workspace context below — files and search results already gathered.
Be concise and specific. Name files and functions explicitly.
"""

_RESUME_MESSAGE_WINDOW = 10  # max messages since task card to still consider it resumable
_RESUMABLE_STATUSES = {"FAILED", "ABORTED"}

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
        retrieval_client: Any | None = None,
    ) -> None:
        self._workspace_path = workspace_path
        self._transport = transport
        self._model = model
        self._store = thread_store
        self._orchestrator = orchestrator
        self._broadcaster = broadcaster
        self._max_explore_calls = max_explore_calls
        # Mirror the orchestrator's planning-registry wiring so the chat explore
        # phase has access to search_semantic when the retrieval client exposes a
        # loaded semantic index. Without this the chat path silently falls back
        # to "Error: semantic index not available" on every search_semantic call.
        self._registry = PlanningToolRegistry(
            real_path=Path(workspace_path),
            semantic_index=getattr(retrieval_client, "_semantic_index", None),
        )
        self._classifier = IntentClassifier(transport=transport, model=model)

    def _compute_blast_radius(self, files_examined: list[str]) -> list[str]:
        """Workspace-relative files structurally connected to the files the
        explore phase read — the cross-file ripple of a change to them. Fed to
        the intent classifier so it doesn't under-scope multi-file changes.
        Best-effort: returns [] when no snapshot or on any error."""
        ws_root = Path(self._workspace_path).resolve()
        rel_paths: list[str] = []
        for raw in files_examined:
            try:
                rel = str(Path(raw).resolve().relative_to(ws_root))
            except (ValueError, OSError):
                continue
            if rel not in rel_paths:
                rel_paths.append(rel)
        if not rel_paths:
            return []
        try:
            return self._registry.blast_radius(rel_paths)
        except Exception:  # noqa: BLE001 — never block classification
            logger.debug("[chat] blast_radius computation failed", exc_info=True)
            return []

    async def handle_message(self, thread_id: str, message: str, channel_id: str) -> None:
        """Process a chat message and broadcast all events to channel_id.

        Replaces the old async-generator form. The route background-tasks this
        coroutine and streams events from the broadcaster to the SSE client.
        """
        thread = self._store.get_thread(thread_id)
        if thread is None:
            raise ValueError(f"Thread {thread_id!r} not found")

        logger.info(
            "[chat] handle_message: thread=%s channel=%s msg_chars=%d",
            thread_id, channel_id, len(message),
        )

        user_msg = ChatMessage(role="user", content=message)
        self._store.append_message(thread_id, user_msg)

        # Auto-name thread from first user message
        if not any(m.role == "user" for m in thread.messages):
            title = message.strip().replace("\n", " ")[:50]
            self._store.update_title(thread_id, title)
            self._broadcaster.broadcast(channel_id, {
                "type": "thread_title_updated",
                "payload": {"thread_id": thread_id, "title": title},
            })

        history = [{"role": m.role, "content": m.content} for m in thread.messages]

        # Explore phase
        context: list[dict[str, Any]] = []
        files_examined: list[str] = []
        thinking_log: list[str] = []

        def _thinking_broadcast(chunk: str) -> None:
            self._broadcaster.broadcast(channel_id, {
                "type": "chat_agent_thinking_chunk",
                "payload": {"chunk": chunk},
            })
            thinking_log.append(chunk)

        def _broadcast_thinking(msg: str) -> None:
            self._broadcaster.broadcast(channel_id, {
                "type": "chat_agent_thinking",
                "payload": {"message": msg},
            })
            thinking_log.append(msg)

        _broadcast_thinking("Exploring workspace…")

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
                    on_thinking=_thinking_broadcast,
                )
            except Exception:
                logger.exception("Explore step failed — stopping early")
                break

            if step.get("action") == "done":
                logger.info("[chat] explore: agent emitted done after %d tool call(s)", len(context))
                break

            tool_name = step.get("tool", "")
            args = step.get("args") or {}
            thought = str(step.get("thought", ""))

            logger.info("[chat] explore tool_call #%d: %s %s", len(context) + 1, tool_name, args)
            self._broadcaster.broadcast(channel_id, {
                "type": "explore_tool_call",
                "payload": {"tool": tool_name, "args": args, "thought": thought},
            })
            path_arg = args.get("path", "") if isinstance(args, dict) else ""
            file_label = f" {path_arg.split('/')[-1]}" if path_arg else ""
            thinking_log.append(f"{tool_name}{file_label}" + (f" — {thought}" if thought else ""))

            try:
                tool_output = await self._registry.execute(tool_name, args)
                context.append({"tool": tool_name, "args": args, "result": tool_output.output, "is_error": tool_output.is_error})
                logger.info(
                    "[chat] explore tool_result: %s is_error=%s output_chars=%d",
                    tool_name, tool_output.is_error, len(tool_output.output),
                )
            except Exception as exc:
                logger.warning("[chat] explore tool error: %s — %s", tool_name, exc)
                context.append({"tool": tool_name, "args": args, "result": str(exc), "is_error": True})

            if tool_name in ("read_file", "list_directory"):
                path = args.get("path", "")
                if path and path not in files_examined:
                    files_examined.append(str(path))

        _broadcast_thinking("Classifying intent…")
        recent_task = await self._find_recent_task(thread.messages)
        # Compute the symbol-graph blast radius of the files the explore phase
        # read, and feed it to the classifier. Without this the classifier
        # judges scope purely from the handful of files explored and routinely
        # UNDER-scopes multi-file changes (e.g. a planner change that ripples
        # into domain/state_machine.py, api/routes.py, orchestrator/engine.py)
        # to small_change — sending them down the inline-edit path instead of
        # the planning loop. The blast radius surfaces that cross-file ripple.
        graph_blast_radius = self._compute_blast_radius(files_examined)
        logger.info(
            "[chat] graph blast radius: %d files connected to %d explored (%s%s)",
            len(graph_blast_radius), len(files_examined),
            ", ".join(p.split("/")[-1] for p in graph_blast_radius[:6]),
            "…" if len(graph_blast_radius) > 6 else "",
        )
        classification = await self._classifier.classify(
            message, context=context, history=history, recent_task=recent_task,
            graph_blast_radius=graph_blast_radius,
        )
        logger.info("[chat] intent=%s targets=%s rationale=%s",
                    classification.intent, classification.likely_targets,
                    (classification.rationale or "")[:120])
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
                logger.info("[chat] QA path: using inline answer from classifier (%d chars)", len(classification.answer))
                response_text = classification.answer
            else:
                logger.info("[chat] QA path: calling generate_text for answer")
                _broadcast_thinking("Composing answer…")
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
                        on_thinking=_thinking_broadcast,
                    )
                    logger.info("[chat] QA path: generate_text returned %d chars", len(response_text))
                except Exception:
                    logger.exception("Q&A LLM call failed")
                    response_text = "Sorry, I couldn't answer that. Please try again."

            self._store.append_message(
                thread_id, ChatMessage(role="agent", content=response_text, metadata={"thinking_log": thinking_log})
            )
            self._broadcaster.broadcast(channel_id, {
                "type": "chat_response",
                "payload": {"chunk": response_text},
            })
            self._broadcaster.broadcast(channel_id, {"type": "chat_done", "payload": {}})
            logger.info("[chat] QA path: response sent, chat_done broadcast")

        elif classification.intent == IntentType.SMALL_CHANGE:
            logger.info("[chat] small_change path: targets=%s", classification.likely_targets)
            if self._orchestrator is not None:
                _broadcast_thinking("Drafting change plan…")
                logger.info("[chat] small_change: drafting plan markdown")
                plan_md = await self._draft_plan_markdown(message, context, on_thinking=_thinking_broadcast)
                logger.info("[chat] small_change: plan_md %d chars — starting run_inline_change", len(plan_md))
                await self._orchestrator.run_inline_change(
                    thread_id=thread_id,
                    goal=message,
                    workspace_path=self._workspace_path,
                    plan_markdown=plan_md,
                    explore_context=context,
                    likely_targets=classification.likely_targets,
                    channel_id=channel_id,
                    store=self._store,
                    thinking_log=thinking_log,
                )
                logger.info("[chat] small_change: run_inline_change completed")
            else:
                logger.warning("[chat] small_change: no orchestrator configured")
                self._broadcaster.broadcast(channel_id, {
                    "type": "chat_response",
                    "payload": {"chunk": "[small_change: no orchestrator configured]"},
                })
                self._broadcaster.broadcast(channel_id, {"type": "chat_done", "payload": {}})

        elif classification.intent == IntentType.RESUME:
            logger.info("[chat] resume path: recent_task=%s", recent_task)
            if recent_task and self._orchestrator is not None:
                _broadcast_thinking("Resuming task…")
                try:
                    child_task_id = await self._orchestrator.resume_from_execute(
                        str(recent_task["task_id"]),
                        chat_channel_id=channel_id,
                    )
                    logger.info("[chat] resume: child task created task_id=%s — streaming to %s", child_task_id, channel_id)
                    self._store.append_message(
                        thread_id,
                        ChatMessage(role="agent", content=child_task_id, type="task_card", task_id=child_task_id, metadata={}),
                    )
                    self._broadcaster.broadcast(channel_id, {
                        "type": "task_card",
                        "payload": {"task_id": child_task_id},
                    })
                    # chat_done is broadcast by _resume_with_channel when execution completes.
                    return
                except Exception:
                    logger.exception("[chat] resume_from_execute failed")
                    self._broadcaster.broadcast(channel_id, {
                        "type": "chat_response",
                        "payload": {"chunk": "Failed to resume the task — it may no longer be resumable (shadow workspace gone or no plan). Start a new task instead."},
                    })
            else:
                self._broadcaster.broadcast(channel_id, {
                    "type": "chat_response",
                    "payload": {"chunk": "There's no recent failed task to resume in this conversation."},
                })
            self._broadcaster.broadcast(channel_id, {"type": "chat_done", "payload": {}})

        elif classification.intent == IntentType.CLARIFY:
            question = classification.clarify_question or "Could you clarify what you'd like to do?"
            logger.info("[chat] clarify path: question=%s", question[:80])
            self._store.append_message(
                thread_id,
                ChatMessage(role="agent", content=question, metadata={"thinking_log": thinking_log}),
            )
            self._broadcaster.broadcast(channel_id, {
                "type": "chat_response",
                "payload": {"chunk": question},
            })
            self._broadcaster.broadcast(channel_id, {"type": "chat_done", "payload": {}})

        else:  # large_change
            logger.info("[chat] large_change path: targets=%s", classification.likely_targets)
            if self._orchestrator is not None:
                _broadcast_thinking("Creating task…")
                logger.info("[chat] large_change: calling create_task_from_chat")
                task_id = await self._orchestrator.create_task_from_chat(
                    thread_id=thread_id,
                    goal=message,
                    workspace_path=self._workspace_path,
                    explore_context=context,
                    store=self._store,
                )
                logger.info("[chat] large_change: task created task_id=%s — broadcasting task_card", task_id)
                self._store.append_message(
                    thread_id,
                    ChatMessage(role="agent", content=task_id, type="task_card", task_id=task_id, metadata={}),
                )
                self._broadcaster.broadcast(channel_id, {
                    "type": "task_card",
                    "payload": {"task_id": task_id},
                })
                # Keep SSE alive while planning runs; engine.py will broadcast
                # task_status_changed to this channel when AWAITING_PLAN_APPROVAL.
                # We also write plan_card to DB so it survives a reload.
                _broadcast_thinking("Planning… (this may take a minute)")
                plan_task = await self._orchestrator.await_plan_ready(task_id)
                if plan_task is not None and plan_task.plan_markdown and plan_task.status.value == "AWAITING_PLAN_APPROVAL":
                    self._store.append_message(
                        thread_id,
                        ChatMessage(
                            role="agent",
                            content=plan_task.plan_markdown,
                            type="plan_card",
                            task_id=task_id,
                            metadata={"taskId": task_id, "plan_markdown": plan_task.plan_markdown},
                        ),
                    )
            else:
                logger.warning("[chat] large_change: no orchestrator configured")
                self._broadcaster.broadcast(channel_id, {
                    "type": "chat_response",
                    "payload": {"chunk": "[large_change: no orchestrator configured]"},
                })
            self._broadcaster.broadcast(channel_id, {"type": "chat_done", "payload": {}})

    async def _find_recent_task(self, messages: list[ChatMessage]) -> dict[str, object] | None:
        """Return resumable task context if a recent failed task exists in the thread."""
        if self._orchestrator is None:
            return None
        for offset, msg in enumerate(reversed(messages)):
            if offset >= _RESUME_MESSAGE_WINDOW:
                break
            if msg.task_id and msg.type in ("task_card", "plan_card"):
                try:
                    task = await self._orchestrator.get_task(msg.task_id)
                    if task.status.value in _RESUMABLE_STATUSES and task.plan:
                        return {
                            "task_id": msg.task_id,
                            "status": task.status.value,
                            "goal": task.goal,
                            "messages_since": offset,
                        }
                except Exception:
                    return None
        return None

    async def _draft_plan_markdown(
        self,
        goal: str,
        explore_context: list[dict[str, Any]],
        on_thinking: Callable[[str], None] | None = None,
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
                on_thinking=on_thinking,
            )
        except Exception:
            logger.exception("_draft_plan_markdown failed — using goal as fallback")
            return f"- {goal}"
