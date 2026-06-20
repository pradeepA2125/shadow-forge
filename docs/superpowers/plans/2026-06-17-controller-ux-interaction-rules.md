# Controller UX Interaction Rules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the agentic `ChatController` to parity with the task pipeline's UX interaction rules (input availability, one-shot decisions, navigation lock) and make controller turns + held-open gates durable across a webview reload by detaching the turn from the SSE request.

**Architecture:** The controller turn moves from "the request *is* the work" to "the request *launches and subscribes to* the work" — exactly like `create_task` + `/stream-patch`. A new in-memory `ChatController._active_turns: dict[str, asyncio.Task]` (mirroring `orchestrator._running_tasks`) is the single source for the in-flight guard, the durable `turn_active` input signal exposed via `/live`, and the `stop_turn` cancel handle. With the turn detached, a held-open `EditGate` survives reload for free (it only died because the SSE-cancel cleared it). The frontend gains a `turn_active`-driven input-availability ruleset and re-subscribes to the chat channel on mount for the live overlay.

**Tech Stack:** Python 3.13 / FastAPI / pydantic / pytest-asyncio (backend); TypeScript / Zod / vitest (editor-client); React / vitest (webview-ui); TypeScript / VS Code API (extension).

**Flag-tolerance principle (applies throughout):** the legacy `ChatAgent` (controller flag off) has no `_active_turns` attribute. Every backend touch-point branches on `hasattr(handler, "_active_turns")` so the legacy request-bound path is **completely unchanged**. This mirrors the existing `/live` flag-tolerance pattern.

---

## File Structure

**Backend (`services/agentd-py/agentd/`)**
- `chat/models.py` — add `ThreadLiveState.turn_active: bool`.
- `chat/controller.py` — `_active_turns` dict + `launch_turn`/`_run_turn` lifecycle helpers; clear gate at `handle_message` start; `stop_turn`; `resolve_edit` backend-restart-orphan handling.
- `api/routes.py` — detach `/message` + `/mode-decision` (flag-tolerant: create_task + subscribe-relay, no cancel-on-disconnect, in-flight 409 guard); `POST /chat/threads/{id}/stop`; `/live` injects `turn_active`.

**editor-client (`apps/editor-client/src/`)**
- `contracts/task-contracts.ts` — `ThreadLiveStateSchema.turnActive`; `streamChannel` + `stopChatTurn` on `BackendTaskClient`.
- `client/http-backend-client.ts` — map `turn_active`→`turnActive`; implement `streamChannel(channelId)` + `stopChatTurn(threadId)`.

**webview-ui (`apps/vscode-extension/webview-ui/src/`)**
- `types.ts` — `AppState.turnActive`; `liveStatus` extension message carries `turnActive` + gate kind (new `setTurnActive` message).
- `inputAvailability.ts` — controller precedence rows (edit / mode / turnActive) ahead of the task rows.
- `components/messages/gates/ModeGate.tsx` — replace the "keep typing" hint with a one-shot "chat about this" input.

**extension (`apps/vscode-extension/src/`)**
- `controller.ts` — plumb `turnActive` + gate kind into the webview; nav-lock on `turn_active`; Stop posts `/stop`; live-resume re-subscribe to `chat:{thread_id}` on mount.

---

## PHASE A — Backend: turn detachment, durability, stop

### Task A1: `ThreadLiveState.turn_active` field

**Files:**
- Modify: `services/agentd-py/agentd/chat/models.py:81-97`
- Test: `services/agentd-py/tests/test_controller_schema.py`

- [ ] **Step 1: Write the failing test**

Add to `services/agentd-py/tests/test_controller_schema.py`:

```python
def test_thread_live_state_turn_active_defaults_false():
    from agentd.chat.models import ThreadLiveState

    state = ThreadLiveState()
    assert state.turn_active is False
    # round-trips through model_dump (the /live route serializes with this)
    assert state.model_dump()["turn_active"] is False
    assert ThreadLiveState(turn_active=True).turn_active is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/agentd-py && pytest tests/test_controller_schema.py::test_thread_live_state_turn_active_defaults_false -v`
Expected: FAIL with `AttributeError` / unexpected-keyword (`turn_active`).

- [ ] **Step 3: Add the field**

In `chat/models.py`, add to `ThreadLiveState` (after `active_task_id`):

```python
    active_task_id: str | None = None
    # True while a controller turn (or a held-open controller gate) is in flight. The
    # /live route sets it from ChatController._active_turns so the FE can keep input
    # disabled across a webview reload (the ephemeral inputEnabled flag resets on mount).
    turn_active: bool = False
    status: str | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/agentd-py && pytest tests/test_controller_schema.py::test_thread_live_state_turn_active_defaults_false -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/agentd-py/agentd/chat/models.py services/agentd-py/tests/test_controller_schema.py
git commit -m "feat(chat-controller): add ThreadLiveState.turn_active field"
```

---

### Task A2: `_active_turns` lifecycle + clear-gate-at-turn-start

**Files:**
- Modify: `services/agentd-py/agentd/chat/controller.py:80-203`
- Test: `services/agentd-py/tests/test_controller_durable_turn.py` (Create)

Adds the in-memory turn registry and the two lifecycle helpers the routes call, plus the start-of-turn gate clear. No route wiring yet (that is A3/A4) — this task is unit-testable in isolation.

- [ ] **Step 1: Write the failing test**

Create `services/agentd-py/tests/test_controller_durable_turn.py`:

```python
"""Detached controller turn lifecycle: _active_turns register/clear, gate-clear at
turn start, the in-flight membership signal, and stop_turn. These mirror the
orchestrator's _running_tasks + /abort pattern (CLAUDE.md Tier B)."""
import asyncio

import pytest

from agentd.chat.controller import ChatController
from agentd.chat.models import PendingGate
from agentd.chat.storage import ChatThreadStore
from agentd.orchestrator.broadcaster import EventBroadcaster
from agentd.orchestrator.scripted_engine import ScriptedReasoningEngine


def _controller(tmp_path, store) -> ChatController:
    return ChatController(
        workspace_path=str(tmp_path),
        reasoning_engine=ScriptedReasoningEngine(None, []),
        thread_store=store, orchestrator=None,
        broadcaster=EventBroadcaster(), retrieval_client=None)


@pytest.mark.asyncio
async def test_launch_turn_registers_then_clears(tmp_path):
    store = ChatThreadStore(tmp_path / "chat.sqlite3")
    thread = store.create_thread(str(tmp_path))
    ctrl = _controller(tmp_path, store)

    started = asyncio.Event()
    release = asyncio.Event()

    async def _body():
        started.set()
        await release.wait()

    task = ctrl.launch_turn(thread.thread_id, _body())
    await started.wait()
    # Registered while in flight — this is the in-flight guard + turn_active signal.
    assert thread.thread_id in ctrl._active_turns
    release.set()
    await task
    # Cleared in the finally on completion.
    assert thread.thread_id not in ctrl._active_turns


@pytest.mark.asyncio
async def test_launch_turn_clears_on_exception(tmp_path):
    store = ChatThreadStore(tmp_path / "chat.sqlite3")
    thread = store.create_thread(str(tmp_path))
    ctrl = _controller(tmp_path, store)

    async def _body():
        raise RuntimeError("boom")

    task = ctrl.launch_turn(thread.thread_id, _body())
    with pytest.raises(RuntimeError):
        await task
    assert thread.thread_id not in ctrl._active_turns


@pytest.mark.asyncio
async def test_handle_message_clears_stale_gate_at_start(tmp_path):
    store = ChatThreadStore(tmp_path / "chat.sqlite3")
    thread = store.create_thread(str(tmp_path))
    store.set_controller_gate(thread.thread_id, PendingGate(kind="mode", payload={}))
    ctrl = _controller(tmp_path, store)

    # Capture the gate state at the moment the turn body runs — proves the clear
    # happens at the START, independent of what the turn ultimately does. Stub the
    # loop so the test never depends on scripted-engine responses.
    gate_at_turn_start: list = []

    async def _fake_loop(thread_id, channel_id, goal, **kwargs):
        refreshed = store.get_thread(thread_id)
        gate_at_turn_start.append(refreshed.pending_controller_gate)
        from agentd.chat.controller_loop import ControllerOutcome
        return ControllerOutcome(kind="answer", text="ok")

    ctrl._run_loop = _fake_loop  # type: ignore[assignment]
    await ctrl.handle_message(
        thread.thread_id, "hello", channel_id=f"chat:{thread.thread_id}")

    assert gate_at_turn_start == [None]  # cleared before the loop ran
```

