"""EnvProfileEnsurer — lazy build + freshness check + concurrent serialization.

Owned by AgentOrchestrator. Called once at task start to make sure the workspace
has a usable env_profile.json. The ensurer is workspace-keyed; concurrent first
tasks on the same workspace wait on a per-workspace asyncio lock.

SSE events:
- env_profile_building — fires when a build starts
- env_profile_built    — fires after the profile is written
No event when the profile is already fresh (the common case).

Events broadcast on the supplied channel_id (typically the task_id, so the UI
can subscribe via the task's stream-patch endpoint). When channel_id is None,
events fall back to the workspace_root path — useful for non-task callers
(e.g. the workspace registration route).

ensure() never propagates exceptions: env-profile is supplementary
infrastructure. If the probe or store fails, the agent still runs and falls
back to find_binary/init_workspace via the existing teaching block.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Protocol

from agentd.env.profile_builder import EnvProfileBuilder
from agentd.env.profile_store import EnvProfileStore

logger = logging.getLogger(__name__)


class _Reasoner(Protocol):
    async def draft_conventions(self, *, probe: object) -> dict: ...


class _Broadcaster(Protocol):
    def broadcast(self, channel_id: str, event: dict) -> None: ...


class EnvProfileEnsurer:
    def __init__(
        self,
        *,
        reasoner: _Reasoner,
        broadcaster: _Broadcaster,
        store: EnvProfileStore | None = None,
    ) -> None:
        self._reasoner = reasoner
        self._broadcaster = broadcaster
        self._store = store or EnvProfileStore()
        self._locks: dict[str, asyncio.Lock] = {}

    async def ensure(
        self,
        workspace_root: Path,
        *,
        channel_id: str | None = None,
        chat_channel_id: str | None = None,
    ) -> None:
        try:
            workspace_key = str(workspace_root.resolve())
            # Broadcast to every distinct channel the caller cares about so the
            # chat panel (chat channel) AND the task review-panel (task channel)
            # both see env_profile_* events during a chat-driven resume.
            sse_channels = {channel_id or workspace_key}
            if chat_channel_id:
                sse_channels.add(chat_channel_id)
            lock = self._locks.setdefault(workspace_key, asyncio.Lock())
            async with lock:
                if not self._store.is_stale(workspace_root):
                    return

                for ch in sse_channels:
                    self._broadcaster.broadcast(ch, {
                        "type": "env_profile_building",
                        "payload": {"workspace_root": workspace_key},
                    })

                builder = EnvProfileBuilder(reasoner=self._reasoner)
                profile = await builder.build(workspace_root)
                self._store.write(workspace_root, profile)

                for ch in sse_channels:
                    self._broadcaster.broadcast(ch, {
                        "type": "env_profile_built",
                        "payload": {
                            "ecosystems_count": len(profile.ecosystems),
                            "bootstrap_needed": profile.bootstrap_needed,
                        },
                    })
        except Exception as exc:  # noqa: BLE001 — supplementary infra; never block the task
            logger.warning(
                "env profile ensure failed (continuing with fallback): %s",
                exc,
                exc_info=True,
            )
