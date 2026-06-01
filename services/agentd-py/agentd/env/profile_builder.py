"""Composes EcosystemProbe + draft_conventions LLM call → EnvProfile.

Includes a deterministic fast-path: when probe evidence is unambiguous (single
ecosystem-scope with a recognised lockfile pinning the PM), conventions are
synthesised without an LLM call. This shaves the ~30-60s qwen3.6 latency from
the first task on a workspace whenever the layout follows community defaults.
The LLM is still called when the fast-path can't decide.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from agentd.domain.models import EnvEcosystemEntry, EnvProfile
from agentd.env.probe import EcosystemFacts, EcosystemProbe, ProbeResult

logger = logging.getLogger(__name__)


class _Reasoner(Protocol):
    async def draft_conventions(self, *, probe: ProbeResult) -> dict: ...


def _path_join_subdir(subdir: str, rel: str) -> str:
    return f"{subdir}/{rel}" if subdir else rel


def _python_dep_strings_from_pyproject(text: str) -> list[str]:
    """Extract dependencies from a pyproject.toml — verbatim strings."""
    try:
        import tomllib
        data = tomllib.loads(text)
    except Exception:
        return []
    project = data.get("project", {}) if isinstance(data, dict) else {}
    deps = project.get("dependencies", []) if isinstance(project, dict) else []
    return [str(d) for d in deps if isinstance(d, str)][:20]


def _node_dep_strings_from_package_json(text: str) -> tuple[list[str], str | None]:
    """Returns (deps_top, inferred_test_command)."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return [], None
    deps_obj = {}
    if isinstance(data, dict):
        if isinstance(data.get("dependencies"), dict):
            deps_obj.update(data["dependencies"])
        if isinstance(data.get("devDependencies"), dict):
            deps_obj.update(data["devDependencies"])
    deps = [f"{k}@{v}" if isinstance(v, str) else k for k, v in deps_obj.items()][:20]
    scripts = data.get("scripts", {}) if isinstance(data, dict) else {}
    test_cmd: str | None = None
    if isinstance(scripts, dict) and isinstance(scripts.get("test"), str):
        # Prefer the explicit script; we'll invoke via "npm test" so the user's
        # script verbatim is honoured.
        test_cmd = "npm test"
    elif "vitest" in deps_obj:
        test_cmd = "vitest run"
    elif "jest" in deps_obj:
        test_cmd = "jest"
    return deps, test_cmd


def _synthesize_entry(facts: EcosystemFacts) -> EnvEcosystemEntry | None:
    """Try to build an EnvEcosystemEntry from probe facts alone.

    Returns None when the evidence is ambiguous and the LLM is needed.
    Convention sources:
      - Python: uv.lock → uv; poetry.lock → poetry; requirements*.txt → pip.
      - Node:   package-lock.json → npm ci; yarn.lock → yarn; pnpm-lock.yaml → pnpm.
      - Rust:   any Cargo.toml → cargo (one ecosystem-wide PM).
      - Go:     any go.mod → go.
    Interpreter / runner / test_command follow community defaults.
    """
    subdir = facts.subdir
    if facts.ecosystem == "python":
        if "uv.lock" in facts.lockfiles_present:
            pm, install = "uv", "uv sync"
        elif "poetry.lock" in facts.lockfiles_present:
            pm, install = "poetry", "poetry install"
        elif any(lf.startswith("requirements") for lf in facts.lockfiles_present):
            req = next(lf for lf in facts.lockfiles_present if lf.startswith("requirements"))
            pm, install = "pip", f"pip install -r {req}"
        else:
            return None  # ambiguous — defer to LLM
        return EnvEcosystemEntry(
            ecosystem="python", subdir=subdir, manifest_path=facts.manifest_path,
            package_manager=pm, install_command=install,
            interpreter_or_runner=_path_join_subdir(subdir, ".venv/bin/python"),
            test_command="pytest",
            declared_dependencies_top=_python_dep_strings_from_pyproject(facts.manifest_text),
            notes=None,
        )
    if facts.ecosystem == "node":
        if "package-lock.json" in facts.lockfiles_present:
            pm, install = "npm", "npm ci"
        elif "yarn.lock" in facts.lockfiles_present:
            pm, install = "yarn", "yarn install --frozen-lockfile"
        elif "pnpm-lock.yaml" in facts.lockfiles_present:
            pm, install = "pnpm", "pnpm install --frozen-lockfile"
        else:
            return None  # ambiguous — defer to LLM
        deps, test_cmd = _node_dep_strings_from_package_json(facts.manifest_text)
        return EnvEcosystemEntry(
            ecosystem="node", subdir=subdir, manifest_path=facts.manifest_path,
            package_manager=pm, install_command=install,
            interpreter_or_runner=_path_join_subdir(subdir, "node_modules/.bin"),
            test_command=test_cmd,
            declared_dependencies_top=deps,
            notes=None,
        )
    if facts.ecosystem == "rust":
        return EnvEcosystemEntry(
            ecosystem="rust", subdir=subdir, manifest_path=facts.manifest_path,
            package_manager="cargo", install_command="cargo fetch",
            interpreter_or_runner=None, test_command="cargo test",
            declared_dependencies_top=[],
            notes=None,
        )
    if facts.ecosystem == "go":
        return EnvEcosystemEntry(
            ecosystem="go", subdir=subdir, manifest_path=facts.manifest_path,
            package_manager="go", install_command="go mod download",
            interpreter_or_runner=None, test_command="go test ./...",
            declared_dependencies_top=[],
            notes=None,
        )
    return None


