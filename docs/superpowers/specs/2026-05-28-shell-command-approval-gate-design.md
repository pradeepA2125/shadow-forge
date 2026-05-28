# Shell Command Approval Gate — Design

**Date:** 2026-05-28
**Status:** Approved (brainstorming) — pending implementation plan
**Branch:** `feat/agentic-planning-delta-replan`

## Problem

`run_command` is gated only by a static allowlist (`AI_EDITOR_SHELL_ALLOWLIST` = `pytest,npm,cargo,ruff,mypy,tsc,eslint`). This is the wrong security model:

- **Over-permits:** an allowlisted binary accepts arbitrary arguments — `python -c "<anything>"` or `pytest --co -p evil` is full code execution.
- **Under-permits:** legitimate commands the planner emits (e.g. `python -c "import agentd…"` import checks) are silently blocked, which destabilizes the verify phase (the model flails when its verify command can't run).

The user should be in control: unless they have granted blanket permission for a session/task, **every** command is surfaced for approval before it runs — Accept once / Accept & remember (this workspace) / Reject. This mirrors the Claude Code / Cursor command-permission model.

## Goals

- Default-deny: in `ask` mode, no command runs without an explicit (or remembered) user approval.
- Opt-in `allow_all` for trusted sessions/CI.
- "Accept & remember (this workspace)" with **user-chosen breadth** at approval time: exact command, a prefix rule, or the binary.
- Reuse the existing decision-gate infrastructure (`AWAITING_SCOPE_DECISION` / `AWAITING_VALIDATION_DECISION`).
- Rejecting one command must not kill the step or task — the agent adapts.

## Non-goals

- Surviving a backend restart mid-gate (in-memory future; same known limitation as the scope/validation gates).
- Gating commands outside the task execution `ToolLoop` (planning is read-only; inline changes skip verify — neither calls `run_command`).
- Unifying the three decision gates — explicitly **deferred** (see Future Work).

## Policy model

`AI_EDITOR_SHELL_POLICY` ∈ {`ask`, `allow_all`}, default `ask` — mirrors `AI_EDITOR_SCOPE_POLICY`. Per-task override via a `shell_policy` field on the task submission / resume payload (env/workspace default < per-task override).

The static `AI_EDITOR_SHELL_ALLOWLIST` is **removed**. In `ask` mode every command prompts until remembered; in `allow_all` mode nothing prompts.

## Gate mechanism (mirrors the scope gate)

New `TaskStatus.AWAITING_COMMAND_DECISION`. State-machine edges:
- `EXECUTING → AWAITING_COMMAND_DECISION` (pause)
- `AWAITING_COMMAND_DECISION → {EXECUTING, FAILED, ABORTED}` (resume / terminal)

`engine._build_command_approval_callback(task_id, …)` — twin of `_build_scope_callback` — is injected into `ToolRegistry`. `ToolRegistry.run_command` calls it **before** executing:

1. `allow_all` → approve silently.
2. Else `CommandRuleStore.matches(cmd_str)` (workspace rules) **or** per-task approved set → approve silently.
3. Else **pause**: create a future in `_pending_command_decisions[task_id]`, set `execution_state.pending_command_request`, `transition(AWAITING_COMMAND_DECISION)`, `broadcast("command_approval_requested", …)`, then `await asyncio.wait_for(future, timeout)`.

On resume: pop the future, clear `pending_command_request`, `transition(EXECUTING)`, and if `approve & remember` → persist the rule (workspace store) and add to the per-task set. Return the decision to `ToolRegistry`.

## Data model (`domain/models.py`)

- `CommandApprovalRequest{decision_id, command, args, cwd, step_id}` — persisted on `execution_state.pending_command_request`.
- `CommandDecision{approve: bool, remember: bool, scope: "exact" | "prefix" | "binary"}`.
- `CommandRule{type: "exact" | "prefix" | "binary", value: str, added_at: str}`.
- `execution_state.pending_command_request: CommandApprovalRequest | None`.
- `execution_state.approved_commands: list[CommandRule]` — per-task remembered approvals (so re-prompts don't happen within a task even before they're persisted).
- `shell_policy` field on submission/resume payload.

## Persistence — `CommandRuleStore`

Per-workspace JSON at `<workspace>/.ai-editor/approved-commands.json`:

```json
[{"type": "prefix", "value": "python -c", "added_at": "2026-05-28T…Z"}]
```

Methods:
- `load() -> list[CommandRule]`
- `matches(cmd_str: str) -> bool`
- `add(rule: CommandRule) -> None` (dedupes; atomic write — temp + rename)

Lives next to `index-snapshot.json` so it is inspectable, hand-editable, and gitignorable.

### Matching semantics — token-aware (NOT character `startswith`)

Both the command and the rule are tokenized with `shlex.split` (shell-aware). `rule.value` is stored as a `shlex.join` of the chosen tokens, so the round-trip is lossless even for quoted args. Let `cmd_tokens = shlex.split(command + " " + " ".join(args))` and `rule_tokens = shlex.split(rule.value)`. A rule matches when:

- `exact`: `cmd_tokens == rule_tokens` (full token-list equality).
- `prefix`: `cmd_tokens[:len(rule_tokens)] == rule_tokens` — a **leading token sublist**, compared token-by-token. This is deliberately *not* character `startswith`: rule `["cat", "/etc/passwd"]` matches `cat /etc/passwd -n` but **not** `cat /etc/password-store/secret`.
- `binary`: `basename(cmd_tokens[0]) == rule.value` (single token; `rule.value` is the basename, so `/usr/bin/pytest …` matches a `pytest` rule).

The approval UI builds the rule from the user-picked scope: `exact` → `shlex.join(all tokens)`; `prefix` → the user picks how many **leading tokens** to lock (the card shows the tokenized command with a cut point), stored as `shlex.join(leading tokens)`; `binary` → `basename(command)`.

**Why prefix needs care (surfaced in the UI):** a prefix always permits arbitrary continuation after the locked tokens, so it is a poor fit for payload flags — a `python -c` prefix would auto-approve `python -c "<any code>"`. For `-c`/`-e`-style payload commands, `exact` is the safe choice. The card renders a one-line preview — `auto-approves: <locked tokens> …` — so the breadth is explicit, and nudges toward `exact` when the next token is a payload flag.

## API + SSE

- SSE event `command_approval_requested` — payload `{decision_id, command, args, cwd, step_id}` — broadcast on the task channel. Chat-originated tasks surface it in the chat UI via the controller's task-stream subscription (`streamTaskIntoChatThread` → `streamPatch`), the same path the plan and other gate events already use. Note: that stream's keep-alive (added alongside this work) is what keeps the connection open through idle gaps so the event is delivered live.
- `POST /v1/tasks/{id}/command-decision` — body `CommandDecision` — resolves `_pending_command_decisions[id]`. Returns 409 if the task is not `AWAITING_COMMAND_DECISION`. Guarded by an in-flight set (same pattern as `_in_flight_feedback`).

## Client / UI

- `editor-client/contracts/task-contracts.ts`: add `command_approval_requested` to `StreamEvent`; add a `command_card` `ChatMessage` type; add `CommandDecisionSchema`; add `sendCommandDecision(taskId, decision)` to `HttpBackendClient`.
- `vscode-extension`: a chat card (reusing the scope/validation-card pattern) showing the command + args, a **scope radio** (this exact command / prefix / any `<binary>`), and `[Reject] [Accept once] [Accept & remember]`. Controller handles the event, posts the decision, and clears the card on resume. Non-chat tasks surface it in the review panel.

## Error handling

- **Reject** → callback returns a rejected decision → `run_command` returns a tool-result error string `"Command rejected by user: <cmd>"` → the agent reads it and adapts within the step (same shape as the old allowlist rejection). No step/task kill.
- **Timeout** `AI_EDITOR_COMMAND_DECISION_TIMEOUT_SEC` (default `0` = wait forever, like scope). On timeout → treat as reject.
- **Backend restart mid-gate** → in-memory future lost; task orphaned at `AWAITING_COMMAND_DECISION`. Documented limitation; recoverable via resume.
- **One pending decision per task** — the `ToolLoop` is sequential within a step, so at most one command awaits approval at a time.

## Enforcement scope

Task execution `ToolLoop` only. The planning registry is read-only (no `run_command`); inline changes skip the verify phase. No gate needed elsewhere.

## Removal of `AI_EDITOR_SHELL_ALLOWLIST`

Delete the env read in `tools/registry.py`, the `allowlist` param + check in `tools/shell.py`, and the schema mention. Remove it from `start-backend.sh`'s env block. Update `CLAUDE.md` (Key Configuration) to document `AI_EDITOR_SHELL_POLICY` in its place.

## Testing

- **`CommandRuleStore`** units: exact/prefix/binary matching; load/add; atomic persistence; dedupe.
- **Engine gate**: ask-mode pauses with no rule; `allow_all` skips; remembered rule (workspace + task) skips; reject → tool-result error; `approve & remember` persists the user-chosen scope.
- **Route**: `command-decision` resolves the future; 409 when not awaiting.
- **Integration**: a `ToolLoop` step that calls `run_command` → gate → approve → command executes; → reject → agent receives the error and proceeds.

## Components touched

**Backend:** `domain/models.py`, `domain/state_machine.py`, `orchestrator/engine.py`, `tools/registry.py`, `tools/shell.py`, new `tools/command_rules.py` (`CommandRuleStore`), `api/routes.py`.
**Client:** `editor-client/contracts/task-contracts.ts`, `editor-client/client/http-backend-client.ts`, `vscode-extension` controller + chat panel + `media/chat.js`.
**Config/docs:** `.env`, `scripts/stress/start-backend.sh`, `CLAUDE.md`.

## Future work (deferred)

Unify `AWAITING_SCOPE_DECISION`, `AWAITING_VALIDATION_DECISION`, and `AWAITING_COMMAND_DECISION` into one shared `_pause_for_decision(kind, payload, timeout)` scaffold (rule of three). Deferred on purpose — do it *after* this gate lands so the refactor does not risk the two already-working gates. Tracked in `docs/roadmap.md` → Deferred Refactors / Tech Debt.
