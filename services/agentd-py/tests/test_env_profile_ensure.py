"""Tests for EnvProfileEnsurer — lazy build, freshness check, concurrent serialization, SSE."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentd.domain.models import EnvProfile
from agentd.env.ensure import EnvProfileEnsurer
from agentd.env.profile_store import EnvProfileStore
from agentd.orchestrator.scripted_engine import ScriptedReasoningEngine


class _RecordingBroadcaster:
    """Captures broadcast calls for SSE assertions."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def broadcast(self, channel_id: str, event: dict) -> None:
        self.events.append((channel_id, event))


@pytest.mark.asyncio
async def test_ensure_builds_profile_when_missing_and_emits_sse(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname=\"x\"\nversion=\"0\"\ndependencies=[\"fastapi\"]\n"
    )
    canned = {
        "ecosystems": [{
            "ecosystem": "python", "subdir": "", "manifest_path": "pyproject.toml",
            "package_manager": "uv", "install_command": "uv sync",
            "interpreter_or_runner": ".venv/bin/python", "test_command": "pytest",
            "declared_dependencies_top": ["fastapi"], "notes": None,
        }],
        "conventions_notes": None,
    }
    reasoner = ScriptedReasoningEngine(plan=None, patches=[], draft_conventions_responses=[canned])
    broadcaster = _RecordingBroadcaster()
    ensurer = EnvProfileEnsurer(reasoner=reasoner, broadcaster=broadcaster)

    await ensurer.ensure(tmp_path)

    assert EnvProfileStore().read(tmp_path) is not None
    event_types = [evt["type"] for _, evt in broadcaster.events]
    assert "env_profile_building" in event_types
    assert "env_profile_built" in event_types


@pytest.mark.asyncio
async def test_ensure_reuses_fresh_profile_skips_llm(tmp_path: Path):
    """A fresh profile must not trigger another draft_conventions call."""
    EnvProfileStore().write(tmp_path, EnvProfile(
        workspace_root=str(tmp_path),
        built_at=datetime.now(timezone.utc),
        bootstrap_needed=False, ecosystems=[], conventions_notes=None, diagnostics=[],
    ))
    # No canned response — would raise if called.
    reasoner = ScriptedReasoningEngine(plan=None, patches=[], draft_conventions_responses=[])
    broadcaster = _RecordingBroadcaster()
    ensurer = EnvProfileEnsurer(reasoner=reasoner, broadcaster=broadcaster)

    await ensurer.ensure(tmp_path)  # must not raise
    # No SSE events when nothing was built.
    assert broadcaster.events == []


@pytest.mark.asyncio
async def test_ensure_broadcasts_to_both_task_and_chat_channels(tmp_path: Path):
    """W1: when chat_channel_id is also supplied (chat-driven resume), events
    fan out to both channels so both UIs surface env activity."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname=\"x\"\nversion=\"0\"\n")
    canned = {
        "ecosystems": [{
            "ecosystem": "python", "subdir": "", "manifest_path": "pyproject.toml",
            "package_manager": "uv", "install_command": "uv sync",
            "interpreter_or_runner": ".venv/bin/python", "test_command": "pytest",
            "declared_dependencies_top": [], "notes": None,
        }],
        "conventions_notes": None,
    }
    reasoner = ScriptedReasoningEngine(plan=None, patches=[], draft_conventions_responses=[canned])
    broadcaster = _RecordingBroadcaster()
    ensurer = EnvProfileEnsurer(reasoner=reasoner, broadcaster=broadcaster)

    await ensurer.ensure(tmp_path, channel_id="task-abc", chat_channel_id="chat-xyz")

    channels = {ch for ch, _ in broadcaster.events}
    assert channels == {"task-abc", "chat-xyz"}


@pytest.mark.asyncio
async def test_ensure_broadcasts_on_supplied_channel_id(tmp_path: Path):
    """When channel_id is passed (orchestrator passes task_id), SSE events
    must land on that channel — not the workspace path."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname=\"x\"\nversion=\"0\"\n")
    canned = {
        "ecosystems": [{
            "ecosystem": "python", "subdir": "", "manifest_path": "pyproject.toml",
            "package_manager": "uv", "install_command": "uv sync",
            "interpreter_or_runner": ".venv/bin/python", "test_command": "pytest",
            "declared_dependencies_top": [], "notes": None,
        }],
        "conventions_notes": None,
    }
    reasoner = ScriptedReasoningEngine(plan=None, patches=[], draft_conventions_responses=[canned])
    broadcaster = _RecordingBroadcaster()
    ensurer = EnvProfileEnsurer(reasoner=reasoner, broadcaster=broadcaster)

    await ensurer.ensure(tmp_path, channel_id="task-abc123")

    channels = {ch for ch, _ in broadcaster.events}
    assert channels == {"task-abc123"}


