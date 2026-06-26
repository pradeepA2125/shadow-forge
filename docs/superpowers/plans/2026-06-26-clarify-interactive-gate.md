# Clarify Interactive Gate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the controller's `clarify` action from a plain chat message into a live, durable Class-A gate card with model-authored answer options plus a free-text escape, auto-resuming the agent on selection and recording one combined `❓ q → a` breadcrumb.

**Architecture:** Mirror the existing `propose_mode → ModeGate → /mode-decision → resolve_mode` flow at every layer. The clarify action gains an `options` array; `_finish` routes clarify to a new `_present_clarify_choice` that sets `PendingGate(kind="clarify")`; a new `resolve_clarify` + `POST /clarify-decision` route re-enters the loop with the answer injected as the user reply. The EDIT-resume target moves from the `_edit_clarify_pending` side map into the gate payload (`resume_phase`).

**Tech Stack:** Python (FastAPI, Pydantic) backend `services/agentd-py`; TypeScript editor-client (Zod contracts); React webview-ui (vitest/jsdom); VS Code extension host.

## Global Constraints

- Controller path only — `AI_EDITOR_CHAT_CONTROLLER=1`. The legacy `ChatAgent` clarify path is untouched.
- Every gate `kind` MUST be added to the editor-client Zod enum in the same change, or `ThreadLiveStateSchema.parse()` throws on every 1s `/live` poll (CLAUDE.md `.min(1)`-class footgun).
- One question, flat option list, single answer. No nesting, no multi-select.
- Free-text "Something else…" escape is UI-appended; the model never authors it.
- Backward compatible: a clarify with zero `options` renders a free-text-only card.
- After editing `editor-client`, run `npm run -w @ai-editor/editor-client build` before `vscode-extension` typecheck (it types off `dist`, not source).
- Webview-ui tests/typecheck run **inside** `apps/vscode-extension/webview-ui` (jsdom config); running from repo root gives `document is not defined`.
- Commit message footer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

### Task 1: Backend — clarify schema gains `options`; loop carries them

**Files:**
- Modify: `services/agentd-py/agentd/chat/controller_prompts.py` (`_VARIANT_SPECS["clarify"]` ~L150; the clarify teaching block ~L243)
- Modify: `services/agentd-py/agentd/chat/controller_loop.py` (clarify branch L431-434)
- Test: `services/agentd-py/tests/test_controller_clarify_gate.py` (new)

**Interfaces:**
- Consumes: `ControllerLoop` ReAct loop; `ControllerOutcome(kind, text, payload, history, …)` dataclass (already has `payload: dict | None`).
- Produces: a `clarify` outcome whose `payload` is `{"question": str, "options": list[str]}`. Task 2 reads this payload.

- [ ] **Step 1: Write the failing test**

Create `services/agentd-py/tests/test_controller_clarify_gate.py`:

```python
import asyncio
import pytest
from agentd.chat.controller_loop import ControllerLoop, ControllerOutcome
from agentd.chat.controller_phase import ControllerPhaseSM


class _ClarifyEngine:
    """Scripted reasoning engine: emits one clarify with options, then stops."""
    def __init__(self):
        self.supports_oneof_grammar = False

    async def create_controller_step(self, *, system_instructions, user_payload, **_):
        return {
            "type": "clarify",
            "thought": "ambiguous target",
            "question": "Which pricing module?",
            "options": ["src/pricing.py", "billing/pricing.py"],
        }


def test_clarify_outcome_carries_question_and_options():
    loop = ControllerLoop(
        _ClarifyEngine(), registry=None, broadcaster=_NullBroadcaster(),
        channel_id="chat:t1", phase_sm=ControllerPhaseSM())
    outcome = asyncio.run(loop.run(plan_context={"goal": "fix pricing"}, seed_history=None))
    assert outcome.kind == "clarify"
    assert outcome.text == "Which pricing module?"
    assert outcome.payload == {
        "question": "Which pricing module?",
        "options": ["src/pricing.py", "billing/pricing.py"],
    }


class _NullBroadcaster:
    def broadcast(self, *_a, **_k): ...
    def clear_replay(self, *_a, **_k): ...
```

