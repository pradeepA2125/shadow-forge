# Controller UX interaction rules — input availability, one-shot actions & durable turns

> Brings the agentic ChatController to parity with the task pipeline's UX interaction
> rules (`2026-06-09-chat-ui-redesign.md` §"UX interaction rules — input availability &
> one-shot actions"), and makes controller turns + held-open gates durable across a
> reload by mirroring the task lifecycle. Date: 2026-06-17.

## Problem

The `2026-06-09` redesign defined four UX rules (input availability, one-shot decisions,
navigation lock, read-only safety) **derived from `/live` status** so they survive a
webview reload and make it impossible for the user to disrupt an in-flight flow. That
ruleset is keyed entirely on **task status**.

The `2026-06-15` ChatController introduced **thread-level** gates (`ModeGate`/`EditGate`
via `ChatThread.pending_controller_gate`) and a deliberately **soft-terminal ModeGate**
("keep typing to discuss/refine"), but never extended the UX rules to cover them.
Consequences, all confirmed by tracing the code:

1. **Controller gates have no status.** `resolve_thread_live` returns a `ThreadLiveState`
   with `status=None` for a controller gate (`live_state.py:134`). The FE
   `inputAvailability` selector keys on `liveStatus`, so a controller gate falls through
   the precedence table to the bottom row → **input enabled**.
   - A live turn (DECIDE streaming, or a held-open EditGate) is still disabled today, but
     only via the **ephemeral** `inputEnabled=false` flag — not durably.
   - A **ModeGate** (turn already ended, `inputEnabled=true`, `status=null`) → **enabled**,
     i.e. ambient typing during the gate. This is the soft-terminal behavior, but it leaves
     a stale gate clickable and is the "between turns input should not be enabled" violation.

2. **The controller turn is bound to the SSE request.** It runs inside the `/message`
   (or `/mode-decision`) request's `_run_agent` task, which `event_stream`'s `finally`
   **cancels on client disconnect** (`routes.py:1168`). A reload therefore *kills* the
   in-flight turn within ~15s (the ping interval). For a held-open **EditGate** this means
   the gate **vanishes** after reload: the cancel unwinds the suspended turn, whose
   `finally` clears `pending_controller_gate` (`controller.py:352`) and tears down the
   turn-shadow — the proposed-but-unaccepted edit is lost.
   - By contrast, a **task** runs *detached* (`asyncio.create_task(run_task)`,
     `routes.py:275`); `/stream-patch` is a separate subscriber. A reload drops the
     subscriber while the task keeps running, parked at `_pause_for_step_review` awaiting
     `_pending_step_decisions[task_id]`. That is exactly why `step_review` gates are
     durable across reload — and the pattern this design adopts for the controller.

## Goals

- A complete input-availability / one-shot / navigation ruleset for the controller,
  mirroring redesign Rules 1–4.
- **Input enabled only at true conversational terminals** (`answer`/`clarify`/idle);
  disabled during all work and all decisions ("between turns input is not enabled").
- Controller turns and held-open gates **durable across reload**, matching `step_review`.

## Non-goals (v1)

- Message **queueing** while busy (deferred; with input disabled there is nothing to queue).

## Design

### 1. Detach the controller turn from the SSE (mirror `run_task`)

`/message` and `/mode-decision` change from "the request *is* the work" to "the request
*launches and subscribes to* the work", exactly like `create_task` + `/stream-patch`:

- Register the thread in `ChatController._active_turns` **before** subscribing (closing the
  same race window the task route closes with `_running_tasks.add` at `routes.py:309`).
- Launch the turn as a **detached** `asyncio.create_task(...)` — not cancelled on disconnect.
- The route returns an SSE that **subscribes to the chat channel** and relays events until
  `chat_done`. On client disconnect: **unsubscribe only** (do NOT cancel the turn).
- The turn's `finally` discards the thread from `_active_turns`.

`ChatController._active_turns: dict[str, asyncio.Task]` is in-memory (a backend restart
correctly clears it — the orphaned turn is dead anyway). It mirrors the orchestrator's
`_running_tasks` and earns its keep three ways: the **in-flight guard** (§3), the **durable
input signal** (§4), and the **task handle** the stop endpoint cancels (§11). Membership
checks (`thread_id in _active_turns`) read identically whether it is a set or a dict.

`handle_message` also calls `set_controller_gate(thread_id, None)` at the **start** of a
turn, so a new turn can never leave a stale gate rendered, and a late decision on a
superseded card hits `gate is None` → `resolve_mode`/`resolve_edit` already no-op.
(A `clarify` sets no gate, so this is a no-op on the clarify→resume path — no conflict
with the EDIT-clarify resume.)

### 2. Durable held-open EditGate (= `step_review` parity)

With the turn detached, the EditGate becomes durable for free — it only died because the
cancel cleared it:

- The turn stays alive, parked in `_edit_decision_cb` awaiting `_pending_edit[thread_id]`.
- `pending_controller_gate(kind="edit")` persists in sqlite; `/live` renders the EditGate
  card after a reload; `POST /edit-decision` fires the in-memory future (which survives a
  webview reload because the backend stays up) → the turn resumes.

This is the same shape as `_pause_for_step_review` + `/step-decision`.

**Backend-restart caveat (held-open EditGate only).** A FE reload keeps the backend (and
its in-memory future) up, so the EditGate resolves normally. A **backend restart** wipes
the running turn and `_pending_edit`, but `pending_controller_gate` persists in sqlite — so
the EditGate card would render with no waiter behind it. `resolve_edit` must detect the
missing waiter (`thread_id not in _pending_edit`) and **clear the stale gate** +
write a breadcrumb ("Previous turn ended — please re-send your request") instead of
no-op'ing, so the UI unwedges (`turn_active` is already `False` post-restart → input
re-enables). The user re-issues the edit. This matches the task pipeline's documented
"orphan task after a backend restart → start a new task / resume" degradation. (The
**ModeGate** has no such caveat: its turn already completed before the gate, so
`resolve_mode` starts a fresh turn and needs no pre-existing waiter — fully durable across
a restart.)

