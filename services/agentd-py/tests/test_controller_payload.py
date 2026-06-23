import json

from agentd.chat.controller_prompts import (
    build_controller_step_payload,
    format_controller_system_prompt,
)


def test_system_prompt_carries_tools_not_retrieval():
    sp = format_controller_system_prompt(
        [{"name": "read_file", "description": "d", "parameters": {}}])
    assert "read_file" in sp
    assert "retrieval_seed" not in sp  # retrieval never in the system string


def test_payload_key_order_is_cache_stable():
    payload = build_controller_step_payload(
        {"goal": "g", "workspace_path": "/w", "retrieval_seed": {"neighbors": []}},
        history=[{"role": "assistant", "content": "{}"}],
        tool_definitions=[],
        phase="DECIDE",
    )
    keys = list(payload.keys())
    assert keys.index("retrieval_seed") < keys.index("conversation_history")
    assert keys[-1] == "budget_status"
    assert keys.index("instruction") < keys.index("budget_status")
    assert keys.index("conversation_history") < keys.index("instruction")
    # `goal` is the CURRENT turn's user message — it VARIES per turn, so it must sit
    # in the per-turn tail (after the append-only history), NOT the head (smoke #13).
    assert keys.index("conversation_history") < keys.index("goal")
    assert keys.index("goal") < keys.index("instruction")


def test_goal_change_preserves_cacheable_prefix():
    """Regression for smoke #13: changing only `goal` (the per-turn message) must NOT
    disturb any byte up through conversation_history, so TQP reuses the cached prefix.
    The byte-identity unit test compares the SAME turn across a restart; THIS guards
    the turn-over-turn axis it never exercised."""
    hist = [
        {"role": "assistant", "content": "{}"},
        {"role": "tool_result", "tool": "read_file", "content": "x"},
    ]
    ctx = {"workspace_path": "/w", "retrieval_seed": {"neighbors": ["a", "b"]}}
    sa = json.dumps(build_controller_step_payload(
        {**ctx, "goal": "short alpha"}, hist, [], phase="DECIDE"))
    sb = json.dumps(build_controller_step_payload(
        {**ctx, "goal": "a COMPLETELY different and longer second-turn message"},
        hist, [], phase="DECIDE"))
    # common prefix
    n = 0
    while n < min(len(sa), len(sb)) and sa[n] == sb[n]:
        n += 1
    shared = sa[:n]
    # the entire conversation_history must be inside the shared prefix; the only
    # divergence is the tail (goal/instruction/budget).
    assert "conversation_history" in shared
    assert json.dumps(hist) in shared
    # the bytes diverge only at/after the goal field (the tail) — never inside the head
    assert n >= sa.index('"goal"')
    assert sa.index('"goal"') > sa.index("conversation_history")


def test_edit_phase_instruction_hint():
    payload = build_controller_step_payload(
        {"goal": "g", "workspace_path": "/w"}, history=[], tool_definitions=[], phase="EDIT")
    assert "EDIT mode" in str(payload["instruction"])


def test_todo_status_lands_in_tail_when_present():
    payload = build_controller_step_payload(
        {"goal": "add features", "workspace_path": "/w",
         "todo_status": "2 items (1 done) — [✓ A] [☐ B]"},
        history=[], tool_definitions=[], phase="EDIT")
    assert payload.get("todo_status") == "2 items (1 done) — [✓ A] [☐ B]"
    keys = list(payload.keys())
    assert keys.index("todo_status") > keys.index("workspace_path")


def test_todo_status_omitted_when_blank():
    payload = build_controller_step_payload(
        {"goal": "g", "workspace_path": "/w", "todo_status": ""},
        history=[], tool_definitions=[], phase="EDIT")
    assert "todo_status" not in payload


def test_system_prompt_teaches_write_todos_and_policy():
    from agentd.chat.controller_prompts import CONTROLLER_SYSTEM_PROMPT
    p = CONTROLLER_SYSTEM_PROMPT
    assert "write_todos" in p
    assert "enumerate" in p.lower()
    assert "evidence" in p.lower()
