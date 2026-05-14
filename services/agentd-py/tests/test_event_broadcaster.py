from __future__ import annotations
import asyncio
import pytest
from agentd.orchestrator.broadcaster import EventBroadcaster, PatchEventBroadcaster


def test_patch_event_broadcaster_is_alias():
    assert PatchEventBroadcaster is EventBroadcaster


@pytest.mark.asyncio
async def test_broadcast_to_chat_channel_id():
    b = EventBroadcaster()
    queue = b.subscribe("chat:abc123")
    b.broadcast("chat:abc123", {"type": "chat_done", "payload": {}})
    event = queue.get_nowait()
    assert event == {"type": "chat_done", "payload": {}}
    b.unsubscribe("chat:abc123", queue)


@pytest.mark.asyncio
async def test_replay_buffer_on_late_subscribe():
    b = EventBroadcaster()
    b.broadcast("chan1", {"type": "e1", "payload": {}})
    b.broadcast("chan1", {"type": "e2", "payload": {}})
    queue = b.subscribe("chan1")
    assert [queue.get_nowait()["type"], queue.get_nowait()["type"]] == ["e1", "e2"]


@pytest.mark.asyncio
async def test_multiple_channels_are_isolated():
    b = EventBroadcaster()
    q1 = b.subscribe("chan1")
    q2 = b.subscribe("chan2")
    b.broadcast("chan1", {"type": "x", "payload": {}})
    assert not q1.empty()
    assert q2.empty()


def test_clear_replay_empties_buffer():
    b = EventBroadcaster()
    b.broadcast("chan", {"type": "e1", "payload": {}})
    b.clear_replay("chan")
    queue = b.subscribe("chan")
    assert queue.empty()