### 3. In-flight guard

Once turns are detached, the SSE-cancel no longer prevents a double-start. A second
`/message` or `/mode-decision` while `_active_turns` holds the thread returns **409**
(benign — the FE already blocks it via disabled input; `isBenignConflict` swallows it).
No concurrent turns. Same posture as the task routes' `_in_flight_*` guards.

### 4. `turn_active` exposed via `/live`

- `ThreadLiveState.turn_active: bool = False` (`chat/models.py`).
- The `/live` route sets `turn_active = thread_id in getattr(_chat_agent, "_active_turns",
  set())` — **flag-tolerant**: the legacy `ChatAgent` (controller flag off) has no
  `_active_turns`, so it resolves to `False` and the legacy path is unaffected.
- `editor-client` `ThreadLiveState` Zod schema gains `turnActive` + snake↔camel mapping.

### 5. Rule 1 — input availability (controller), precedence top→bottom, first match wins

1. controller gate `kind=="edit"` → **disabled**, "Waiting for your decision on the card
   above"; only the EditGate is interactive.
2. controller gate `kind=="mode"` → **disabled**, "Choose how to proceed — or chat about it
   on the card"; only the ModeGate is interactive (incl. its in-card "chat about this" field).
3. `turn_active` (no gate) → **disabled**, "Agent is working…".
4. a task exists → **defer to the existing task-status Rule 1 table** (covers the
   `create_task` handoff aftermath: plan approval, executing, gates, review, terminal).
5. otherwise (no gate, no active turn, no task — i.e. `answer`/`clarify`/idle) → **enabled**.

Notes:
- During a *live* turn the existing ephemeral `inputEnabled=false` already disables input;
  `turn_active` is the **durable reload-window guard** (a fresh webview mounts with
  `inputEnabled=true` while the detached turn is still running). The genuinely-new disable
  is the **ModeGate** row (today it is enabled).
- The gate rows precede the generic `turn_active` row so the EditGate gets its specific
  placeholder + "only this card is interactive" semantics, not the generic "Agent is
  working…".
