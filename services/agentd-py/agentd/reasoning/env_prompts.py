"""Prompts + JSON schema for the single draft_conventions LLM call.

The call takes a deterministic EcosystemProbe result and returns a compact
list of EnvEcosystemEntry-shaped dicts plus a short conventions_notes string.
One call per profile build; result is persisted in env_profile.json.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentd.env.probe import ProbeResult


DRAFT_CONVENTIONS_SYSTEM_PROMPT = """\
You are a build-system expert. The user gives you a deterministic probe of a
software workspace: discovered manifests, lockfiles, top-level dirs, and which
package managers / language runtimes are on PATH.

Decide for each ecosystem-scope:
- which package manager to use (uv vs pip; npm vs yarn vs pnpm; cargo; go)
- the exact install command (e.g. "uv sync", "npm ci")
- the project's interpreter or binary-runner path RELATIVE to the workspace
  root (e.g. "services/agentd-py/.venv/bin/python"); null if not applicable
- the test command (e.g. "pytest", "vitest run", "cargo test"); null if you
  cannot infer one
- the top ~20 declared dependencies (verbatim strings from the manifest)
- short notes about quirks for this scope

Prefer the manifest's evidence over PATH presence. Be concrete; avoid
hedging. If a scope has no clear PM (e.g. no lockfile and ambiguous
manifest), still pick one and explain in `notes`.

Output STRICTLY conforming to the response schema.
"""

DRAFT_CONVENTIONS_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "ecosystems": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ecosystem": {"type": "string", "enum": ["python", "node", "rust", "go"]},
                    "subdir": {"type": "string"},
                    "manifest_path": {"type": "string"},
                    "package_manager": {"type": "string"},
                    "install_command": {"type": "string"},
                    "interpreter_or_runner": {"type": ["string", "null"]},
                    "test_command": {"type": ["string", "null"]},
                    "declared_dependencies_top": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "notes": {"type": ["string", "null"]},
                },
                "required": [
                    "ecosystem", "subdir", "manifest_path", "package_manager",
                    "install_command", "interpreter_or_runner", "test_command",
                    "declared_dependencies_top", "notes",
                ],
            },
        },
        "conventions_notes": {"type": ["string", "null"]},
    },
    "required": ["ecosystems", "conventions_notes"],
}


def build_draft_conventions_payload(probe: "ProbeResult") -> dict:
    """Build the user payload for the LLM. Include rich context per project
    rule: raw manifest text, lockfiles, top-level dirs, runtimes/PMs on PATH,
    workspace tree."""
    return {
        "workspace_root": probe.workspace_root,
        "workspace_tree": probe.workspace_tree,
        "package_managers_on_path": probe.package_managers_on_path,
        "language_runtimes_on_path": probe.language_runtimes_on_path,
        "diagnostics": probe.diagnostics,
        "ecosystems": [
            {
                "ecosystem": e.ecosystem,
                "subdir": e.subdir,
                "manifest_path": e.manifest_path,
                "manifest_text": e.manifest_text,
                "top_level_dirs": e.top_level_dirs,
                "lockfiles_present": e.lockfiles_present,
            }
            for e in probe.ecosystems
        ],
    }