(Confirm `ControllerOutcome`'s required fields by reading `agentd/chat/controller_loop.py` — adjust the kwargs if `kind`/`text` aren't the exact constructor names.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/agentd-py && pytest tests/test_controller_durable_turn.py -v`
Expected: FAIL — `AttributeError: 'ChatController' object has no attribute 'launch_turn'`.

- [ ] **Step 3: Add `_active_turns`, `launch_turn`, `_run_turn`, and the start-of-turn gate clear**

In `chat/controller.py`, add to `__init__` (after `self._edit_clarify_pending`):

```python
        self._edit_clarify_pending: set[str] = set()
        # In-memory registry of the one detached turn per thread (mirrors the
        # orchestrator's _running_tasks). Earns its keep three ways: the in-flight
        # 409 guard (routes), the durable `turn_active` input signal (/live), and the
        # task handle stop_turn cancels. A backend restart clears it — the orphaned
        # turn is dead anyway (the transcript + pending_controller_gate survive in sqlite).
        self._active_turns: dict[str, asyncio.Task] = {}
```

Add these two methods (place after `__init__`, before `_build_registry`):

```python
    def launch_turn(self, thread_id: str, coro) -> asyncio.Task:
        """Detach a turn: create the task, register it, return the handle.

        create_task + the dict assignment have no `await` between them, so the
        in-flight guard (routes: `thread_id in _active_turns`) is race-safe in
        asyncio — same posture as the task routes' `_in_flight_*` guards."""
        task = asyncio.create_task(self._run_turn(thread_id, coro))
        self._active_turns[thread_id] = task
        return task

    async def _run_turn(self, thread_id: str, coro) -> None:
        """Run a turn coroutine and unconditionally clear its registry entry.

        The `finally` fires on normal completion, on error, AND on cancellation
        (stop_turn) — the single owner releasing its own slot so the thread never
        stays falsely `turn_active`."""
        try:
            await coro
        finally:
            self._active_turns.pop(thread_id, None)
```

In `handle_message`, clear any stale gate at the very start (after the `thread is None` check, before auto-naming):

```python
        thread = self._store.get_thread(thread_id)
        if thread is None:
            raise ValueError(f"Thread {thread_id!r} not found")
        # A new turn can never leave a stale gate rendered: clear it at the start so a
        # late decision on a superseded card hits `gate is None` and no-ops (resolve_mode/
        # resolve_edit already guard on this). A clarify sets no gate, so this is a no-op
        # on the clarify/EDIT-clarify resume path — no conflict.
        self._store.set_controller_gate(thread_id, None)
        # Auto-name the thread from its first user message (mirrors ChatAgent).
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/agentd-py && pytest tests/test_controller_durable_turn.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the controller regression suite**

Run: `cd services/agentd-py && pytest tests/test_controller_durable_edit.py tests/test_controller_edit_clarify.py tests/test_controller_live_gate.py -q`
Expected: PASS — confirms the start-of-turn gate clear didn't break the EDIT-clarify resume or live-gate flows.

- [ ] **Step 6: Commit**

```bash
git add services/agentd-py/agentd/chat/controller.py services/agentd-py/tests/test_controller_durable_turn.py
git commit -m "feat(chat-controller): _active_turns lifecycle + clear stale gate at turn start"
```

---

### Task A3: Detach `/message` route (in-flight guard, subscribe-relay, no cancel-on-disconnect)

**Files:**
- Modify: `services/agentd-py/agentd/api/routes.py:1122-1170`
- Test: `services/agentd-py/tests/test_controller_route_detach.py` (Create)

- [ ] **Step 1: Write the failing test**

Create `services/agentd-py/tests/test_controller_route_detach.py`:

```python
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
    from agentd.storage.memory_store import InMemoryTaskStore

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
    app.include_router(
        build_router(store, _Orch(), None, None, handler), prefix="/v1")
    return app, chat_store, gate, handler


@pytest.mark.asyncio
async def test_second_message_while_active_returns_409(_app):
    app, chat_store, gate, handler = _app
    thread = chat_store.create_thread("/ws")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        # First message: open the stream, but DON'T release the gate — turn stays active.
        async with client.stream(
            "POST", f"/v1/chat/threads/{thread.thread_id}/message",
            json={"content": "hi"},
        ) as resp1:
            assert resp1.status_code == 200
            # Give the detached turn a tick to register in _active_turns.
            await asyncio.sleep(0.05)
            assert thread.thread_id in handler._active_turns
            resp2 = await client.post(
                f"/v1/chat/threads/{thread.thread_id}/message", json={"content": "again"})
            assert resp2.status_code == 409
            gate.set()  # release so the first stream completes cleanly
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/agentd-py && pytest tests/test_controller_route_detach.py::test_second_message_while_active_returns_409 -v`
Expected: FAIL with `assert 200 == 409` (no guard yet) — or a 200 second response.

- [ ] **Step 3: Rewrite the `/message` route to detach (flag-tolerant)**

Replace the body of `post_chat_message` (`routes.py:1122-1170`) with:

```python
        @router.post("/chat/threads/{thread_id}/message")
        async def post_chat_message(thread_id: str, request: dict) -> StreamingResponse:
            import asyncio as _asyncio_chat
            import json as _json
            message = request.get("content") or request.get("message", "")
            _raw_step_review = request.get("step_review")
            step_review = _raw_step_review if isinstance(_raw_step_review, bool) else None
            channel_id = f"chat:{thread_id}"

            # Flag-tolerant: only the ChatController detaches turns. The legacy
            # ChatAgent (no _active_turns) keeps the request-bound path below.
            _active = getattr(_chat_agent, "_active_turns", None)

            if _active is not None:
                # In-flight guard: a concurrent turn on this thread is a 409 (benign —
                # the FE already blocks it via disabled input; isBenignConflict swallows
                # it). Check+launch have no await between → race-safe in asyncio.
                if thread_id in _active:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Thread {thread_id} already has a turn in progress")
                _chat_agent._broadcaster.clear_replay(channel_id)
                _chat_agent.launch_turn(  # type: ignore[attr-defined]
                    thread_id,
                    _chat_agent.handle_message(
                        thread_id, message, channel_id=channel_id,
                        step_review=step_review),
                )
                queue = _chat_agent._broadcaster.subscribe(channel_id)

                async def detached_stream():
                    # Subscribe-relay only: a client disconnect unsubscribes but does
                    # NOT cancel the detached turn (mirrors /stream-patch). The turn
                    # keeps running, parked on any gate, durable across the reload.
                    try:
                        while True:
                            try:
                                event = await _asyncio_chat.wait_for(
                                    queue.get(), timeout=15.0)
                            except _asyncio_chat.TimeoutError:
                                yield ": ping\n\n"
                                continue
                            yield f"data: {_json.dumps(event)}\n\n"
                            if event.get("type") in ("chat_done", "done"):
                                break
                    finally:
                        _chat_agent._broadcaster.unsubscribe(channel_id, queue)

                return StreamingResponse(
                    detached_stream(), media_type="text/event-stream")

            # --- legacy ChatAgent path (request-bound, cancel-on-disconnect) ---
            _chat_agent._broadcaster.clear_replay(channel_id)
            queue = _chat_agent._broadcaster.subscribe(channel_id)

            async def _run_agent() -> None:
                try:
                    await _chat_agent.handle_message(
                        thread_id, message, channel_id=channel_id, step_review=step_review,
                    )
                except Exception:
                    import logging as _logging
                    _logging.getLogger(__name__).exception("handle_message failed")
                    _chat_agent._broadcaster.broadcast(
                        channel_id, {"type": "chat_done", "payload": {}})

            async def event_stream():
                agent_task = _asyncio_chat.create_task(_run_agent())
                try:
                    while True:
                        try:
                            event = await _asyncio_chat.wait_for(queue.get(), timeout=15.0)
                        except _asyncio_chat.TimeoutError:
                            yield ": ping\n\n"
                            continue
                        yield f"data: {_json.dumps(event)}\n\n"
                        if event.get("type") in ("chat_done", "done"):
                            break
                finally:
                    _chat_agent._broadcaster.unsubscribe(channel_id, queue)
                    agent_task.cancel()

            return StreamingResponse(event_stream(), media_type="text/event-stream")
```

Note: the detached path has no top-level try/except around `handle_message` because `_run_turn` lets the exception propagate to the task (and `_active_turns` clears in its `finally`); the relay closes on `chat_done`. To guarantee a `chat_done` even on a turn that errors before emitting one, the controller's existing per-outcome `_finish` always broadcasts `chat_done`; an *unexpected* exception is covered in Step 4.

- [ ] **Step 4: Guarantee `chat_done` on an errored detached turn**

So a crashed turn doesn't hang the relay (no `chat_done`), wrap the coroutine passed to `launch_turn`. In `chat/controller.py`, change `launch_turn` to accept a `channel_id` for the failsafe broadcast:

```python
    def launch_turn(self, thread_id: str, coro, *, channel_id: str | None = None) -> asyncio.Task:
        task = asyncio.create_task(self._run_turn(thread_id, coro, channel_id))
        self._active_turns[thread_id] = task
        return task

    async def _run_turn(self, thread_id: str, coro, channel_id: str | None = None) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise  # stop_turn / shutdown — re-raise so the task is marked cancelled
        except Exception:
            logger.exception("[controller] turn failed (thread=%s)", thread_id)
            if channel_id is not None:
                self._broadcaster.broadcast(
                    channel_id, {"type": "chat_done", "payload": {}})
        finally:
            self._active_turns.pop(thread_id, None)
```

Update the route's `launch_turn` call to pass `channel_id=channel_id`. Update the A2 test `test_launch_turn_clears_on_exception` to expect the task to **complete** (the exception is now swallowed + logged, not re-raised) rather than raising:

```python
@pytest.mark.asyncio
async def test_launch_turn_clears_on_exception(tmp_path):
    store = ChatThreadStore(tmp_path / "chat.sqlite3")
    thread = store.create_thread(str(tmp_path))
    ctrl = _controller(tmp_path, store)

    async def _body():
        raise RuntimeError("boom")

    task = ctrl.launch_turn(
        thread.thread_id, _body(), channel_id=f"chat:{thread.thread_id}")
    await task  # swallowed + logged, task completes
    assert thread.thread_id not in ctrl._active_turns
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd services/agentd-py && pytest tests/test_controller_route_detach.py tests/test_controller_durable_turn.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add services/agentd-py/agentd/api/routes.py services/agentd-py/agentd/chat/controller.py services/agentd-py/tests/test_controller_route_detach.py services/agentd-py/tests/test_controller_durable_turn.py
git commit -m "feat(chat-controller): detach /message turn — 409 guard + subscribe-relay, no cancel-on-disconnect"
```

---

### Task A4: Detach `/mode-decision` route

**Files:**
- Modify: `services/agentd-py/agentd/api/routes.py:1172-1213`
- Test: `services/agentd-py/tests/test_controller_route_detach.py` (extend)

`resolve_mode` is the turn body for the mode gate (edit/explain re-enter the loop, create_task hands off). It must detach identically so the re-entered EDIT turn is durable.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_controller_route_detach.py`:

```python
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
        async with client.stream(
            "POST", f"/v1/chat/threads/{thread.thread_id}/mode-decision",
            json={"mode": "explain"},
        ) as resp:
            assert resp.status_code == 200
            await asyncio.sleep(0.05)
            assert thread.thread_id in handler._active_turns
            gate.set()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/agentd-py && pytest tests/test_controller_route_detach.py::test_mode_decision_registers_detached_turn -v`
Expected: FAIL — `assert thread_id in handler._active_turns` (the current route uses a request-bound `create_task` that does not register in `_active_turns`).

- [ ] **Step 3: Rewrite the `/mode-decision` route to detach (flag-tolerant)**

Replace the body of `post_mode_decision` (`routes.py:1172-1213`) with:

```python
        @router.post("/chat/threads/{thread_id}/mode-decision")
        async def post_mode_decision(thread_id: str, request: dict) -> StreamingResponse:
            import asyncio as _asyncio_mode
            import json as _json_mode
            mode = request.get("mode", "")
            channel_id = f"chat:{thread_id}"
            thread = _chat_agent._store.get_thread(thread_id)
            goal = ""
            if thread is not None:
                goal = next(
                    (m.content for m in reversed(thread.messages) if m.role == "user"), "")

            _active = getattr(_chat_agent, "_active_turns", None)

            if _active is not None:
                if thread_id in _active:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Thread {thread_id} already has a turn in progress")
                _chat_agent._broadcaster.clear_replay(channel_id)
                _chat_agent.launch_turn(  # type: ignore[attr-defined]
                    thread_id,
                    _chat_agent.resolve_mode(  # type: ignore[attr-defined]
                        thread_id, mode, channel_id=channel_id, goal=goal),
                    channel_id=channel_id,
                )
                queue = _chat_agent._broadcaster.subscribe(channel_id)

                async def detached_mode_stream():
                    try:
                        while True:
                            try:
                                event = await _asyncio_mode.wait_for(
                                    queue.get(), timeout=15.0)
                            except _asyncio_mode.TimeoutError:
                                yield ": ping\n\n"
                                continue
                            yield f"data: {_json_mode.dumps(event)}\n\n"
                            if event.get("type") in ("chat_done", "done"):
                                break
                    finally:
                        _chat_agent._broadcaster.unsubscribe(channel_id, queue)

                return StreamingResponse(
                    detached_mode_stream(), media_type="text/event-stream")

            # --- legacy ChatAgent path (request-bound) ---
            _chat_agent._broadcaster.clear_replay(channel_id)
            queue = _chat_agent._broadcaster.subscribe(channel_id)

            async def _run_dispatch() -> None:
                try:
                    await _chat_agent.resolve_mode(  # type: ignore[attr-defined]
                        thread_id, mode, channel_id=channel_id, goal=goal)
                except Exception:
                    import logging as _logging
                    _logging.getLogger(__name__).exception("resolve_mode failed")
                    _chat_agent._broadcaster.broadcast(
                        channel_id, {"type": "chat_done", "payload": {}})

            async def event_stream():
                dispatch_task = _asyncio_mode.create_task(_run_dispatch())
                try:
                    while True:
                        try:
                            event = await _asyncio_mode.wait_for(queue.get(), timeout=15.0)
                        except TimeoutError:
                            yield ": ping\n\n"
                            continue
                        yield f"data: {_json_mode.dumps(event)}\n\n"
                        if event.get("type") in ("chat_done", "done"):
                            break
                finally:
                    _chat_agent._broadcaster.unsubscribe(channel_id, queue)
                    dispatch_task.cancel()

            return StreamingResponse(event_stream(), media_type="text/event-stream")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd services/agentd-py && pytest tests/test_controller_route_detach.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add services/agentd-py/agentd/api/routes.py services/agentd-py/tests/test_controller_route_detach.py
git commit -m "feat(chat-controller): detach /mode-decision turn (parity with /message)"
```

---

### Task A5: `/live` injects `turn_active`

**Files:**
- Modify: `services/agentd-py/agentd/api/routes.py:1085-1120`
- Test: `services/agentd-py/tests/test_controller_live_gate.py` (extend) or `test_controller_route_detach.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_controller_route_detach.py`:

```python
@pytest.mark.asyncio
async def test_live_reports_turn_active(_app):
    app, chat_store, gate, handler = _app
    thread = chat_store.create_thread("/ws")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        # No turn yet → turn_active False.
        live0 = await client.get(f"/v1/chat/threads/{thread.thread_id}/live")
        assert live0.json()["turn_active"] is False

        async with client.stream(
            "POST", f"/v1/chat/threads/{thread.thread_id}/message",
            json={"content": "hi"},
        ):
            await asyncio.sleep(0.05)
            live1 = await client.get(f"/v1/chat/threads/{thread.thread_id}/live")
            assert live1.json()["turn_active"] is True
            gate.set()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/agentd-py && pytest tests/test_controller_route_detach.py::test_live_reports_turn_active -v`
Expected: FAIL — `turn_active` is always `False` (the route never sets it).

- [ ] **Step 3: Inject `turn_active` in the `/live` route**

In `get_thread_live` (`routes.py:1117-1120`), after `live = resolve_thread_live(...)`:

```python
            # Thread-aware overlay: a controller gate (mode/edit) on the thread takes
            # precedence over the task-derived state (the controller has no task).
            live = resolve_thread_live(thread, active_id if task is not None else None, _get)
            # Flag-tolerant durable input signal: a detached controller turn (or a held-
            # open controller gate parked on a future) keeps input disabled across reload.
            # The legacy ChatAgent has no _active_turns → resolves to False (no regression).
            live.turn_active = thread_id in getattr(_chat_agent, "_active_turns", {})
            return live.model_dump()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/agentd-py && pytest tests/test_controller_route_detach.py::test_live_reports_turn_active -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/agentd-py/agentd/api/routes.py services/agentd-py/tests/test_controller_route_detach.py
git commit -m "feat(chat-controller): /live exposes turn_active (flag-tolerant)"
```

---

### Task A6: `stop_turn` + `POST /chat/threads/{id}/stop`

**Files:**
- Modify: `services/agentd-py/agentd/chat/controller.py`
- Modify: `services/agentd-py/agentd/api/routes.py` (new route, near the edit-decision route ~1215)
- Test: `services/agentd-py/tests/test_controller_durable_turn.py` (extend)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_controller_durable_turn.py`:

```python
@pytest.mark.asyncio
async def test_stop_turn_cancels_and_breadcrumbs(tmp_path):
    store = ChatThreadStore(tmp_path / "chat.sqlite3")
    thread = store.create_thread(str(tmp_path))
    ctrl = _controller(tmp_path, store)
    channel_id = f"chat:{thread.thread_id}"

    release = asyncio.Event()

    async def _body():
        await release.wait()  # never released — stop_turn cancels it

    task = ctrl.launch_turn(thread.thread_id, _body(), channel_id=channel_id)
    await asyncio.sleep(0.02)
    assert thread.thread_id in ctrl._active_turns

    ok = await ctrl.stop_turn(thread.thread_id)
    assert ok is True
    assert task.cancelled()
    assert thread.thread_id not in ctrl._active_turns

    # Durable ✗ Stopped breadcrumb persisted.
    refreshed = store.get_thread(thread.thread_id)
    assert any(
        m.metadata.get("breadcrumb") and "Stopped" in m.content
        for m in refreshed.messages)
    # Idempotent: stopping an idle thread is a benign no-op.
    assert await ctrl.stop_turn(thread.thread_id) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/agentd-py && pytest tests/test_controller_durable_turn.py::test_stop_turn_cancels_and_breadcrumbs -v`
Expected: FAIL — `AttributeError: 'ChatController' object has no attribute 'stop_turn'`.

- [ ] **Step 3: Add `stop_turn` to the controller**

In `chat/controller.py`, add (after `resolve_edit`):

```python
    async def stop_turn(self, thread_id: str) -> bool:
        """Cancel a detached turn (POST /stop) — a slimmer cousin of task /abort.

        Cancels the asyncio.Task; the turn's own finally chain does the cleanup:
        _run_turn pops _active_turns, ControllerLoop.run's finally closes the turn-
        shadow, and a held-open EditGate's _edit_decision_cb finally clears the gate +
        pops _pending_edit. Then broadcast chat_done so the relay closes, and write a
        durable ✗ Stopped breadcrumb. Benign no-op (False) if no active turn."""
        task = self._active_turns.get(thread_id)
        if task is None or task.done():
            return False
        task.cancel()
        try:
            await task  # let the cancellation unwind (finally chain runs)
        except asyncio.CancelledError:
            pass
        channel_id = f"chat:{thread_id}"
        self._write_breadcrumb(thread_id, channel_id, "✗ Stopped")
        self._broadcaster.broadcast(channel_id, {"type": "chat_done", "payload": {}})
        return True
```

- [ ] **Step 4: Run the controller test to verify it passes**

Run: `cd services/agentd-py && pytest tests/test_controller_durable_turn.py::test_stop_turn_cancels_and_breadcrumbs -v`
Expected: PASS

- [ ] **Step 5: Add the `POST /stop` route**

In `routes.py`, after the `post_edit_decision` route (~1221):

```python
        @router.post("/chat/threads/{thread_id}/stop")
        async def post_stop_turn(thread_id: str) -> dict:
            # Stop a detached controller turn (replaces the old SSE-disconnect cancel).
            # Benign no-op when no turn is active. The legacy ChatAgent has no stop_turn —
            # report ok=false rather than 500.
            stop = getattr(_chat_agent, "stop_turn", None)
            if stop is None:
                return {"ok": False}
            ok = await stop(thread_id)  # type: ignore[misc]
            return {"ok": ok}
```

- [ ] **Step 6: Write + run a route-level stop test**

Add to `tests/test_controller_route_detach.py`:

```python
@pytest.mark.asyncio
async def test_stop_route_cancels_active_turn(_app):
    app, chat_store, gate, handler = _app
    thread = chat_store.create_thread("/ws")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        async with client.stream(
            "POST", f"/v1/chat/threads/{thread.thread_id}/message",
            json={"content": "hi"},
        ):
            await asyncio.sleep(0.05)
            resp = await client.post(f"/v1/chat/threads/{thread.thread_id}/stop")
            assert resp.status_code == 200
            assert resp.json()["ok"] is True
        assert thread.thread_id not in handler._active_turns
    # Idle thread → benign no-op.
    transport2 = ASGITransport(app=app)
    async with AsyncClient(transport=transport2, base_url="http://t") as client:
        resp = await client.post(f"/v1/chat/threads/{thread.thread_id}/stop")
        assert resp.json()["ok"] is False
```

Run: `cd services/agentd-py && pytest tests/test_controller_route_detach.py tests/test_controller_durable_turn.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add services/agentd-py/agentd/chat/controller.py services/agentd-py/agentd/api/routes.py services/agentd-py/tests/test_controller_durable_turn.py services/agentd-py/tests/test_controller_route_detach.py
git commit -m "feat(chat-controller): stop_turn + POST /stop (restores Stop for detached turns)"
```

---

### Task A7: `resolve_edit` clears a stale gate on the backend-restart orphan

**Files:**
- Modify: `services/agentd-py/agentd/chat/controller.py:392-399`
- Test: `services/agentd-py/tests/test_controller_durable_edit.py` (extend)

After a backend restart, `pending_controller_gate(kind="edit")` persists in sqlite but `_pending_edit` (in-memory) is gone — the EditGate would render with no waiter. `resolve_edit` must detect the missing waiter and clear the stale gate + write a breadcrumb so the UI unwedges (since `turn_active` is already `False` post-restart, input re-enables).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_controller_durable_edit.py`:

```python
@pytest.mark.asyncio
async def test_resolve_edit_clears_stale_gate_when_no_waiter(tmp_path: Path):
    """Backend-restart orphan: a persisted EditGate with no live _pending_edit future
    is cleared (+ breadcrumb) instead of no-op'ing, so the UI unwedges."""
    store = ChatThreadStore(tmp_path / "chat.sqlite3")
    thread = store.create_thread(str(tmp_path))
    # Simulate the post-restart state: gate persisted, no in-memory waiter.
    store.set_controller_gate(thread.thread_id, PendingGate(kind="edit", payload={}))
    ctrl = _controller(tmp_path, store)
    assert thread.thread_id not in ctrl._pending_edit

    ok = await ctrl.resolve_edit(thread.thread_id, {"decision": "accept"})
    assert ok is False  # nothing resumed (no waiter)

    refreshed = store.get_thread(thread.thread_id)
    assert refreshed.pending_controller_gate is None  # stale gate cleared
    assert any(
        m.metadata.get("breadcrumb") and "re-send" in m.content.lower()
        for m in refreshed.messages)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/agentd-py && pytest tests/test_controller_durable_edit.py::test_resolve_edit_clears_stale_gate_when_no_waiter -v`
Expected: FAIL — gate stays set (current `resolve_edit` returns `False` without clearing).

- [ ] **Step 3: Update `resolve_edit`**

Replace `resolve_edit` (`controller.py:392-399`) with:

```python
    async def resolve_edit(self, thread_id: str, decision: dict[str, object]) -> bool:
        """Resolve the per-edit gate (POST /edit-decision). Fires the future when a
        live waiter exists (never mutates/persists during the await — Class-A safety).

        Backend-restart orphan: when the EditGate persisted in sqlite but the in-memory
        waiter is gone (`thread_id not in _pending_edit`), clear the stale gate + write a
        breadcrumb so the UI unwedges (turn_active is already False post-restart → input
        re-enables). The user re-issues the edit. Matches the orphaned-task degradation."""
        fut = self._pending_edit.get(thread_id)
        if fut is None or fut.done():
            # No live waiter. If a stale edit gate persists (restart orphan), clear it.
            thread = self._store.get_thread(thread_id)
            gate = thread.pending_controller_gate if thread is not None else None
            if gate is not None and gate.kind == "edit":
                self._store.set_controller_gate(thread_id, None)
                self._write_breadcrumb(
                    thread_id, f"chat:{thread_id}",
                    "Previous turn ended — please re-send your request.")
            return False
        fut.set_result(decision)
        return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd services/agentd-py && pytest tests/test_controller_durable_edit.py -v`
Expected: PASS (existing + new test)

- [ ] **Step 5: Run the full controller backend suite**

Run: `cd services/agentd-py && pytest tests/ -k controller -q`
Expected: PASS (read the summary line, not a piped exit code — see CLAUDE.md).

- [ ] **Step 6: Commit**

```bash
git add services/agentd-py/agentd/chat/controller.py services/agentd-py/tests/test_controller_durable_edit.py
git commit -m "fix(chat-controller): resolve_edit clears stale EditGate on backend-restart orphan"
```

---

## PHASE B — editor-client contract

### Task B1: `ThreadLiveStateSchema.turnActive` + mapping

**Files:**
- Modify: `apps/editor-client/src/contracts/task-contracts.ts:255-265`
- Modify: `apps/editor-client/src/client/http-backend-client.ts:399-415`
- Test: `apps/editor-client/src/contracts/__tests__/` (find the existing contracts test file) or a new `http-backend-client` test.

- [ ] **Step 1: Locate the existing test file**

Run: `cd apps/editor-client && ls src/**/__tests__/ src/**/*.test.ts 2>/dev/null; grep -rl "ThreadLiveStateSchema\|getThreadLiveState" src --include=*.test.ts`
Expected: a path like `src/client/__tests__/http-backend-client.test.ts`. Use that file in Step 2 (create one beside `http-backend-client.ts` if none exists).

- [ ] **Step 2: Write the failing test**

Add to the located test file (adapt the existing fetch-mock harness in that file):

```typescript
it("maps turn_active to turnActive on /live", async () => {
  const client = makeClientWithJsonResponse({
    active_task_id: null,
    status: null,
    pending_gate: null,
    plan: null,
    turn_active: true,
  });
  const live = await client.getThreadLiveState("chat-1");
  expect(live.turnActive).toBe(true);
});

it("defaults turnActive to false when absent", async () => {
  const client = makeClientWithJsonResponse({
    active_task_id: null, status: null, pending_gate: null, plan: null,
  });
  const live = await client.getThreadLiveState("chat-1");
  expect(live.turnActive).toBe(false);
});
```

(`makeClientWithJsonResponse` stands in for the file's existing fetch-stub helper — reuse whatever it already uses to mock a JSON response.)

- [ ] **Step 3: Run test to verify it fails**

Run: `cd apps/editor-client && npx vitest run -t "turnActive"`
Expected: FAIL — `turnActive` is `undefined` (not in schema, not mapped).

- [ ] **Step 4: Add the schema field + mapping**

In `task-contracts.ts`, add to `ThreadLiveStateSchema` (after `plan`):

```typescript
  plan: z.record(z.unknown()).nullable(),
  // True while a controller turn / held-open controller gate is in flight (durable
  // input-disable signal that survives a webview reload). Absent on legacy payloads → false.
  turnActive: z.boolean().default(false),
```

In `http-backend-client.ts` `getThreadLiveState`, add to the parsed object:

```typescript
      plan: raw["plan"] ?? null,
      turnActive: raw["turn_active"] ?? false,
      failureSummary: this.toFailureSummary(raw),
```

- [ ] **Step 5: Run test + build**

Run: `cd apps/editor-client && npx vitest run -t "turnActive" && npm run -w @ai-editor/editor-client build`
Expected: tests PASS; build succeeds (the extension types off the compiled `dist`, so this build is required before Phase D typecheck).

- [ ] **Step 6: Commit**

```bash
git add apps/editor-client/src/contracts/task-contracts.ts apps/editor-client/src/client/http-backend-client.ts apps/editor-client/src/**/*.test.ts
git commit -m "feat(editor-client): ThreadLiveState.turnActive schema + mapping"
```

---

### Task B2: `streamChannel(channelId)` + `stopChatTurn(threadId)` client methods

**Files:**
- Modify: `apps/editor-client/src/contracts/task-contracts.ts:286-296` (interface)
- Modify: `apps/editor-client/src/client/http-backend-client.ts`
- Test: same client test file as B1.

- [ ] **Step 1: Write the failing test**

Add to the client test file:

```typescript
it("stopChatTurn posts to /stop and returns ok", async () => {
  const client = makeClientWithJsonResponse({ ok: true });
  const result = await client.stopChatTurn("chat-1");
  expect(result.ok).toBe(true);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/editor-client && npx vitest run -t "stopChatTurn"`
Expected: FAIL — `client.stopChatTurn is not a function`.

- [ ] **Step 3: Add the interface members**

In `task-contracts.ts` `BackendTaskClient`, after `postEditDecision`:

```typescript
  postEditDecision(threadId: string, decision: "accept" | "reject", reason?: string): Promise<void>;
  // Stop a detached controller turn (POST /chat/threads/{id}/stop). ok=false is benign.
  stopChatTurn(threadId: string): Promise<{ ok: boolean }>;
  // Subscribe-only SSE to any broadcaster channel (GET /v1/channels/{id}/stream). Used
  // to resume the live overlay for a controller turn after a webview reload (chat:{id}).
  streamChannel(channelId: string): AsyncIterable<StreamEvent>;
```

- [ ] **Step 4: Implement the methods**

In `http-backend-client.ts`, after `postModeDecision`:

```typescript
  async stopChatTurn(threadId: string): Promise<{ ok: boolean }> {
    const raw = await this.fetchJson(
      `/v1/chat/threads/${encodeURIComponent(threadId)}/stop`,
      { method: "POST", body: "{}" }
    ) as Record<string, unknown>;
    return { ok: Boolean(raw["ok"]) };
  }

  // Subscribe-only SSE relay (no turn launch). Reuses the SSE line-parsing already
  // behind postModeDecision/streamPatch. Closes on `done`/`chat_done`.
  async *streamChannel(channelId: string): AsyncIterable<StreamEvent> {
    const response = await this.fetchFn(
      `${this.options.baseUrl}/v1/channels/${encodeURIComponent(channelId)}/stream`,
      { headers: { accept: "text/event-stream" } }
    );
    if (!response.ok) {
      throw new Error(`Channel stream failed (${response.status}) for ${channelId}`);
    }
    if (!response.body) return;
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";
        for (const line of lines) {
          if (!line.startsWith("data:")) continue;
          try {
            const event = ChatEventSchema.parse(
              JSON.parse(line.slice(5).trim())) as StreamEvent;
            yield event;
            if (event.type === "chat_done" || event.type === "done") return;
          } catch {
            // skip malformed SSE line
          }
        }
      }
    } finally {
      reader.cancel().catch(() => {});
    }
  }
```

- [ ] **Step 5: Run test + build**

Run: `cd apps/editor-client && npx vitest run -t "stopChatTurn" && npm run -w @ai-editor/editor-client build`
Expected: PASS; build succeeds.

- [ ] **Step 6: Commit**

```bash
git add apps/editor-client/src/contracts/task-contracts.ts apps/editor-client/src/client/http-backend-client.ts apps/editor-client/src/**/*.test.ts
git commit -m "feat(editor-client): streamChannel + stopChatTurn client methods"
```

---

## PHASE C — webview-ui: input rules + ModeGate

### Task C1: `inputAvailability` controller precedence rows

**Files:**
- Modify: `apps/vscode-extension/webview-ui/src/types.ts` (AppState + signature of inputAvailability inputs)
- Modify: `apps/vscode-extension/webview-ui/src/inputAvailability.ts`
- Test: `apps/vscode-extension/webview-ui/src/inputAvailability.test.ts` (find/create)

- [ ] **Step 1: Locate or create the test file**

Run: `cd apps/vscode-extension/webview-ui && ls src/inputAvailability.test.ts 2>/dev/null || grep -rl inputAvailability src --include=*.test.ts`
Use that path (create `src/inputAvailability.test.ts` if absent).

- [ ] **Step 2: Write the failing tests**

Add (or create the file with) these cases — they encode the spec §5 precedence top→bottom:

```typescript
import { describe, expect, it } from "vitest";
import { inputAvailability } from "./inputAvailability";

const base = { inputEnabled: true, liveStatus: null, workbar: null,
               liveGate: null, turnActive: false } as const;

describe("inputAvailability — controller precedence", () => {
  it("edit gate → disabled, decision placeholder", () => {
    const r = inputAvailability({
      ...base, liveGate: { kind: "edit", taskId: "t", payload: {} } });
    expect(r.disabled).toBe(true);
    expect(r.placeholder).toMatch(/decision on the card/i);
  });

  it("mode gate → disabled, choose-how placeholder", () => {
    const r = inputAvailability({
      ...base, liveGate: { kind: "mode", taskId: "t", payload: {} } });
    expect(r.disabled).toBe(true);
    expect(r.placeholder).toMatch(/choose how to proceed/i);
  });

  it("turn_active (no gate) → disabled, working placeholder, Stop shown", () => {
    const r = inputAvailability({ ...base, turnActive: true });
    expect(r.disabled).toBe(true);
    expect(r.placeholder).toMatch(/working/i);
    expect(r.showStop).toBe(true);
  });

  it("no gate, no turn, no task → enabled", () => {
    const r = inputAvailability(base);
    expect(r.disabled).toBe(false);
  });

  it("flag-off regression: task gate still disables (existing behavior)", () => {
    const r = inputAvailability({ ...base, liveStatus: "AWAITING_STEP_REVIEW" });
    expect(r.disabled).toBe(true);
    expect(r.placeholder).toMatch(/decision on the card/i);
  });
});
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd apps/vscode-extension/webview-ui && npx vitest run inputAvailability`
Expected: FAIL — `inputAvailability` doesn't accept `liveGate`/`turnActive` and has no controller rows.

- [ ] **Step 4: Extend `AppState` and the input signature**

In `types.ts` `AppState`, add:

```typescript
  liveStatus: string | null;
  // True while a controller turn / held-open controller gate is in flight (durable
  // input-disable signal from /live; survives reload). Distinct from inputEnabled,
  // which is the ephemeral per-turn flag a fresh webview mounts as `true`.
  turnActive: boolean;
```

In `inputAvailability.ts`, change the function signature and add the controller rows ABOVE the existing task rows:

```typescript
const CONTROLLER_GATE_KINDS = new Set(["mode", "edit"]);

export function inputAvailability(
  state: Pick<AppState, "inputEnabled" | "liveStatus" | "workbar" | "liveGate" | "turnActive">,
): InputAvailability {
  const { inputEnabled, liveStatus, workbar, liveGate, turnActive } = state;
  const taskStop = liveStatus !== null && ABORTABLE_STATUSES.has(liveStatus);

  // ── Controller precedence (spec §5), first match wins, ahead of task rows ──
  // Row 1: per-edit gate — only the EditGate card is interactive.
  if (liveGate?.kind === "edit") {
    return {
      disabled: true,
      placeholder: "Waiting for your decision on the card above",
      showStop: false,
      taskStop,
    };
  }
  // Row 2: mode gate — only the ModeGate (incl. its in-card field) is interactive.
  if (liveGate?.kind === "mode") {
    return {
      disabled: true,
      placeholder: "Choose how to proceed — or chat about it on the card",
      showStop: false,
      taskStop,
    };
  }
  // Row 3: a controller turn is running (no gate). The durable reload-window guard:
  // a fresh webview mounts inputEnabled=true while the detached turn still runs.
  // Stop is shown — a controller turn can be stopped (no task is active here).
  if (turnActive && (liveStatus === null || !TASK_ACTIVE_STATUSES.has(liveStatus))) {
    return {
      disabled: true,
      placeholder: "Agent is working…",
      showStop: true,
      taskStop,
    };
  }

  // ── Existing task-status rows (unchanged) ──
  // Precedence 1: a local chat turn is streaming.
  if (!inputEnabled) {
    const showStop =
      liveStatus === null || !TASK_ACTIVE_STATUSES.has(liveStatus);
    return { disabled: true, placeholder: "Agent is working…", showStop, taskStop };
  }
  // ... (leave the rest of the function — plan-approval, gate, running, default — as-is)
```

Keep everything from the original "Precedence 2: awaiting plan approval" downward unchanged. Guard against a `mode`/`edit` kind leaking into the existing `GATE_STATUSES` row — it won't, because that row keys on `liveStatus`, and controller gates set no task status.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd apps/vscode-extension/webview-ui && npx vitest run inputAvailability`
Expected: PASS (all rows + flag-off regression)

- [ ] **Step 6: Commit**

```bash
git add apps/vscode-extension/webview-ui/src/inputAvailability.ts apps/vscode-extension/webview-ui/src/types.ts apps/vscode-extension/webview-ui/src/inputAvailability.test.ts
git commit -m "feat(webview): controller input-availability precedence rows (edit/mode/turnActive)"
```

---

### Task C2: ModeGate "chat about this" one-shot field

**Files:**
- Modify: `apps/vscode-extension/webview-ui/src/components/messages/gates/ModeGate.tsx`
- Test: `apps/vscode-extension/webview-ui/src/components/messages/gates/ModeGate.test.tsx` (find/create)

The main composer is disabled while the ModeGate is up (Rule 1.2), so the in-card field is the only typing path — a controlled card action, not ambient typing. Submitting it posts a normal `sendMessage` (a fresh turn that supersedes the gate; `handle_message` clears the gate at start — Task A2).

- [ ] **Step 1: Write the failing test**

Create `ModeGate.test.tsx` beside the component (reuse the testing-library + `vscode` mock pattern from a sibling gate test, e.g. find one with `grep -rl "vscode.postMessage" src/components/messages/gates/*.test.tsx`):

```typescript
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ModeGate } from "./ModeGate";
import { vscode } from "../../../vscodeApi";

vi.mock("../../../vscodeApi", () => ({ vscode: { postMessage: vi.fn() } }));

const payload = {
  options: [{ mode: "edit", label: "Edit inline", description: "" }],
  recommended: "edit",
};

describe("ModeGate — chat about this", () => {
  it("renders the chat-about-this input instead of a hint", () => {
    render(<ModeGate taskId="chat-1" payload={payload} />);
    expect(screen.getByPlaceholderText(/chat about this/i)).toBeInTheDocument();
  });

  it("submitting posts exactly one sendMessage and is one-shot", () => {
    render(<ModeGate taskId="chat-1" payload={payload} />);
    const input = screen.getByPlaceholderText(/chat about this/i);
    fireEvent.change(input, { target: { value: "make it minimal" } });
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.keyDown(input, { key: "Enter" }); // second press ignored (one-shot)
    const calls = (vscode.postMessage as ReturnType<typeof vi.fn>).mock.calls
      .filter((c) => c[0]?.type === "sendMessage");
    expect(calls).toHaveLength(1);
    expect(calls[0][0].text).toBe("make it minimal");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/vscode-extension/webview-ui && npx vitest run ModeGate`
Expected: FAIL — no "chat about this" input (the component still shows the "…or keep typing" hint).

- [ ] **Step 3: Replace the hint with the one-shot input**

In `ModeGate.tsx`, add a second piece of one-shot state and a submit handler, and swap the hint `<span>` for an input. After `const [resolved, setResolved] = useState<string | null>(null);`:

```typescript
  const [resolved, setResolved] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  function handlePick(mode: string, label: string) {
    if (resolved !== null) return; // one-shot guard
    setResolved(label);
    vscode.postMessage({ type: "modeDecision", threadId: taskId, mode });
  }

  function handleChatAbout() {
    if (resolved !== null) return; // one-shot — shared with the mode picks
    const text = draft.trim();
    if (!text) return;
    setResolved("Discussing…");
    // A fresh turn supersedes the gate (handle_message clears it at start).
    vscode.postMessage({ type: "sendMessage", text });
  }
```

Replace the hint span (the `<span className="text-[10px] text-text-2 px-0.5">…or keep typing…</span>` block) with:

```tsx
          <input
            type="text"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                handleChatAbout();
              }
            }}
            placeholder="Chat about this approach…"
            className="mt-1 w-full rounded border border-border bg-transparent px-2 py-1 text-[11px] text-text-1 placeholder:text-text-2"
          />
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/vscode-extension/webview-ui && npx vitest run ModeGate`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/vscode-extension/webview-ui/src/components/messages/gates/ModeGate.tsx apps/vscode-extension/webview-ui/src/components/messages/gates/ModeGate.test.tsx
git commit -m "feat(webview): ModeGate one-shot 'chat about this' field (replaces ambient-typing hint)"
```

---

## PHASE D — extension controller wiring

### Task D1: Plumb `turnActive` + gate kind into the webview; nav-lock

**Files:**
- Modify: `apps/vscode-extension/webview-ui/src/types.ts` (extension→webview message)
- Modify: `apps/vscode-extension/src/controller.ts:1603` (`sendLiveStatus` site) + the webview message reducer
- Test: extend `inputAvailability` consumers are covered by C1; this task is verified via typecheck + the controller test if one exists.

The webview's `AppState.turnActive` must be fed from `/live`. The controller already calls `this.ui.sendLiveStatus(live.status ?? null)` at the end of `pollThreadLiveState`. Add `turnActive` to that signal.

- [ ] **Step 1: Find how `liveStatus` reaches AppState**

Run: `cd apps/vscode-extension && grep -rn "sendLiveStatus\|liveStatus\|case \"liveStatus\"" src webview-ui/src`
This shows the `ControllerUI.sendLiveStatus` declaration, the `ExtensionMessage` `liveStatus` variant, and the webview reducer case. Use those exact sites below.

- [ ] **Step 2: Extend the `liveStatus` extension message to carry `turnActive`**

In `webview-ui/src/types.ts` `ExtensionMessage`, change:

```typescript
  | { type: "liveStatus"; status: string | null; turnActive?: boolean }
```

In the webview reducer (where `liveStatus` is handled — found in Step 1), set both `liveStatus` and `turnActive` (default `false` when absent):

```typescript
    case "liveStatus":
      return { ...state, liveStatus: msg.status, turnActive: msg.turnActive ?? false };
```

Ensure the initial `AppState` includes `turnActive: false`.

- [ ] **Step 3: Send `turnActive` from the controller**

In `controller.ts`, update the `ControllerUI.sendLiveStatus` signature (interface near line 60) and the call site at line 1603:

```typescript
  // interface ControllerUI:
  sendLiveStatus(status: string | null, turnActive: boolean): void;
```

```typescript
    // pollThreadLiveState end (was: this.ui.sendLiveStatus(live.status ?? null);)
    this.ui.sendLiveStatus(live.status ?? null, live.turnActive ?? false);
```

Update the concrete `ui` implementation in `extension.ts` (and any test stub `ControllerUI`) to forward `turnActive`:

```typescript
    sendLiveStatus: (status, turnActive) =>
      chatPanel.postMessage({ type: "liveStatus", status, turnActive }),
```

- [ ] **Step 4: Nav-lock on `turn_active` (Rule 3)**

The webview already disables `‹ back` / history / `+ New Chat` when input is disabled; with C1, `inputAvailability` now disables on `turnActive`, so the nav lock follows automatically wherever it keys off the same selector. Confirm by searching:

Run: `cd apps/vscode-extension/webview-ui && grep -rn "inputAvailability\|disabled.*back\|newChat\|switchThread" src/components`
If nav controls read `inputAvailability(state).disabled` (or `state.turnActive`), no further change is needed. If they key only on `liveStatus`, add `|| state.turnActive` to their disable condition. Document which in the commit.

- [ ] **Step 5: Typecheck**

Run: `cd /Users/pradeepkumar/projects/AI\ editor/.worktrees/feat-agentic-chat-controller && npm run -w @ai-editor/editor-client build && npm run -w @ai-editor/vscode-extension typecheck`
Expected: clean (no stale-type errors — editor-client was rebuilt in B1/B2).

- [ ] **Step 6: Commit**

```bash
git add apps/vscode-extension/src/controller.ts apps/vscode-extension/src/extension.ts apps/vscode-extension/webview-ui/src/types.ts apps/vscode-extension/webview-ui/src/**/*.ts apps/vscode-extension/webview-ui/src/**/*.tsx
git commit -m "feat(extension): plumb turnActive into webview AppState; nav-lock follows input disable"
```

---

### Task D2: Stop posts `/stop`; live-resume re-subscribe on mount

**Files:**
- Modify: `apps/vscode-extension/src/controller.ts:1140-1142` (`stopActiveTurn`) and `switchChatThread`/mount + `pollThreadLiveState`
- Test: extend the extension controller test suite if present (`grep -rl "AiEditorController" src/**/*.test.ts`).

- [ ] **Step 1: Stop posts `/stop`**

Replace `stopActiveTurn` (`controller.ts:1140-1142`):

```typescript
  async stopActiveTurn(): Promise<void> {
    // Detached turns are not cancelled by disconnecting the SSE anymore — Stop is an
    // explicit POST /stop (a slimmer cousin of task /abort). Still abort the local SSE
    // reader so the relay loop unwinds promptly; the server-side cancel is the real stop.
    this.turnAbort?.abort();
    const threadId = this.activeThreadId;
    if (!threadId) return;
    try {
      await this.clientForChat().stopChatTurn(threadId);
    } catch (error) {
      if (this.isBenignConflict(error)) return;
      // A failed stop is non-fatal — the turn finishes on its own; log only.
      this.ui.showWarning(`Stop failed: ${formatError(error)}`);
    } finally {
      this.lastLiveSignature = null;
      void this.pollThreadLiveState();
    }
  }
```

`stopActiveTurn` is referenced in `extension.ts` as `() => controller.stopActiveTurn()` — now returns a Promise; update the arrow to `() => void controller.stopActiveTurn()` if the callback type is `() => void`.

- [ ] **Step 2: Live-resume — re-subscribe to `chat:{threadId}` on mount when a turn is active**

Add a guarded re-subscribe in `pollThreadLiveState`. After `this.latestLiveState = live;` and before the signature dedup, add:

```typescript
    this.latestLiveState = live;

    // Live-resume: a fresh webview (reload mid-turn) reconstructs the transcript from
    // the thread fetch, but the live overlay (streaming pills/chunks) died with the old
    // SSE. When /live reports an in-flight turn or a controller gate, re-subscribe to the
    // chat channel (subscribe-only — does NOT relaunch the turn) to resume the overlay
    // from the broadcaster's replay buffer onward. Idempotent via _liveResumeThreadId.
    const channelActive = live.turnActive || live.pendingGate?.kind === "mode"
      || live.pendingGate?.kind === "edit";
    if (channelActive && this._liveResumeThreadId !== threadId) {
      this._liveResumeThreadId = threadId;
      void this.resumeLiveOverlay(threadId);
    } else if (!channelActive && this._liveResumeThreadId === threadId) {
      this._liveResumeThreadId = null; // turn ended — allow a future resume
    }
```

Add the field + method (near `streamTurn`):

```typescript
  // Thread currently being live-resumed (channel re-subscribe) — idempotency guard.
  private _liveResumeThreadId: string | null = null;

  /**
   * Resume the live overlay for an in-flight controller turn after a webview reload.
   * Subscribe-only relay (no turn launch) over GET /v1/channels/{id}/stream; reuses
   * streamTurn's event rendering. Best-effort: events older than the 50-event replay
   * buffer (and everything after a backend restart) come from the reconstructed
   * transcript, not here.
   */
  private async resumeLiveOverlay(threadId: string): Promise<void> {
    try {
      this.turnAbort = new AbortController();
      await this.streamTurn(this.clientForChat().streamChannel(`chat:${threadId}`));
    } catch (error) {
      // A closed/empty channel is expected (turn already done) — clear the guard so a
      // later turn can resume, and let the /live poll keep driving durable state.
      this._liveResumeThreadId = null;
      if (!(error instanceof Error && error.name === "AbortError")) {
        // non-fatal
      }
    }
  }
```

Reset `_liveResumeThreadId = null` in `switchChatThread` and `newChatThread` (beside the existing `this.lastLiveSignature = null;`) so switching threads doesn't suppress a resume on the new thread.

- [ ] **Step 3: Typecheck + extension tests**

Run: `cd /Users/pradeepkumar/projects/AI\ editor/.worktrees/feat-agentic-chat-controller && npm run -w @ai-editor/vscode-extension typecheck && npm run -w @ai-editor/vscode-extension test`
Expected: clean typecheck; tests PASS (update any `ControllerUI` stub that lacks the new `sendLiveStatus` signature).

- [ ] **Step 4: Commit**

```bash
git add apps/vscode-extension/src/controller.ts apps/vscode-extension/src/extension.ts apps/vscode-extension/src/**/*.test.ts
git commit -m "feat(extension): Stop posts /stop; live-resume re-subscribes to chat channel on mount"
```

---

## FINAL VERIFICATION

- [ ] **Backend full suite**

Run: `cd services/agentd-py && pytest tests/ -q`
Expected: read the summary `FAILED`/passed line directly (never trust a piped exit code — CLAUDE.md). All green; the controller suite (`-k controller`) in particular.

- [ ] **TypeScript full suite + typecheck**

Run: `cd /Users/pradeepkumar/projects/AI\ editor/.worktrees/feat-agentic-chat-controller && npm run build && npm run test && npm run typecheck`
Expected: all green.

- [ ] **Lint/format the touched Python**

Run: `cd services/agentd-py && ruff check agentd/chat/controller.py agentd/api/routes.py agentd/chat/models.py && ruff format --check agentd/chat/controller.py agentd/api/routes.py`
Expected: clean.

---

## Spec coverage self-check

| Spec section | Task |
|---|---|
| §1 Detach the turn from SSE | A2 (`launch_turn`/`_run_turn`), A3 (`/message`), A4 (`/mode-decision`) |
| §1 clear gate at `handle_message` start | A2 |
| §2 Durable held-open EditGate | A3/A4 (detachment makes it durable for free) + A7 (restart orphan) |
| §3 In-flight guard (409) | A3, A4 |
| §4 `turn_active` via `/live` | A1 (field), A5 (route), B1 (FE schema/mapping) |
| §5 Rule 1 input availability | C1 |
| §6 Rule 2 one-shot decisions | C2 (ModeGate), existing one-shot guards (EditGate) |
| §7 Rule 3 navigation lock | D1 |
| §8 Rule 4 read-only safety | unchanged (no task) |
| §9 ModeGate component change | C2 |
| §10 Reload reconnect / live-resume | B2 (`streamChannel`), D2 |
| §11 Stop endpoint | A6 (backend), B2 (`stopChatTurn`), D2 (FE) |

**Deferred (spec non-goals / v1 risks):** message queueing while busy (§Non-goals); replay events older than the 50-event buffer (§Risks) — both intentionally out of scope.