- FE: `inputAvailability(state)` is extended to also consume the controller **gate kind**
  and **`turnActive`**, plumbed from `/live` into `AppState`. The flag-off `ChatAgent` path
  has neither, so it falls through to existing behavior (no regression).

### 6. Rule 2 — every controller decision is one-shot

- **ModeGate**: the mode option buttons (`Edit inline` / `Plan as task` / `Explain`) and
  the in-card **"chat about this"** input resolve **together** on first action (optimistic
  `✓ Proceeding: …` / `✓ Discussing…`); a single POST fires; the card goes inert until the
  `/live` poll clears or remounts it.
- **"chat about this"** routes through `POST /message` — a fresh turn that supersedes the
  gate (which `handle_message` clears at start, §1).
- **EditGate**: `Accept`/`Reject` resolve together (StepGate-shaped; reject reason as today).
- Racing-click 409s stay swallowed as benign (`isBenignConflict`).

### 7. Rule 3 — navigation cannot orphan a controller turn

While `turn_active` or a controller gate is pending, lock `‹` back, history rows, and
`+ New Chat` ("A turn is in progress"). Now durable via `turn_active` rather than only the
ephemeral SSE flag.

### 8. Rule 4 — read-only affordances always safe

Unchanged: copy, expand/collapse, diff view, and search work in every state, including
viewing the diff on an EditGate.

### 9. ModeGate component change

Replace the trailing "keep typing to discuss/refine" hint with an inline **"chat about
this"** input field (one-shot per Rule 2). The main composer is disabled while the ModeGate
is up (Rule 1.2), so the in-card field is the only typing path — a controlled card action,
not ambient between-turn typing.

### 10. Reload reconnect (live-resume)

Durable state (gate / status / `turn_active`) comes from the 1s `/live` poll, and the
EditGate resolves via `/edit-decision`. For the **live overlay**, the webview re-subscribes
to the chat channel `chat:{thread_id}` on mount whenever `/live` reports `turn_active` or a
controller gate, using the **existing** `GET /channels/{channel_id}/stream` (`routes.py:363`
— a generic subscribe-only SSE; **no new endpoint**). This is the exact pattern tasks use
with `/stream-patch`: durable state from `/live`, live events from the channel. The
`editor-client` gains a `streamChannel(channelId)` method (reusing the SSE parsing already
behind `streamPatchEvents`/`sendChatMessage`).

**Two durability tiers** (they survive different failures):
- **Persisted transcript (sqlite)** — the durable source of truth. Survives **both** a FE
  reload *and* a backend restart. The webview always reconstructs the turn's history
  (messages, `tool_events`, diff records) from the thread fetch.
- **Channel re-subscribe (in-memory replay buffer, 50 events / `_REPLAY_BUFFER_SIZE`)** — a
  live overlay that only survives a **FE reload** (the backend, hence its replay buffer and
  running turn, must still be up). A backend restart wipes it; the transcript is then the
  sole recovery path.

So a FE reload mid-turn **resumes the live stream** (channel re-subscribe) on top of the
reconstructed transcript; events older than the 50-event buffer, and *everything* after a
backend restart, come from the transcript. The transcript fetch is therefore not just an
aged-out backstop — it is the cross-restart durability mechanism.

### 11. Stop endpoint (restores Stop once turns are detached)

Today Stop works by disconnecting the SSE, which cancels the request-bound turn. Detaching
the turn (§1) breaks that, so Stop is restored explicitly — a slimmer cousin of the task
`/abort`:

- `POST /chat/threads/{id}/stop` → `ChatController.stop_turn(thread_id)`.
- `stop_turn` looks up `_active_turns[thread_id]` (the detached `asyncio.Task`) and
  `cancel()`s it. The turn's existing `finally` does all the cleanup — discards from
  `_active_turns`, closes the turn-shadow (`_edit.close()`), and a held-open EditGate's
  `_edit_decision_cb` `finally` clears `pending_controller_gate` and pops `_pending_edit`.
  `stop_turn` then broadcasts `chat_done` so the SSE relay (and any subscriber) closes, and
  writes a `✗ Stopped` breadcrumb (durable record, like the abort breadcrumb).
- No-op + benign if no active turn (returns `ok=false` / 409, swallowed as benign) —
  mirrors the idempotent decision routes.
