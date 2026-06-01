"""Deterministic ecosystem probe — pure filesystem, no LLM, no decisions."""
from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# TOML allows whitespace around the `=` between key and value, so any string-
# matching on a build-backend line must tolerate both `build-backend="x"` and
# `build-backend = "x"`. Pre-compiled here so _diagnose stays cheap.
_SETUPTOOLS_BUILD_BACKEND_RE = re.compile(
    r'build-backend\s*=\s*"setuptools\.build_meta"'
)


_MANIFEST_TO_ECOSYSTEM: dict[str, str] = {
    "pyproject.toml": "python",
    "package.json": "node",
    "Cargo.toml": "rust",
    "go.mod": "go",
}

_LOCKFILES_BY_ECOSYSTEM: dict[str, tuple[str, ...]] = {
    "python": ("uv.lock", "poetry.lock", "requirements.txt", "Pipfile.lock"),
    "node": ("package-lock.json", "yarn.lock", "pnpm-lock.yaml"),
    "rust": ("Cargo.lock",),
    "go": ("go.sum",),
}

_EXCLUDE_DIRS = frozenset({
    ".git", ".venv", "venv", ".env", "node_modules",
    "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache",
    "target", "dist", "build", ".tox", ".nox",
    ".agentd", ".ai-editor", ".worktrees", ".tmp",
})

_MAX_DEPTH = 3
_MAX_MANIFEST_BYTES = 64 * 1024


@dataclass
class EcosystemFacts:
    ecosystem: str
    subdir: str
    manifest_path: str
    manifest_text: str
    top_level_dirs: list[str]
    lockfiles_present: list[str]


@dataclass
class ProbeResult:
    workspace_root: str
    ecosystems: list[EcosystemFacts] = field(default_factory=list)
    workspace_tree: list[str] = field(default_factory=list)
    package_managers_on_path: dict[str, str] = field(default_factory=dict)
    language_runtimes_on_path: dict[str, str] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)


class EcosystemProbe:
    """Deterministic workspace probe. No LLM. Returns facts only."""

    @classmethod
    async def scan(cls, workspace_root: Path) -> ProbeResult:
        workspace_root = workspace_root.resolve()
        result = ProbeResult(workspace_root=str(workspace_root))

        manifests = cls._walk_manifests(workspace_root)
        for manifest_abs in manifests:
            ecosystem = _MANIFEST_TO_ECOSYSTEM[manifest_abs.name]
            rel_manifest = str(manifest_abs.relative_to(workspace_root))
            rel_subdir = str(manifest_abs.parent.relative_to(workspace_root))
            if rel_subdir == ".":
                rel_subdir = ""
            try:
                text = manifest_abs.read_text(errors="replace")[:_MAX_MANIFEST_BYTES]
            except OSError as exc:
                result.diagnostics.append(
                    f"MANIFEST_READ_FAILED:{rel_manifest}:{exc}"
                )
                continue

            top_dirs = [
                p.name for p in sorted(manifest_abs.parent.iterdir())
                if p.is_dir() and p.name not in _EXCLUDE_DIRS
            ]
            locks = [
                lf for lf in _LOCKFILES_BY_ECOSYSTEM.get(ecosystem, ())
                if (manifest_abs.parent / lf).exists()
            ]

            result.ecosystems.append(EcosystemFacts(
                ecosystem=ecosystem,
                subdir=rel_subdir,
                manifest_path=rel_manifest,
                manifest_text=text,
                top_level_dirs=top_dirs,
                lockfiles_present=locks,
            ))

            cls._diagnose(
                ecosystem, text, top_dirs, rel_manifest, result.diagnostics,
                manifest_dir=manifest_abs.parent,
                lockfiles_present=locks,
            )

        result.workspace_tree = cls._workspace_tree(workspace_root, cap=80)

        result.package_managers_on_path = await cls._which_many(
            ["uv", "pip", "pip3", "npm", "yarn", "pnpm", "cargo", "go", "poetry", "rustup"]
        )
        result.language_runtimes_on_path = await cls._which_many(
            ["python3", "python", "node", "rustc", "go"]
        )

        return result

    @classmethod
    def _walk_manifests(cls, root: Path) -> list[Path]:
        manifests: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(root):
            depth = Path(dirpath).relative_to(root).parts
            if len(depth) > _MAX_DEPTH:
                dirnames.clear()
                continue
            dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS]
            for name in filenames:
                if name in _MANIFEST_TO_ECOSYSTEM:
                    manifests.append(Path(dirpath) / name)
        return manifests

    @classmethod
    def _workspace_tree(cls, root: Path, *, cap: int) -> list[str]:
        entries: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            depth = Path(dirpath).relative_to(root).parts
            if len(depth) > _MAX_DEPTH:
                dirnames.clear()
                continue
            dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS]
            rel = Path(dirpath).relative_to(root)
            for d in dirnames:
                entries.append(str(rel / d))
                if len(entries) >= cap:
                    return entries
        return entries

    @classmethod
    async def _which_many(cls, names: list[str]) -> dict[str, str]:
        async def one(name: str) -> tuple[str, str | None]:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "which", name,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2)
                if proc.returncode == 0:
                    return name, stdout.decode().strip()
            except (TimeoutError, FileNotFoundError):
                pass
            return name, None

        results = await asyncio.gather(*[one(n) for n in names])
        return {n: p for n, p in results if p}

    @classmethod
    def _diagnose(
        cls,
        ecosystem: str,
        text: str,
        top_dirs: list[str],
        rel_manifest: str,
        diagnostics: list[str],
        *,
        manifest_dir: Path | None = None,
        lockfiles_present: list[str] | None = None,
    ) -> None:
        if ecosystem == "python":
            if (
                _SETUPTOOLS_BUILD_BACKEND_RE.search(text) is not None
                and "[tool.setuptools.packages.find]" not in text
                and len([d for d in top_dirs if not d.startswith(".")]) >= 2
            ):
                diagnostics.append(
                    f"SETUPTOOLS_FLAT_LAYOUT_RISK:{rel_manifest}:"
                    f"multiple top-level dirs {top_dirs} and no packages.find stanza"
                )
            # W3: venv absence is the most actionable signal for the agent —
            # tells it 'don't try to run the interpreter directly; setup_env first'.
            if manifest_dir is not None:
                venv_python = manifest_dir / ".venv" / "bin" / "python"
                if not venv_python.is_file():
                    diagnostics.append(
                        f"VENV_ABSENT:{rel_manifest}:.venv not yet created — "
                        "setup_env must run before the interpreter is usable"
                    )
        elif ecosystem == "node":
            if manifest_dir is not None and not (manifest_dir / "node_modules").is_dir():
                diagnostics.append(
                    f"NODE_MODULES_ABSENT:{rel_manifest}:node_modules not yet installed"
                )
        elif ecosystem == "rust":
            if manifest_dir is not None and not (manifest_dir / "target").is_dir():
                diagnostics.append(
                    f"CARGO_TARGET_ABSENT:{rel_manifest}:target/ not yet built"
                )

        # W8: lockfile-missing signal (applies to ecosystems where a lockfile is
        # standard practice — Python and Node especially). Helps the consumer
        # pick the right install_command (uv sync vs uv lock + sync; npm install
        # vs npm ci) and warns the agent that reproducibility is lower.
        if lockfiles_present is not None and ecosystem in ("python", "node", "rust"):
            if not lockfiles_present:
                diagnostics.append(
                    f"LOCKFILE_MISSING:{ecosystem}:{rel_manifest}:no recognized lockfile"
                )
