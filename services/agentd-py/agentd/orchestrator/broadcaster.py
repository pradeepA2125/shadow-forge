"""General-purpose SSE event broadcaster keyed by channel_id.

Replaces the old PatchEventBroadcaster (which was keyed by task_id only).
All existing callers that pass task_id still work — task_id is a valid channel_id.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from typing import Any

_REPLAY_BUFFER_SIZE = 50


class EventBroadcaster:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._replay: dict[str, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=_REPLAY_BUFFER_SIZE)
        )

    def subscribe(self, channel_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        for event in self._replay[channel_id]:
            queue.put_nowait(event)
        self._subscribers[channel_id].add(queue)
        return queue

    def unsubscribe(self, channel_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        subs = self._subscribers.get(channel_id)
        if subs is not None:
            subs.discard(queue)
            if not subs:
                self._subscribers.pop(channel_id, None)

    def broadcast(self, channel_id: str, event: dict[str, Any]) -> None:
        self._replay[channel_id].append(event)
        for queue in self._subscribers.get(channel_id, set()):
            queue.put_nowait(event)

    def clear_replay(self, channel_id: str) -> None:
        self._replay.pop(channel_id, None)


# Backward-compat alias — all existing callers using task_id as channel_id still compile.
PatchEventBroadcaster = EventBroadcaster