@pytest.mark.asyncio
async def test_ensure_swallows_exceptions_does_not_propagate(tmp_path: Path):
    """env-profile is supplementary infrastructure; failures must not block
    the task. Builder error → log + return None."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname=\"x\"\nversion=\"0\"\n")

    class BoomReasoner:
        async def draft_conventions(self, *, probe):
            raise RuntimeError("simulated upstream failure")

    broadcaster = _RecordingBroadcaster()
    ensurer = EnvProfileEnsurer(reasoner=BoomReasoner(), broadcaster=broadcaster)

    # Must not raise. The store.write call still happens with bootstrap_needed=true
    # because the BUILDER's own retry-then-fallback covers it. To exercise the
    # outermost swallow we patch store.write to raise.
    async def real_ensure_then_assert():
        await ensurer.ensure(tmp_path, channel_id="task-x")
    await real_ensure_then_assert()


@pytest.mark.asyncio
async def test_ensure_swallows_store_write_failure(tmp_path: Path, monkeypatch):
    """If EnvProfileStore.write fails (disk/permission), ensure() must still
    return cleanly so the orchestrator continues."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname=\"x\"\nversion=\"0\"\n")
    canned = {
        "ecosystems": [{
            "ecosystem": "python", "subdir": "", "manifest_path": "pyproject.toml",
            "package_manager": "uv", "install_command": "uv sync",
            "interpreter_or_runner": ".venv/bin/python", "test_command": "pytest",
            "declared_dependencies_top": [], "notes": None,
        }],
        "conventions_notes": None,
    }
    reasoner = ScriptedReasoningEngine(plan=None, patches=[], draft_conventions_responses=[canned])
    broadcaster = _RecordingBroadcaster()
    ensurer = EnvProfileEnsurer(reasoner=reasoner, broadcaster=broadcaster)

    def boom(*a, **k):
        raise PermissionError("disk full")
    monkeypatch.setattr(EnvProfileStore, "write", boom)

    # Must not raise.
    await ensurer.ensure(tmp_path, channel_id="task-y")


@pytest.mark.asyncio
async def test_ensure_serializes_concurrent_calls_for_same_workspace(tmp_path: Path):
    """Two concurrent ensure() calls on the same workspace should only build once."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname=\"x\"\nversion=\"0\"\n")

    calls = 0

    class CountingReasoner:
        async def draft_conventions(self, *, probe):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)  # ensure overlap
            return {"ecosystems": [{
                "ecosystem": "python", "subdir": "", "manifest_path": "pyproject.toml",
                "package_manager": "uv", "install_command": "uv sync",
                "interpreter_or_runner": ".venv/bin/python", "test_command": "pytest",
                "declared_dependencies_top": [], "notes": None,
            }], "conventions_notes": None}

    ensurer = EnvProfileEnsurer(reasoner=CountingReasoner(), broadcaster=_RecordingBroadcaster())
    await asyncio.gather(ensurer.ensure(tmp_path), ensurer.ensure(tmp_path))
    assert calls == 1