- Cancelling mid-edit: accepted edits were already instant-promoted; an in-progress
  (unaccepted) edit's turn-shadow is torn down — nothing partial reaches the real workspace.

The Stop button shows during a controller turn exactly as today (`showStop=true` when
`turn_active` and no task is active); it now posts `/stop` instead of relying on disconnect.

## Affected files

**Backend**
- `chat/controller.py` — `_active_turns` dict (thread→Task); register/clear in
  `handle_message` + `resolve_mode`; clear gate at `handle_message` start; `stop_turn`;
  `resolve_edit` clears a stale gate + breadcrumb when there is no live waiter (backend-
  restart orphan).
- `api/routes.py` — detach turns (`create_task`, no cancel-on-disconnect, subscribe-relay)
  on `/message` + `/mode-decision`; in-flight 409 guard; `POST /chat/threads/{id}/stop`;
  `/live` injects `turn_active` (flag-tolerant).
- `chat/models.py` — `ThreadLiveState.turn_active`.

**Frontend**
- `editor-client/src/contracts/task-contracts.ts` — `ThreadLiveState` schema + `turnActive`
  mapping.
- `editor-client/src/client/http-backend-client.ts` — `streamChannel(channelId)` method
  (reuse the SSE parsing behind `streamPatchEvents`/`sendChatMessage`) hitting
  `GET /v1/channels/{channelId}/stream`.
- `webview-ui/src/inputAvailability.ts` — controller precedence rows + new inputs; `AppState`
  plumbing of gate kind + `turnActive`.
- `webview-ui/src/components/messages/gates/ModeGate.tsx` — "chat about this" field;
  `LiveSlot`/cards inert while `turn_active`.
- `vscode-extension/src/controller.ts` — on mount, re-subscribe to `chat:{thread_id}` via
  `streamChannel` when `/live` reports `turn_active`/a controller gate (live-resume), with
  thread refetch as the aged-out backstop; nav lock on `turn_active`; Stop posts
  `POST /chat/threads/{id}/stop`.

## Testing

**Backend**
- A detached turn is **not** cancelled on a simulated client disconnect (turn completes).
- EditGate persists across a simulated reload and resolves: `/edit-decision` fires the
  future, the turn resumes and promotes.
- A second `/message` while `_active_turns` holds the thread → 409.
- `handle_message` clears a pending gate at turn start.
- `/live` exposes `turn_active`, and is flag-tolerant (legacy `ChatAgent` → `False`).
- `live_state`/route: an EditGate yields a disabled-input state.
- `stop_turn` cancels a detached turn: `_active_turns` is cleared, any gate cleared, a
  `✗ Stopped` breadcrumb persisted; `/stop` on an idle thread is a benign no-op.
- `resolve_edit` on a persisted EditGate with no live waiter (the backend-restart orphan)
  clears the stale gate + writes a breadcrumb instead of no-op'ing (the UI unwedges).

**Frontend**
- `inputAvailability` unit tests per precedence row (edit / mode / `turnActive` / task /
  enabled), plus a flag-off regression (no controller fields → existing behavior).
- ModeGate "chat about this": renders, submit posts exactly one message, one-shot.
- Live-resume: with `/live` reporting `turn_active`, the controller re-subscribes to
  `chat:{thread_id}` via `streamChannel` on mount and renders relayed events.
- Backend-restart orphan: a persisted EditGate with no `_pending_edit` waiter → `resolve_edit`
  clears the gate + writes a breadcrumb (does not no-op); `/live` then reports no gate.

## Risks / limitations (v1)

- No queueing of messages while busy.
- On reconnect, live events older than the 50-event broadcaster replay buffer are not
  re-delivered over the channel; they are reconstructed from the durable transcript on the
  thread fetch (the live overlay resumes from the buffer onward).
- A **backend restart** orphans an in-flight turn and a held-open EditGate's waiter (the
  conversation transcript survives in sqlite). `resolve_edit` clears the orphaned gate so
  the UI unwedges and the user re-issues — the same accepted degradation as an orphaned
  task after a backend restart. (ModeGate is unaffected — fully durable across a restart.)