> NOTE: match the real `ControllerLoop.__init__`/`run` signatures when you open the file — adjust the harness (registry, todo_ledger, `run(...)` kwargs) to whatever the constructor actually requires. The assertion on `outcome.payload` is the contract.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/agentd-py && pytest tests/test_controller_clarify_gate.py::test_clarify_outcome_carries_question_and_options -v`
Expected: FAIL — `outcome.payload` is `None` (loop doesn't set it yet).

- [ ] **Step 3: Add `options` to the clarify schema**

In `controller_prompts.py`, change the clarify entry in `_VARIANT_SPECS`:

```python
    "clarify": {
        "required": ["question"],
        "properties": {
            "question": _STR,
            "options": {"type": "array", "items": _STR},
        },
    },
```

- [ ] **Step 4: Update the clarify teaching block**

In `controller_prompts.py`, find the clarify variant doc (~L243, `Variant — clarify …`) and append an options instruction. Replace the existing clarify example line with:

```python
Variant — clarify (you genuinely cannot proceed): {type, question, options}
  Emit 2-4 SHORT candidate answers in `options` — what you think the user most
  likely means. Never add a "something else"/free-text option yourself; the UI
  appends a free-text escape automatically. If you truly have no candidates,
  emit an empty `options` array.
  {"type":"clarify","thought":"ambiguous target","question":"Which pricing module?","options":["src/pricing.py","billing/pricing.py"]}
```

- [ ] **Step 5: Carry options into the outcome payload**

In `controller_loop.py`, the clarify branch (L431-434) currently is:

```python
            if atype == "clarify":
                history.append(assistant_turn(resp))
                return ControllerOutcome(
                    kind="clarify", text=str(resp.get("question", "")), history=history)
```

Replace with:

```python
            if atype == "clarify":
                history.append(assistant_turn(resp))
                raw_opts = resp.get("options")
                options = [str(o) for o in raw_opts] if isinstance(raw_opts, list) else []
                question = str(resp.get("question", ""))
                return ControllerOutcome(
                    kind="clarify", text=question, history=history,
                    payload={"question": question, "options": options})
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd services/agentd-py && pytest tests/test_controller_clarify_gate.py::test_clarify_outcome_carries_question_and_options -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add services/agentd-py/agentd/chat/controller_prompts.py \
        services/agentd-py/agentd/chat/controller_loop.py \
        services/agentd-py/tests/test_controller_clarify_gate.py
git commit -m "feat(controller): clarify action carries answer options

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Backend — `clarify` gate model, presentation, and `resolve_clarify`

**Files:**
- Modify: `services/agentd-py/agentd/chat/models.py` (`PendingGate.kind` literal L45)
- Modify: `services/agentd-py/agentd/chat/controller.py` (`_finish` L433-452; new `_present_clarify_choice`; new `resolve_clarify`; remove `_edit_clarify_pending` at L137, L280-285, L404-411; add `resume_phase` to clarify outcome in `_run_loop` tail ~L408)
- Test: `services/agentd-py/tests/test_controller_clarify_gate.py` (extend)

**Interfaces:**
- Consumes: clarify `ControllerOutcome.payload = {"question", "options"}` (Task 1); `PendingGate(kind, payload)`; `set_controller_gate`, `_write_breadcrumb`, `_run_loop`, `_finish`, `_seed_for`, `_step_review_by_thread`.
- Produces:
  - `PendingGate(kind="clarify", payload={"question": str, "options": list[str], "resume_phase": "EDIT" | None})`
  - `async def resolve_clarify(self, thread_id: str, answer: str, *, channel_id: str, goal: str) -> None` — Task 3's route calls this.

- [ ] **Step 1: Write the failing tests**

Append to `services/agentd-py/tests/test_controller_clarify_gate.py`:

```python
from agentd.chat.controller import ChatController
from agentd.chat.models import PendingGate
# Build a ChatController over an InMemory store + a stub broadcaster, like the
# other controller tests in tests/test_controller_*.py — reuse their fixture.


def _make_controller(tmp_path):
    # Mirror the construction used in tests/test_controller_mode_gate.py (or nearest).
    # Returns (controller, store, thread_id).
    ...


def test_present_clarify_sets_gate_not_chat_response(tmp_path):
    controller, store, thread_id = _make_controller(tmp_path)
    outcome = ControllerOutcome(
        kind="clarify", text="Which module?",
        payload={"question": "Which module?", "options": ["a.py", "b.py"]})
    asyncio.run(controller._present_clarify_choice(thread_id, f"chat:{thread_id}", outcome))
    gate = store.get_thread(thread_id).pending_controller_gate
    assert gate is not None and gate.kind == "clarify"
    assert gate.payload["question"] == "Which module?"
    assert gate.payload["options"] == ["a.py", "b.py"]


def test_resolve_clarify_writes_combined_breadcrumb(tmp_path):
    controller, store, thread_id = _make_controller(tmp_path)
    store.set_controller_gate(thread_id, PendingGate(
        kind="clarify",
        payload={"question": "Which module?", "options": ["a.py", "b.py"],
                 "resume_phase": None}))
    # Stub _run_loop so re-entry is a no-op terminal (we only assert the breadcrumb).
    async def _noop_loop(*a, **k):
        return ControllerOutcome(kind="answer", text="ok")
    controller._run_loop = _noop_loop  # type: ignore[assignment]
    asyncio.run(controller.resolve_clarify(
        thread_id, "a.py", channel_id=f"chat:{thread_id}", goal="fix pricing"))
    msgs = store.get_thread(thread_id).messages
    crumb = next(m for m in msgs if m.metadata.get("breadcrumb"))
    assert "Which module?" in crumb.content and "a.py" in crumb.content
    assert store.get_thread(thread_id).pending_controller_gate is None  # cleared


def test_resolve_clarify_idempotent_no_gate(tmp_path):
    controller, store, thread_id = _make_controller(tmp_path)
    # No pending clarify gate → no-op (no breadcrumb, no raise).
    asyncio.run(controller.resolve_clarify(
        thread_id, "a.py", channel_id=f"chat:{thread_id}", goal="g"))
    assert not store.get_thread(thread_id).messages


def test_resolve_clarify_empty_answer_noops(tmp_path):
    controller, store, thread_id = _make_controller(tmp_path)
    store.set_controller_gate(thread_id, PendingGate(
        kind="clarify", payload={"question": "Q", "options": [], "resume_phase": None}))
    asyncio.run(controller.resolve_clarify(
        thread_id, "   ", channel_id=f"chat:{thread_id}", goal="g"))
    # Gate stays (nothing resolved) — the card shouldn't submit blank, but defend.
    assert store.get_thread(thread_id).pending_controller_gate is not None
```

> NOTE: copy the exact `_make_controller` body from the nearest existing controller test fixture (e.g. `tests/test_controller_mode_gate.py`). Do not invent store/broadcaster wiring.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd services/agentd-py && pytest tests/test_controller_clarify_gate.py -v -k "present_clarify or resolve_clarify"`
Expected: FAIL — `_present_clarify_choice` / `resolve_clarify` don't exist (AttributeError).

- [ ] **Step 3: Add `"clarify"` to the gate model**

In `models.py` L45:

```python
    kind: Literal["command", "step", "scope", "validation", "mode", "edit", "clarify"]
```

- [ ] **Step 4: Stamp `resume_phase` into the clarify outcome in `_run_loop`**

In `controller.py`, the `_run_loop` tail currently sets/clears `_edit_clarify_pending` (L404-411). Replace that block:

```python
        # Mark/clear EDIT-clarify resume: a clarify emitted while in EDIT must resume
        # in EDIT on the user's reply. ...
        if outcome.kind == "clarify" and sm.phase == "EDIT":
            self._edit_clarify_pending.add(thread_id)
        else:
            self._edit_clarify_pending.discard(thread_id)
        return outcome
```

with (carry the resume target IN the outcome payload — no side map):

```python
        # A clarify raised mid-EDIT must resume in EDIT. Carry the resume target in the
        # gate payload (resolve_clarify reads it) rather than a side map keyed on thread.
        if outcome.kind == "clarify":
            payload = dict(outcome.payload or {})
            payload["resume_phase"] = "EDIT" if sm.phase == "EDIT" else None
            outcome = replace(outcome, payload=payload)
        return outcome
```

Add `from dataclasses import replace` to the imports if absent. Then delete the `_edit_clarify_pending` field declaration (L137) and its read at L280-285 — replace the `resume_phase` computation in `handle_message` (L280-285):

```python
        resume_phase = (
            "EDIT"
            if thread_id in self._edit_clarify_pending and self._orchestrator is not None
            else None
        )
        self._edit_clarify_pending.discard(thread_id)
```

with a no-op (handle_message no longer drives clarify resume — `resolve_clarify` does, via the gate):

```python
        # Clarify-resume is now driven by resolve_clarify (gate-carried resume_phase),
        # not by a fresh user message. A normal new message always re-enters DECIDE.
        resume_phase = None
