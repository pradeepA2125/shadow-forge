"""Tests for /v1/workspaces/env-profile GET + POST routes."""
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_get_returns_404_when_no_profile(tmp_path: Path):
    from agentd.chat.app_factory import build_app
    app = build_app(str(tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/v1/workspaces/env-profile", params={"workspace": str(tmp_path)})
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_post_builds_profile_then_get_returns_it(tmp_path: Path):
    from agentd.chat.app_factory import build_app
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
    app = build_app(str(tmp_path), draft_conventions_responses=[canned])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/workspaces/env-profile", params={"workspace": str(tmp_path)})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ecosystems"][0]["package_manager"] == "uv"

        r2 = await c.get("/v1/workspaces/env-profile", params={"workspace": str(tmp_path)})
        assert r2.status_code == 200
        assert r2.json()["ecosystems"][0]["install_command"] == "uv sync"


@pytest.mark.asyncio
async def test_post_on_bare_workspace_returns_bootstrap_needed(tmp_path: Path):
    """No manifests → no LLM call required → bootstrap_needed=true."""
    from agentd.chat.app_factory import build_app
    app = build_app(str(tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/workspaces/env-profile", params={"workspace": str(tmp_path)})
        assert r.status_code == 200
        assert r.json()["bootstrap_needed"] is True


@pytest.mark.asyncio
async def test_post_rejects_nonexistent_workspace(tmp_path: Path):
    from agentd.chat.app_factory import build_app
    app = build_app(str(tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/v1/workspaces/env-profile",
            params={"workspace": "/nonexistent/path/xyz-12345"},
        )
        assert r.status_code == 400