def _try_synthesize_all(probe: ProbeResult) -> list[EnvEcosystemEntry] | None:
    """Synthesise entries for ALL ecosystems, or return None if any is ambiguous."""
    out: list[EnvEcosystemEntry] = []
    for facts in probe.ecosystems:
        entry = _synthesize_entry(facts)
        if entry is None:
            return None
        out.append(entry)
    return out


def _normalise_interpreter_path(entry_dict: dict) -> dict:
    """W4 defense: if the LLM returned a runner path without the subdir prefix
    (e.g. '.venv/bin/python' for subdir='services/agentd-py'), prepend it.

    Skipped for absolute paths and when subdir is empty (workspace root)."""
    interp = entry_dict.get("interpreter_or_runner")
    subdir = entry_dict.get("subdir", "")
    if not interp or not subdir:
        return entry_dict
    if interp.startswith("/") or interp.startswith(f"{subdir}/"):
        return entry_dict
    entry_dict["interpreter_or_runner"] = f"{subdir}/{interp}"
    return entry_dict


class EnvProfileBuilder:
    """Build an EnvProfile via deterministic probe + one LLM call.

    Failure mode: any unrecoverable error in the LLM call yields a
    `bootstrap_needed=True` profile with a diagnostic; the caller (orchestrator)
    still persists it so the agent uses find_binary/init_workspace going forward.
    """

    def __init__(self, *, reasoner: _Reasoner) -> None:
        self._reasoner = reasoner

    async def build(self, workspace_root: Path) -> EnvProfile:
        probe = await EcosystemProbe.scan(workspace_root)
        now = datetime.now(timezone.utc)

        # No manifests → no LLM call.
        if not probe.ecosystems:
            return EnvProfile(
                workspace_root=probe.workspace_root,
                built_at=now,
                bootstrap_needed=True,
                ecosystems=[],
                conventions_notes=None,
                diagnostics=[*probe.diagnostics, "no manifests found in workspace"],
            )

        # W2 fast-path: when probe evidence is unambiguous, synthesise the
        # entries deterministically and skip the LLM (saves 30-60s per first
        # task on a workspace).
        synthesized = _try_synthesize_all(probe)
        if synthesized is not None:
            logger.info(
                "env profile: synthesised %d ecosystem(s) deterministically; skipped LLM",
                len(synthesized),
            )
            return EnvProfile(
                workspace_root=probe.workspace_root,
                built_at=now,
                bootstrap_needed=False,
                ecosystems=synthesized,
                conventions_notes="synthesised from probe lockfiles; no LLM call",
                diagnostics=list(probe.diagnostics),
            )

        # LLM call: try once + one retry on any exception.
        last_err: Exception | None = None
        decision: dict | None = None
        for _ in range(2):
            try:
                decision = await self._reasoner.draft_conventions(probe=probe)
                break
            except Exception as exc:  # noqa: BLE001 — message surfaced in diagnostic
                last_err = exc

        if decision is None:
            return EnvProfile(
                workspace_root=probe.workspace_root,
                built_at=now,
                bootstrap_needed=True,
                ecosystems=[],
                conventions_notes=None,
                diagnostics=[
                    *probe.diagnostics,
                    f"convention drafting failed: {last_err}",
                ],
            )

        # W4 defense: normalise interpreter_or_runner to subdir-prefixed form
        # in case the LLM emitted it relative to the subdir rather than the root.
        raw_entries = decision.get("ecosystems", [])
        normalised = [_normalise_interpreter_path(dict(e)) for e in raw_entries]
        entries = [EnvEcosystemEntry(**e) for e in normalised]
        return EnvProfile(
            workspace_root=probe.workspace_root,
            built_at=now,
            bootstrap_needed=False,
            ecosystems=entries,
            conventions_notes=decision.get("conventions_notes"),
            diagnostics=list(probe.diagnostics),
        )