```

- [ ] **Step 5: Route clarify to a gate in `_finish`; add `_present_clarify_choice`**

In `controller.py` `_finish` (L433), the `("answer", "clarify")` branch currently handles both. Split clarify out:

```python
        if outcome.kind == "answer":
            self._write_turn_message(thread_id, turn_id, outcome.text, outcome)
            self._broadcaster.broadcast(
                channel_id, {"type": "chat_response", "payload": {"chunk": outcome.text}})
            self._broadcaster.broadcast(channel_id, {"type": "chat_done", "payload": {}})
        elif outcome.kind == "clarify":
            await self._present_clarify_choice(thread_id, channel_id, outcome)
```

(Leave the `submit_changes` and `propose_mode` branches unchanged below.)

Add the method (clone of `_present_mode_choice`, after it ~L533):

```python
    async def _present_clarify_choice(
        self, thread_id: str, channel_id: str, outcome: ControllerOutcome,
    ) -> None:
        """Class-A gate: render the clarify question + options as a durable live card
        (/live → ClarifyGate), survives reload. No chat bubble — the question lives in
        the card; resolve_clarify writes the combined Q→A breadcrumb. Resolved by
        POST /clarify-decision."""
        metadata = self._turn_metadata(outcome)
        if metadata:
            self._store.append_message(thread_id, ChatMessage(
                role="agent", content="", metadata=metadata))
        self._store.set_controller_gate(
            thread_id, PendingGate(kind="clarify", payload=outcome.payload or {}))
        self._broadcaster.broadcast(channel_id, {"type": "chat_done", "payload": {}})
```

- [ ] **Step 6: Add `resolve_clarify`**

Add after `resolve_mode` (~L827), mirroring its idempotency guard + re-entry:

```python
    async def resolve_clarify(
        self, thread_id: str, answer: str, *, channel_id: str, goal: str,
    ) -> None:
        """Resolve the clarify gate (POST /clarify-decision). Clears the gate in place
        (Class-A), writes ONE combined `❓ q → a` breadcrumb, then re-enters the loop
        with the answer injected as the user's reply (EDIT if the clarify fired mid-edit,
        else DECIDE) — a fresh streamed turn, like resolve_mode's edit/explain re-entry."""
        answer = (answer or "").strip()
        thread = self._store.get_thread(thread_id)
        gate = thread.pending_controller_gate if thread is not None else None
        if gate is None or gate.kind != "clarify":
            logger.info("[controller] resolve_clarify no-op: no pending clarify gate (thread=%s)",
                        thread_id)
            return
        if not answer:
            logger.info("[controller] resolve_clarify no-op: empty answer (thread=%s)", thread_id)
            return
        question = str(gate.payload.get("question") or "")
        resume_phase = gate.payload.get("resume_phase")
        resume_phase = resume_phase if resume_phase in ("EDIT",) else None
        self._store.set_controller_gate(thread_id, None)
        self._write_breadcrumb(thread_id, channel_id, f"❓ {question} → {answer}")

        # Re-enter: the answer is the user's reply. effective_goal stays the original goal
        # (the answer rides as seed history, not as the goal).
        review = self._step_review_by_thread.get(thread_id)
        seed_history = (self._seed_for(thread_id) or []) + [
            {"role": "user", "content": answer}]
        turn_id = uuid4().hex
        outcome = await self._run_loop(
            thread_id, channel_id, goal, seed_history=seed_history,
            step_review=review, phase=resume_phase, turn_id=turn_id,
            edit_is_resume=(resume_phase == "EDIT"))
        await self._finish(
            thread_id, channel_id, outcome, step_review=review, turn_id=turn_id)
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd services/agentd-py && pytest tests/test_controller_clarify_gate.py -v`
Expected: PASS (all clarify tests)

- [ ] **Step 8: Run the controller suite for regressions (esp. EDIT-clarify resume)**

Run: `cd services/agentd-py && pytest tests/ -k controller -q`
Expected: PASS. If a test referenced `_edit_clarify_pending`, update it to the gate-driven flow.

- [ ] **Step 9: Commit**

```bash
git add services/agentd-py/agentd/chat/models.py \
        services/agentd-py/agentd/chat/controller.py \
        services/agentd-py/tests/test_controller_clarify_gate.py
git commit -m "feat(controller): clarify renders as Class-A gate + resolve_clarify

