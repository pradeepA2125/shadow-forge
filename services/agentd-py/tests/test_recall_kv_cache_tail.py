from agentd.chat.controller_prompts import build_controller_step_payload


def test_recalled_memories_land_after_history_in_tail():
    plan_context = {
        "goal": "do X", "workspace_path": "/ws",
        "recalled_memories": ["- (semantic) patch ops in patch/engine.py"],
    }
    history = [{"role": "user", "content": "hi"}]
    payload = build_controller_step_payload(plan_context, history, [], phase="DECIDE")
    keys = list(payload.keys())
    assert "recalled_memories" in keys
    # KV-cache invariant: recalled lands AFTER the cached conversation_history (dynamic tail).
    assert keys.index("recalled_memories") > keys.index("conversation_history")


def test_no_recalled_key_when_empty():
    plan_context = {"goal": "do X", "workspace_path": "/ws", "recalled_memories": []}
    history = [{"role": "user", "content": "hi"}]
    payload = build_controller_step_payload(plan_context, history, [], phase="DECIDE")
    assert "recalled_memories" not in payload  # empty → omitted, no KV churn
