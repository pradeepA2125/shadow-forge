"""ControllerLoop — the agentic chat-turn ReAct loop (mirrors PlanningLoop).

Reads always hit the real workspace (no shadow-read flip). Terminal actions own
their own teardown. E1 implements explore (tool_call) + answer; clarify/propose_mode/
edit/submit_changes are added in E2/E3.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentd.reasoning.react_common import MALFORMED_CORRECTION, assistant_turn, dedup_key

if TYPE_CHECKING:
    from agentd.chat.controller_phase import ControllerPhaseSM
    from agentd.chat.edit_session import TurnEditSession
    from agentd.orchestrator.broadcaster import EventBroadcaster
    from agentd.reasoning.contracts import ReasoningEngine
    from agentd.tools.sources import AggregatingToolRegistry


@dataclass
class ControllerOutcome:
    kind: str  # "answer" | "clarify" | "propose_mode" | "submit_changes"
    text: str = ""
    payload: dict[str, object] | None = None
    history: list[dict[str, object]] | None = None


class ControllerLoop:
    def __init__(
        self,
        reasoning: ReasoningEngine,
        registry: AggregatingToolRegistry,
        broadcaster: EventBroadcaster,
        *,
        channel_id: str,
        phase_sm: ControllerPhaseSM,
        edit_session: TurnEditSession | None = None,
    ) -> None:
        self._reasoning = reasoning
        self._registry = registry
        self._broadcaster = broadcaster
        self._channel_id = channel_id
        self._sm = phase_sm
        self._edit = edit_session

    async def run(
        self,
        plan_context: dict[str, object],
        *,
        max_iters: int = 32,
        seed_history: list[dict[str, object]] | None = None,
        auto_accept_edits: bool = False,
    ) -> ControllerOutcome:
        tool_defs = [d.model_dump() for d in self._registry.definitions()]
        history = [dict(m) for m in seed_history] if seed_history else []
        seen: dict[str, int] = {}
        plan_context = {**plan_context, "max_iters": max_iters}
        for iteration in range(max_iters + 1):
            resp = await self._reasoning.create_controller_step(
                plan_context=plan_context, history=history,
                tool_definitions=tool_defs, phase=self._sm.phase,
            )
            atype = str(resp.get("type", ""))
            if atype == "answer":
                history.append(assistant_turn(resp))
                return ControllerOutcome(
                    kind="answer", text=str(resp.get("answer", "")), history=history)
            if atype not in self._sm.allowed_types():
                history.append(assistant_turn(resp))
                history.append({"role": "tool_result", "tool": "", "content": MALFORMED_CORRECTION})
                continue
            if atype == "tool_call":
                if iteration >= max_iters:
                    return ControllerOutcome(
                        kind="answer", text="(step budget exhausted)", history=history)
                tool = str(resp.get("tool", ""))
                raw_args = resp.get("args")
                args: dict[str, object] = raw_args if isinstance(raw_args, dict) else {}
                key = dedup_key(tool, args)
                if key in seen:
                    history.append({"role": "assistant", "content": "{}"})
                    history.append({
                        "role": "tool_result", "tool": tool,
                        "content": f"DUPLICATE BLOCKED (iter {seen[key]}); do differently.",
                    })
                    continue
                seen[key] = iteration + 1
                out = await self._registry.execute(tool, args)
                history.append(assistant_turn(resp))
                history.append({"role": "tool_result", "tool": tool, "content": out.output})
                continue
            if atype == "clarify":
                history.append(assistant_turn(resp))
                return ControllerOutcome(
                    kind="clarify", text=str(resp.get("question", "")), history=history)
            if atype == "propose_mode":
                history.append(assistant_turn(resp))
                return ControllerOutcome(kind="propose_mode", payload={
                    "plan_sketch": resp.get("plan_sketch", ""),
                    "recommended": resp.get("recommended"),
                    "reason": resp.get("reason", ""),
                    "options": resp.get("options", []),
                }, history=history)
            if atype == "edit":
                # EDIT phase is only reachable with an edit_session (phase SM gate).
                assert self._edit is not None
                raw_ops = resp.get("patch_ops")
                ops: list[dict[str, object]] = raw_ops if isinstance(raw_ops, list) else []
                diff = await self._edit.apply(ops)
                self._broadcaster.broadcast(self._channel_id, {
                    "type": "diff_ready",
                    "payload": {"diff_entries": [d.path for d in diff]},
                })
                # Per-edit review gate wired in Phase F; for now always accept
                # (auto_accept_edits selects the policy there). Instant-promote.
                await self._edit.accept()
                history.append(assistant_turn(resp))
                history.append({
                    "role": "tool_result", "tool": "edit",
                    "content": f"applied+promoted: {[d.path for d in diff]}",
                })
                continue
            if atype == "submit_changes":
                assert self._edit is not None
                await self._edit.close()
                history.append(assistant_turn(resp))
                return ControllerOutcome(
                    kind="submit_changes", text=str(resp.get("summary", "")), history=history)
            raise NotImplementedError(atype)
        return ControllerOutcome(kind="answer", text="(loop ended)", history=history)