Routes clarify to a durable pending_controller_gate (kind=clarify) instead of
a chat bubble; resolve_clarify writes one combined Q->A breadcrumb and re-enters
the loop with the answer as the user reply. EDIT-resume moves from the
_edit_clarify_pending side map into the gate payload (resume_phase).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Backend route + editor-client client/interface/contract enum

**Files:**
- Modify: `services/agentd-py/agentd/api/routes.py` (new route after `post_mode_decision` L1234-1293)
- Modify: `apps/editor-client/src/contracts/task-contracts.ts` (gate enum L247; `BackendTaskClient.postClarifyDecision` L315)
- Modify: `apps/editor-client/src/client/http-backend-client.ts` (`postClarifyDecision` clone of `postModeDecision` L234)
- Test: `services/agentd-py/tests/test_controller_clarify_gate.py` (route test); `apps/editor-client` build

**Interfaces:**
- Consumes: `ChatController.resolve_clarify(thread_id, answer, *, channel_id, goal)` (Task 2).
- Produces:
  - `POST /v1/chat/threads/{thread_id}/clarify-decision` body `{answer: str}` → SSE stream.
  - `BackendTaskClient.postClarifyDecision(threadId: string, answer: string): AsyncIterable<StreamEvent>`.

- [ ] **Step 1: Add `"clarify"` to the editor-client gate enum**

In `apps/editor-client/src/contracts/task-contracts.ts` L247:

```typescript
  kind: z.enum(["command", "step", "scope", "validation", "mode", "edit", "clarify"]),
```

- [ ] **Step 2: Declare `postClarifyDecision` on the interface**

In the same file, after `postModeDecision` (L315):

```typescript
  postClarifyDecision(threadId: string, answer: string): AsyncIterable<StreamEvent>;
```

- [ ] **Step 3: Implement `postClarifyDecision` (clone of `postModeDecision`)**

In `http-backend-client.ts`, after `postModeDecision` (ends ~L230 of that method; place the new method adjacent). It is byte-identical to `postModeDecision` except the URL segment and the body key:

```typescript
  // Controller clarify gate: a STREAMED dispatch (re-enters the loop), consumed like
  // sendChatMessage — mirror of postModeDecision.
  async *postClarifyDecision(threadId: string, answer: string): AsyncIterable<StreamEvent> {
    const response = await this.fetchFn(
      `${this.options.baseUrl}/v1/chat/threads/${encodeURIComponent(threadId)}/clarify-decision`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ answer }),
      }
    );
    if (!response.ok) {
      throw new Error(`Clarify decision failed (${response.status}) for thread ${threadId}`);
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
          const trimmed = line.trim();
          if (!trimmed.startsWith("data:")) continue;
          const json = trimmed.slice("data:".length).trim();
          if (!json) continue;
          const event = parseStreamEvent(JSON.parse(json));
          if (event) yield event;
        }
      }
    } finally {
      reader.releaseLock();
    }
  }
```

> NOTE: match the exact tail of `postModeDecision` in the file (the line-parsing/`parseStreamEvent` helper names may differ). Copy that method verbatim and change only the URL segment (`clarify-decision`), the param (`answer`), the body (`{ answer }`), and the error string.

- [ ] **Step 4: Add the backend route (clone of `post_mode_decision`)**

In `routes.py`, after `post_mode_decision` (L1293), add a near-identical handler. Differences: path `clarify-decision`, reads `answer` from the body, calls `resolve_clarify(thread_id, answer, …)`:

```python
        @router.post("/chat/threads/{thread_id}/clarify-decision")
        async def post_clarify_decision(thread_id: str, request: dict) -> StreamingResponse:
            import asyncio as _asyncio_clar
            import json as _json_clar
            answer = request.get("answer", "")
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
                    _chat_agent.resolve_clarify(  # type: ignore[attr-defined]
                        thread_id, answer, channel_id=channel_id, goal=goal),
                    channel_id=channel_id,
                )
                queue = _chat_agent._broadcaster.subscribe(channel_id)

                async def detached_clarify_stream():
                    try:
                        while True:
                            try:
                                event = await _asyncio_clar.wait_for(
                                    queue.get(), timeout=15.0)
                            except TimeoutError:
                                yield ": ping\n\n"
                                continue
                            yield f"data: {_json_clar.dumps(event)}\n\n"
                            if event.get("type") in ("chat_done", "done"):
                                break
                    finally:
                        _chat_agent._broadcaster.unsubscribe(channel_id, queue)

                return StreamingResponse(
                    detached_clarify_stream(), media_type="text/event-stream")

            # --- legacy ChatAgent path has no clarify gate; degrade to an empty stream ---
            _chat_agent._broadcaster.clear_replay(channel_id)

            async def _empty():
                yield 'data: {"type": "chat_done", "payload": {}}\n\n'

            return StreamingResponse(_empty(), media_type="text/event-stream")
```

