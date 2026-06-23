from agentd.chat.todo_ledger import TodoItem, TodoLedger


def test_pending_excludes_done_blocked_cancelled():
    led = TodoLedger()
    led.replace([
        TodoItem("Enemies", "done"),
        TodoItem("Jump", "in_progress"),
        TodoItem("Timer", "pending"),
        TodoItem("Sound", "blocked", note="needs audio asset"),
        TodoItem("Old", "cancelled"),
    ])
    # blocked + cancelled + done are NOT pending -> a blocked item cannot deadlock the gate
    assert [i.title for i in led.pending()] == ["Jump", "Timer"]


def test_render_includes_count_and_glyphs():
    led = TodoLedger()
    led.replace([TodoItem("A", "done"), TodoItem("B", "pending"), TodoItem("C", "blocked")])
    out = led.render()
    assert "3 items" in out and "(1 done)" in out
    assert "A" in out and "B" in out and "C" in out


def test_render_empty_is_blank():
    assert TodoLedger().render() == ""


def test_json_roundtrip_preserves_status_and_note():
    led = TodoLedger()
    led.replace([TodoItem("A", "blocked", note="why"), TodoItem("B", "cancelled")])
    back = TodoLedger.from_json(led.to_json())
    assert [(i.title, i.status, i.note) for i in back.items] == [
        ("A", "blocked", "why"), ("B", "cancelled", "")]


def test_from_json_none_is_empty():
    assert TodoLedger.from_json(None).items == []
