"""The /message + /mode-decision routes detach the turn (create_task, subscribe-
relay) so a client disconnect does NOT cancel the work, and a concurrent /message
returns 409. Driven through the real FastAPI router with a ChatController handler."""
import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agentd.api.routes import build_router
from agentd.chat.controller import ChatController
from agentd.chat.storage import ChatThreadStore
from agentd.orchestrator.broadcaster import EventBroadcaster
from agentd.orchestrator.scripted_engine import ScriptedReasoningEngine


class _SlowController(ChatController):
    """A controller whose turn blocks until released — lets the test hold the turn
    in flight while issuing a second request (the 409 path)."""
    def __init__(self, *args, gate: asyncio.Event, **kwargs):
        super().__init__(*args, **kwargs)
        self._gate = gate

    async def handle_message(self, thread_id, message, channel_id, step_review=None):
        await self._gate.wait()
        self._broadcaster.broadcast(channel_id, {"type": "chat_done", "payload": {}})


@pytest.fixture
def _app(tmp_path):
    # Minimal store/orchestrator/workspace stubs: the chat routes only touch the
    # chat handler; task routes are unused here.
    from agentd.storage.in_memory import InMemoryTaskStore

    store = InMemoryTaskStore()
    chat_store = ChatThreadStore(tmp_path / "chat.sqlite3")
    gate = asyncio.Event()
    handler = _SlowController(
        workspace_path=str(tmp_path),
        reasoning_engine=ScriptedReasoningEngine(None, []),
        thread_store=chat_store, orchestrator=None,
        broadcaster=EventBroadcaster(), retrieval_client=None, gate=gate)

    class _Orch:  # build_router reads .broadcaster + ._running_tasks for task routes
        broadcaster = handler._broadcaster
        _running_tasks: set = set()

    app = FastAPI()
    # build_router already self-prefixes "/v1" (see main.py: include_router with no
    # extra prefix), so mount it bare — routes live at /v1/chat/...
    app.include_router(build_router(store, _Orch(), None, None, handler))
    return app, chat_store, gate, handler


async def _consume_stream(client, url: str, json_body: dict) -> None:
    """Drive a streaming POST to completion in a background task.

    httpx's ASGITransport BUFFERS the whole response — `client.stream(...)` does not
    return until the ASGI body generator finishes. A turn parked on a gate would never
    finish, so we cannot hold the stream open inline and fire a second request. Instead
    we consume the stream in a background task: the route handler (and its `launch_turn`)
    still runs synchronously when the app is invoked, registering the turn in
    `_active_turns` before the body blocks — which is all the in-flight guard needs."""
    async with client.stream("POST", url, json=json_body) as resp:
        async for _line in resp.aiter_lines():
            pass


@pytest.mark.asyncio
async def test_second_message_while_active_returns_409(_app):
    app, chat_store, gate, handler = _app
    thread = chat_store.create_thread("/ws")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        url = f"/v1/chat/threads/{thread.thread_id}/message"
        # First message: consume in the background so the route registers the turn but
        # the gate keeps it in flight (the background consumer blocks on the buffered body).
        bg = asyncio.create_task(_consume_stream(client, url, {"content": "hi"}))
        await asyncio.sleep(0.05)  # let the detached turn register in _active_turns
        assert thread.thread_id in handler._active_turns
        resp2 = await client.post(url, json={"content": "again"})
        assert resp2.status_code == 409
        gate.set()  # release so the first stream completes cleanly
        await bg


@pytest.mark.asyncio
async def test_mode_decision_registers_detached_turn(_app):
    app, chat_store, gate, handler = _app
    thread = chat_store.create_thread("/ws")

    # Override resolve_mode to block on the same gate so we can observe registration.
    async def _slow_resolve(thread_id, mode, *, channel_id, goal):
        await handler._gate.wait()
        handler._broadcaster.broadcast(channel_id, {"type": "chat_done", "payload": {}})
    handler.resolve_mode = _slow_resolve  # type: ignore[assignment]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        url = f"/v1/chat/threads/{thread.thread_id}/mode-decision"
        bg = asyncio.create_task(_consume_stream(client, url, {"mode": "explain"}))
        await asyncio.sleep(0.05)
        assert thread.thread_id in handler._active_turns
        gate.set()
        await bg


@pytest.mark.asyncio
async def test_live_reports_turn_active(_app):
    app, chat_store, gate, handler = _app
    thread = chat_store.create_thread("/ws")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        # No turn yet → turn_active False.
        live0 = await client.get(f"/v1/chat/threads/{thread.thread_id}/live")
        assert live0.json()["turn_active"] is False

        url = f"/v1/chat/threads/{thread.thread_id}/message"
        bg = asyncio.create_task(_consume_stream(client, url, {"content": "hi"}))
        await asyncio.sleep(0.05)
        live1 = await client.get(f"/v1/chat/threads/{thread.thread_id}/live")
        assert live1.json()["turn_active"] is True
        gate.set()
        await bg


@pytest.mark.asyncio
async def test_stop_route_cancels_active_turn(_app):
    app, chat_store, gate, handler = _app
    thread = chat_store.create_thread("/ws")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        url = f"/v1/chat/threads/{thread.thread_id}/message"
        bg = asyncio.create_task(_consume_stream(client, url, {"content": "hi"}))
        await asyncio.sleep(0.05)
        assert thread.thread_id in handler._active_turns
        resp = await client.post(f"/v1/chat/threads/{thread.thread_id}/stop")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        # The cancelled turn's relay closes on the stop's chat_done broadcast.
        await bg
    assert thread.thread_id not in handler._active_turns
    # Idle thread → benign no-op.
    transport2 = ASGITransport(app=app)
    async with AsyncClient(transport=transport2, base_url="http://t") as client:
        resp = await client.post(f"/v1/chat/threads/{thread.thread_id}/stop")
        assert resp.json()["ok"] is False