> NOTE: guard the route registration the same way `post_mode_decision` is (it lives inside the `if chat_agent is not None` block / the controller-handler section). Place it adjacent to `post_mode_decision`.

- [ ] **Step 5: Write the route test**

Append to `tests/test_controller_clarify_gate.py` a FastAPI route test using the test app factory (`agentd.chat.app_factory.build_app`), mirroring the nearest existing `mode-decision` route test:

```python
def test_clarify_decision_route_resolves_gate(tmp_path):
    # Build the test app (ScriptedReasoningEngine + InMemory store), seed a thread with a
    # pending clarify gate, POST /clarify-decision, assert the stream ends with chat_done
    # and the gate is cleared + a breadcrumb persisted. Copy the harness from the existing
    # mode-decision route test (tests/test_routes_*mode*).
    ...
```

> NOTE: if no `mode-decision` route test exists to copy, assert at the controller level instead (Task 2 already covers `resolve_clarify`); a thin smoke that the route is registered (`POST` returns 200 + an SSE content-type) is sufficient.

- [ ] **Step 6: Build editor-client + run backend test**

Run:
```bash
npm run -w @ai-editor/editor-client build
cd services/agentd-py && pytest tests/test_controller_clarify_gate.py -v && cd -
```
Expected: editor-client builds clean; pytest PASS.

- [ ] **Step 7: Commit**

```bash
git add services/agentd-py/agentd/api/routes.py \
        apps/editor-client/src/contracts/task-contracts.ts \
        apps/editor-client/src/client/http-backend-client.ts \
        services/agentd-py/tests/test_controller_clarify_gate.py
git commit -m "feat(controller): POST /clarify-decision route + client

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Frontend — ClarifyGate card + wiring

**Files:**
- Create: `apps/vscode-extension/webview-ui/src/components/messages/gates/ClarifyGate.tsx`
- Modify: `apps/vscode-extension/webview-ui/src/components/LiveSlot.tsx` (GateDispatch L24-39)
- Modify: `apps/vscode-extension/webview-ui/src/inputAvailability.ts` (mode branch L67-74)
- Modify: `apps/vscode-extension/src/chat-panel.ts` (handler type L21; ctor field L48; message branch L142)
- Modify: `apps/vscode-extension/src/controller.ts` (new `handleClarifyDecisionFromChat` after `handleModeDecisionFromChat` L993-998)
- Modify: `apps/vscode-extension/src/extension.ts` (ChatPanel ctor arg after L34)
- Test: `apps/vscode-extension/webview-ui/src/test/ClarifyGate.test.tsx` (new)

**Interfaces:**
- Consumes: `PendingGate` payload `{question: string, options: string[], resume_phase}`; `vscode.postMessage`; `BackendTaskClient.postClarifyDecision` (Task 3).
- Produces: webview message `{type: "clarifyDecision", threadId: string, answer: string}`; `ClarifyDecisionHandler = (threadId: string, answer: string) => Promise<void>`.

- [ ] **Step 1: Write the failing component test**

Create `apps/vscode-extension/webview-ui/src/test/ClarifyGate.test.tsx`:

```tsx
import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { ClarifyGate } from "../components/messages/gates/ClarifyGate";
import { vscode } from "../components/../vscodeApi";

vi.mock("../components/../vscodeApi", () => ({ vscode: { postMessage: vi.fn() } }));

