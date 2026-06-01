"""Test the scripted engine's draft_conventions and prompt builder."""
import pytest

from agentd.env.probe import EcosystemFacts, ProbeResult
from agentd.orchestrator.scripted_engine import ScriptedReasoningEngine
from agentd.reasoning.env_prompts import (
    DRAFT_CONVENTIONS_RESPONSE_SCHEMA,
    DRAFT_CONVENTIONS_SYSTEM_PROMPT,
    build_draft_conventions_payload,
)


def _probe_with_python(workspace: str = "/tmp/ws") -> ProbeResult:
    return ProbeResult(
        workspace_root=workspace,
        ecosystems=[EcosystemFacts(
            ecosystem="python",
            subdir="",
            manifest_path="pyproject.toml",
            manifest_text="[project]\nname=\"demo\"\nversion=\"0\"\ndependencies=[\"fastapi\"]\n",
            top_level_dirs=["agentd"],
            lockfiles_present=["uv.lock"],
        )],
        workspace_tree=["agentd", "tests"],
        package_managers_on_path={"uv": "/usr/local/bin/uv"},
        language_runtimes_on_path={"python3": "/usr/bin/python3"},
        diagnostics=[],
    )


def test_draft_conventions_payload_includes_manifest_text():
    probe = _probe_with_python()
    payload = build_draft_conventions_payload(probe)
    s = str(payload)
    assert "[project]" in s
    assert "fastapi" in s
    assert "uv.lock" in s


def test_draft_conventions_system_prompt_mentions_direct_interpreter_pattern():
    assert "interpreter" in DRAFT_CONVENTIONS_SYSTEM_PROMPT
    assert "install command" in DRAFT_CONVENTIONS_SYSTEM_PROMPT


def test_draft_conventions_response_schema_has_required_fields():
    schema = DRAFT_CONVENTIONS_RESPONSE_SCHEMA
    assert schema["type"] == "object"
    props = schema["properties"]
    assert "ecosystems" in props
    assert "conventions_notes" in props
    eco_props = props["ecosystems"]["items"]["properties"]
    for f in (
        "ecosystem", "subdir", "manifest_path", "package_manager",
        "install_command", "interpreter_or_runner", "test_command",
        "declared_dependencies_top", "notes",
    ):
        assert f in eco_props, f"missing {f} in entry schema"


@pytest.mark.asyncio
async def test_scripted_engine_draft_conventions_returns_canned_response():
    canned = {
        "ecosystems": [{
            "ecosystem": "python", "subdir": "", "manifest_path": "pyproject.toml",
            "package_manager": "uv", "install_command": "uv sync",
            "interpreter_or_runner": ".venv/bin/python", "test_command": "pytest",
            "declared_dependencies_top": ["fastapi"], "notes": None,
        }],
        "conventions_notes": "uses uv",
    }
    engine = ScriptedReasoningEngine(
        plan=None, patches=[], draft_conventions_responses=[canned],
    )
    out = await engine.draft_conventions(probe=_probe_with_python())
    assert out["ecosystems"][0]["package_manager"] == "uv"
    assert out["conventions_notes"] == "uses uv"


@pytest.mark.asyncio
async def test_scripted_engine_raises_when_no_canned_response():
    engine = ScriptedReasoningEngine(
        plan=None, patches=[], draft_conventions_responses=[],
    )
    with pytest.raises(RuntimeError, match="no draft_conventions response"):
        await engine.draft_conventions(probe=_probe_with_python())