describe("ClarifyGate", () => {
  beforeEach(() => vi.clearAllMocks());

  const payload = { question: "Which module?", options: ["a.py", "b.py"] };

  it("renders the question, each option, and a free-text row", () => {
    render(<ClarifyGate taskId="t1" payload={payload} />);
    expect(screen.getByText("Which module?")).toBeTruthy();
    expect(screen.getByText("a.py")).toBeTruthy();
    expect(screen.getByText("b.py")).toBeTruthy();
    expect(screen.getByPlaceholderText(/something else/i)).toBeTruthy();
  });

  it("posts clarifyDecision with the option text on click", () => {
    render(<ClarifyGate taskId="t1" payload={payload} />);
    fireEvent.click(screen.getByText("a.py"));
    expect(vscode.postMessage).toHaveBeenCalledWith({
      type: "clarifyDecision", threadId: "t1", answer: "a.py" });
  });

  it("posts clarifyDecision with the typed free text on submit", () => {
    render(<ClarifyGate taskId="t1" payload={payload} />);
    const input = screen.getByPlaceholderText(/something else/i);
    fireEvent.change(input, { target: { value: "c.py" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(vscode.postMessage).toHaveBeenCalledWith({
      type: "clarifyDecision", threadId: "t1", answer: "c.py" });
  });

  it("ignores a second pick (one-shot guard)", () => {
    render(<ClarifyGate taskId="t1" payload={payload} />);
    fireEvent.click(screen.getByText("a.py"));
    fireEvent.click(screen.getByText("b.py"));
    expect(vscode.postMessage).toHaveBeenCalledTimes(1);
  });
});
```

> NOTE: match the real import path of `vscode` (see how `ModeGate.tsx` imports `vscodeApi`) and adjust the mock path accordingly.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/vscode-extension/webview-ui && npx vitest run src/test/ClarifyGate.test.tsx`
Expected: FAIL — module `ClarifyGate` not found.

- [ ] **Step 3: Implement `ClarifyGate.tsx`**

```tsx
import { useState } from "react";
import { vscode } from "../../../vscodeApi";
import { CardShell } from "../../shared/CardShell";
import { BtnGhost, BtnPrimary } from "../../shared/buttons";

interface Props {
  /** Carries the threadId (controller gates have no task — LiveSlot passes activeTaskId ?? threadId). */
  taskId: string;
  payload: Record<string, unknown>;
}

function parseOptions(payload: Record<string, unknown>): string[] {
  if (!Array.isArray(payload.options)) return [];
  return (payload.options as unknown[]).map((o) => String(o)).filter((s) => s.length > 0);
}

/**
 * ClarifyGate — the controller's clarify gate (sibling of ModeGate). Shows the agent's
 * question, model-authored candidate answers as one-click options, and an always-present
 * free-text "Something else…" escape. Picking either posts clarifyDecision; the backend
 * resolves the gate, writes a combined Q→A breadcrumb, and auto-resumes the agent.
 */
export function ClarifyGate({ taskId, payload }: Props) {
  const question = String(payload.question ?? "");
  const options = parseOptions(payload);

  const [resolved, setResolved] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  function submit(answer: string) {
    if (resolved !== null) return; // one-shot guard, shared across all paths
    const text = answer.trim();
    if (!text) return;
    setResolved(text);
    vscode.postMessage({ type: "clarifyDecision", threadId: taskId, answer: text });
  }

  return (
    <CardShell
      icon="search"
      title="A quick question"
      subtitle={question || undefined}
      borderColor="var(--accent-brd)"
      headerTint="linear-gradient(180deg, var(--accent-bg), transparent)"
    >
      {resolved === null ? (
        <div className="flex flex-col gap-1.5 px-2.5 py-2 border-t border-border">
          {options.map((opt) => (
            <BtnGhost key={opt} onClick={() => submit(opt)}>
              {opt}
            </BtnGhost>
          ))}
          {/* free-text escape — always present */}
          <div className="flex items-center gap-1.5 pt-1">
            <input
              className="flex-1 rounded border border-border bg-surface-2 px-2 py-1 text-[12px] text-text-1 outline-none"
              placeholder="Something else… (type your answer)"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") submit(draft); }}
            />
            <BtnPrimary onClick={() => submit(draft)}>Send</BtnPrimary>
          </div>
        </div>
      ) : (
        <div className="px-2.5 py-2 text-[12px] text-text-3 border-t border-border">
          Answered: {resolved}
        </div>
      )}
    </CardShell>
  );
}
```

> NOTE: confirm `BtnGhost`/`BtnPrimary` prop API and the `vscodeApi` import path against `ModeGate.tsx`; adjust if they differ (e.g. children vs `label` prop). Use the same tokens ModeGate uses.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/vscode-extension/webview-ui && npx vitest run src/test/ClarifyGate.test.tsx`
Expected: PASS

- [ ] **Step 5: Wire LiveSlot + inputAvailability**

`LiveSlot.tsx` — add to `GateDispatch` switch (after the `mode` case L33) and import `ClarifyGate`:

```tsx
    case "clarify":
      return <ClarifyGate taskId={taskId} payload={payload} />;
```

`inputAvailability.ts` — extend the mode branch (L67) to cover clarify:

```typescript
  // Row 2: mode/clarify gate — the card (incl. its in-card field) is the input.
  if (liveGate?.kind === "mode" || liveGate?.kind === "clarify") {
    return {
      disabled: true,
      placeholder: "Answer on the card above",
      showStop: false,
      taskStop,
    };
  }
```

- [ ] **Step 6: Wire the extension host (chat-panel → controller → client)**

`chat-panel.ts`:
- Add the handler type (after `ModeDecisionHandler` L21):
  ```typescript
  export type ClarifyDecisionHandler = (threadId: string, answer: string) => Promise<void>;
  ```
- Add a constructor field (after `onModeDecision` L48):
  ```typescript
      private readonly onClarifyDecision: ClarifyDecisionHandler,
  ```
- Add the message branch (after the `modeDecision` branch L143):
  ```typescript
        } else if (m["type"] === "clarifyDecision") {
          p = this.onClarifyDecision(m["threadId"] as string, m["answer"] as string);
  ```

`controller.ts` — add after `handleModeDecisionFromChat` (L998):

```typescript
  /**
   * Resolve the controller clarify gate. A STREAMED dispatch: re-enters the loop with
   * the answer as the user reply, producing live chat events — consume via streamTurn
   * like a normal turn (mirror of handleModeDecisionFromChat).
   */
  async handleClarifyDecisionFromChat(threadId: string, answer: string): Promise<void> {
    const client = this.clientForChat();
    this.ui.setChatInputEnabled(false);
    this.turnAbort = new AbortController();
    await this.streamTurn(client.postClarifyDecision(threadId, answer));
  }
```

`extension.ts` — add the ctor arg in the `new ChatPanel(...)` call, in the same position as the field (after the `modeDecision` arg L34):

```typescript
    (threadId, answer) => controller.handleClarifyDecisionFromChat(threadId, answer),
```

- [ ] **Step 7: Typecheck + full webview-ui suite + build**

Run:
```bash
npm run -w @ai-editor/editor-client build
cd apps/vscode-extension/webview-ui && npx tsc --noEmit && npx vitest run && cd -
npm run -w @ai-editor/vscode-extension typecheck
cd apps/vscode-extension/webview-ui && npm run build && cd -
```
Expected: all clean; webview-ui suite green; fresh `dist/` written.

- [ ] **Step 8: Commit**

```bash
git add apps/vscode-extension/webview-ui/src/components/messages/gates/ClarifyGate.tsx \
        apps/vscode-extension/webview-ui/src/test/ClarifyGate.test.tsx \
        apps/vscode-extension/webview-ui/src/components/LiveSlot.tsx \
        apps/vscode-extension/webview-ui/src/inputAvailability.ts \
        apps/vscode-extension/webview-ui/dist \
        apps/vscode-extension/src/chat-panel.ts \
        apps/vscode-extension/src/controller.ts \
        apps/vscode-extension/src/extension.ts
git commit -m "feat(webview): ClarifyGate interactive card + wiring

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- §1 schema/options → Task 1. §2 gate render → Task 2 (`_present_clarify_choice`, `_finish`). §3 resolve_clarify (auto-resume + combined breadcrumb + EDIT-resume via resume_phase) → Task 2. §4 route → Task 3. §5 frontend (ClarifyGate, LiveSlot, inputAvailability, chat-panel, controller) → Task 4. §6 contract enum (PendingGate + editor-client) → Task 2 (model) + Task 3 (Zod). §7 tests → distributed across all tasks. ✅ All covered.

**Placeholder scan:** The `...` markers are explicit "copy the existing fixture" pointers for test harness wiring that must match real (unseen-until-open) constructors — each carries a NOTE naming the exact source to copy and the concrete assertion that defines done. No vague "add error handling"/"write tests for the above" steps; all production code is shown in full.

**Type consistency:** `resume_phase` carried in gate payload (Task 2) and read by `resolve_clarify` (Task 2). `postClarifyDecision(threadId, answer)` consistent across interface (Task 3 Step 2), impl (Step 3), controller.ts (Task 4 Step 6), and webview message `{type:"clarifyDecision", threadId, answer}` (Task 4 Steps 1/3/6). Gate `kind="clarify"` added in both Python (`models.py`, Task 2) and Zod (Task 3). `ClarifyDecisionHandler = (threadId, answer) => Promise<void>` matches the `extension.ts` arrow. ✅
