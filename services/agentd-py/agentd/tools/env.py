"""Environment setup and binary discovery tools."""
from __future__ import annotations

import asyncio
import os
from asyncio.subprocess import PIPE, STDOUT
from pathlib import Path

from agentd.tools._paths import resolve_workspace_bin
from agentd.tools.registry import ToolOutput
from agentd.tools.shell import _resolve_workspace_cwd

_MAX_OUTPUT_CHARS = 4000

# Maps a missing binary to the setup_env command most likely to install it.
# Used to make find_binary's "not found" output actionable.
_PM_HINT_FOR_BINARY: dict[str, str] = {
    "pytest": "uv sync",
    "ruff": "uv sync",
    "mypy": "uv sync",
    "black": "uv sync",
    "vitest": "npm install",
    "jest": "npm install",
    "tsc": "npm install",
    "eslint": "npm install",
    "prettier": "npm install",
}


async def find_binary(*, name: str, real_workspace: Path) -> ToolOutput:
    """Locate an executable binary in the real workspace or on system PATH.

    Probes workspace-local dirs (`.venv/bin`, `node_modules/.bin`, `target/release`,
    `target/debug`) first, then `which`, then a `find -maxdepth 6` sweep of the
    workspace tree. Returns all found paths ranked shallowest first, or a structured
    "not found" message with an `AGENT SHOULD: setup_env "<cmd>"` hint when the
    binary maps to a known package-manager install command.

    Always returns is_error=False — used as a non-failing probe in verify-phase gating.
    """
    if not name or "/" in name:
        return ToolOutput(
            output="Error: binary name must not contain path separators", is_error=True
        )

    found: list[str] = []

    # 1. Workspace-local probe (cheapest, most relevant — agent-installed deps).
    local_bin = resolve_workspace_bin(real_workspace, name)
    if local_bin is not None:
        found.append(str(local_bin))

    # 2. System PATH lookup.
    which_path = await _run_silent("which", name)
    if which_path:
        which_path = which_path.strip()
        if which_path and which_path not in found:
            found.append(which_path)

    # 3. Workspace-wide search (covers nested venvs, node_modules under packages, etc.).
    try:
        proc = await asyncio.create_subprocess_exec(
            "find", str(real_workspace),
            "-name", name,
            "-maxdepth", "6",
            "-type", "f",
            stdout=PIPE,
            stderr=PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and line not in found:
                found.append(line)
    except (TimeoutError, FileNotFoundError):
        pass

    if not found:
        miss = f"not found: no '{name}' binary on PATH or in {real_workspace}"
        hint = _PM_HINT_FOR_BINARY.get(name)
        if hint:
            miss += (
                f"\nAGENT SHOULD: emit_patch to declare '{name}' as a dependency, "
                f'then setup_env "{hint}"'
            )
        else:
            # Generic, tool-agnostic escalation hint — let the agent read the
            # workspace's project manifest to pick the right package manager.
            miss += (
                "\nAGENT SHOULD: if this binary belongs to a project-local dev "
                "environment, inspect the workspace's project manifest "
                "(pyproject.toml, package.json, Cargo.toml, go.mod, ...) for the "
                "package manager and call setup_env with its sync/install command."
            )
        return ToolOutput(output=miss, is_error=False)

    # Workspace-local hit (already first); rank remaining by depth (shallowest first).
    head = found[:1] if local_bin is not None else []
    tail = sorted(found[len(head):], key=lambda p: p.count(os.sep))
    ranked = head + tail
    lines = [f"found: {p}" for p in ranked]
    return ToolOutput(output="\n".join(lines))


async def _run_silent(command: str, *args: str) -> str | None:
    """Run a command, return stdout stripped, or None on failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            command, *args,
            stdout=PIPE,
            stderr=PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode == 0:
            return stdout.decode("utf-8", errors="replace").strip()
    except (TimeoutError, FileNotFoundError):
        pass
    return None


_SETUP_ENV_BINARIES = {"uv", "pip3", "pip", "npm", "yarn", "pnpm", "cargo", "go", "poetry", "rustup"}
_SETUP_ENV_TIMEOUT_SEC = 300

_PYTHON_PMS: frozenset[str] = frozenset({"uv", "pip", "pip3", "poetry"})
_NODE_PMS: frozenset[str] = frozenset({"npm", "yarn", "pnpm"})
_RUST_PMS: frozenset[str] = frozenset({"cargo", "rustup"})
_GO_PMS: frozenset[str] = frozenset({"go"})

_PM_INSTALL_URLS: dict[str, str] = {
    "uv": "curl -LsSf https://astral.sh/uv/install.sh | sh",
    "poetry": "curl -sSL https://install.python-poetry.org | python3 -",
    "npm": "https://nodejs.org/en/download (bundled with Node)",
    "yarn": "npm install -g yarn  (after installing Node)",
    "pnpm": "npm install -g pnpm  (after installing Node)",
    "cargo": "https://rustup.rs",
    "rustup": "https://rustup.rs",
    "go": "https://go.dev/doc/install",
}

_NODE_LOCKFILE_TO_PM: dict[str, str] = {
    "package-lock.json": "npm",
    "yarn.lock": "yarn",
    "pnpm-lock.yaml": "pnpm",
}

_NODE_PM_INSTALL_FLAG: dict[str, str] = {
    "npm": "ci",
    "yarn": "install --frozen-lockfile",
    "pnpm": "install --frozen-lockfile",
}

_VALID_ECOSYSTEMS: frozenset[str] = frozenset({"python", "node", "rust", "go"})

# Smallest valid pyproject.toml that uv/pip can read. Adds dev_deps as runtime
# dependencies (pip will resolve them when installing the project) so any pip
# version handles them — PEP 735 dependency-groups would require pip 25+.
_PYTHON_PYPROJECT_TEMPLATE = """\
[project]
name = "workspace"
version = "0.0.0"
requires-python = ">=3.9"
dependencies = {deps}

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"
"""

# Minimal package.json — single test script, devDependencies the agent declared.
_NODE_PACKAGE_JSON_TEMPLATE = """\
{{
  "name": "workspace",
  "version": "0.0.0",
  "private": true,
  "scripts": {{
    "test": "{test_cmd}"
  }},
  "devDependencies": {dev_deps_json}
}}
"""

_RUST_CARGO_TOML_TEMPLATE = """\
[package]
name = "workspace"
version = "0.0.0"
edition = "2021"

[dependencies]
{deps}

[dev-dependencies]
{dev_deps}
"""

_GO_MOD_TEMPLATE = "module workspace\n\ngo 1.21\n"


async def _which(name: str) -> bool:
    """True iff `name` is an executable on PATH."""
    return (await _run_silent("which", name)) is not None


def _structured_pm_missing(binary: str) -> ToolOutput:
    """Terminal error for a missing toolchain we cannot bootstrap."""
    install = _PM_INSTALL_URLS.get(binary, "(no install hint)")
    return ToolOutput(
        output=(
            f"Error: '{binary}' not found on PATH. Cannot bootstrap automatically.\n"
            f"Install: {install}\n"
            f"AGENT SHOULD: emit revision_needed citing missing toolchain '{binary}'."
        ),
        is_error=True,
    )


async def _node_pm_alternative(binary: str, shadow_root: Path) -> ToolOutput:
    """Suggest an alt Node PM if its lockfile is in shadow and its binary is on PATH."""
    for lockfile, alt_pm in _NODE_LOCKFILE_TO_PM.items():
        if alt_pm == binary:
            continue
        if not (shadow_root / lockfile).exists():
            continue
        if not await _which(alt_pm):
            continue
        cmd = _NODE_PM_INSTALL_FLAG.get(alt_pm, "install")
        return ToolOutput(
            output=(
                f"Error: '{binary}' not found on PATH. "
                f"Detected '{lockfile}' in workspace and '{alt_pm}' is available.\n"
                f'AGENT SHOULD: setup_env "{alt_pm} {cmd}"'
            ),
            is_error=True,
        )
    return _structured_pm_missing(binary)


async def _python_pm_fallback(
    binary: str,
    parts: list[str],
    shadow_root: Path,
    real_workspace: Path,
    timeout_sec: int,
) -> ToolOutput:
    """Bootstrap a venv via system python3 + pip when the requested PM is missing."""
    if binary == "poetry":
        # Poetry's lockfile semantics can't be faithfully reproduced via pip — escalate.
        return _structured_pm_missing(binary)

    python = await _run_silent("which", "python3") or await _run_silent("which", "python")
    if not python:
        # Last-resort backup: the backend's own interpreter.
        import sys as _sys
        python = _sys.executable
        if not python or not Path(python).is_file():
            return ToolOutput(
                output=(
                    "Error: 'uv' not found and no system 'python3' available either. "
                    "This is a fatal environment issue — install Python 3 or uv "
                    "(curl -LsSf https://astral.sh/uv/install.sh | sh)."
                ),
                is_error=True,
            )
    python = python.strip()

    venv_path = real_workspace / ".venv"
    venv_python = venv_path / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python"
    )

    log_lines: list[str] = [
        f"note: '{binary}' not on PATH; bootstrapped via python3 -m venv + pip"
    ]

    # Step 1: create venv (idempotent).
    if not venv_python.is_file():
        rc, out = await _run_capture(
            [python, "-m", "venv", str(venv_path)],
            cwd=str(shadow_root),
            timeout_sec=120,
        )
        log_lines.append(f"$ {python} -m venv {venv_path}")
        log_lines.append(out.strip() or "(no output)")
        if rc != 0:
            return ToolOutput(output="\n".join(log_lines), is_error=True)
    else:
        log_lines.append(f"(venv already present at {venv_path})")

    venv_pip = venv_path / ("Scripts" if os.name == "nt" else "bin") / (
        "pip.exe" if os.name == "nt" else "pip"
    )

    # Step 2: choose install source from shadow.
    install_cmd = _build_pip_install(parts, shadow_root, str(venv_pip))
    if install_cmd is None:
        log_lines.append(
            f"(no pyproject.toml or requirements*.txt in {shadow_root}; venv created, nothing installed)"
        )
        return ToolOutput(output="\n".join(log_lines), is_error=False)

    log_lines.append(f"$ {' '.join(install_cmd)}")
    rc, out = await _run_capture(install_cmd, cwd=str(shadow_root), timeout_sec=timeout_sec)
    log_lines.append(out.strip() or "(no output)")
    return ToolOutput(output="\n".join(log_lines), is_error=rc != 0)


def _build_pip_install(
    parts: list[str], shadow_root: Path, pip: str
) -> list[str] | None:
    """Translate the agent's setup_env command into a venv-pip install command."""
    # If the agent wrote `pip install -r requirements.txt`, honor it directly.
    if parts[0] in ("pip", "pip3") and len(parts) >= 2 and parts[1] == "install":
        return [pip, *parts[1:]]

    # Otherwise — `uv sync` style. Pick install source from shadow.
    requirements = next(iter(sorted(shadow_root.glob("requirements*.txt"))), None)
    if requirements is not None:
        return [pip, "install", "-r", str(requirements)]
    if (shadow_root / "pyproject.toml").exists():
        # `pip install -e <shadow>` reads pyproject.toml's [project.dependencies]
        # via PEP 517. PEP 735 dependency-groups (uv-style dev deps) require pip 25+;
        # older pip will skip them. Still installs the project + runtime deps.
        return [pip, "install", "-e", str(shadow_root)]
    return None


async def _run_capture(
    cmd: list[str], *, cwd: str, timeout_sec: int
) -> tuple[int, str]:
    """Run cmd, return (returncode, combined_stdout_stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=PIPE,
            stderr=STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    except TimeoutError:
        return -1, f"(timed out after {timeout_sec}s)"
    except FileNotFoundError:
        return -1, f"(executable not found: {cmd[0]})"
    except Exception as exc:
        return -1, f"(error: {exc})"
    return proc.returncode or 0, stdout.decode("utf-8", errors="replace")


async def setup_env(
    *,
    command: str,
    shadow_root: Path,
    real_workspace: Path,
    cwd: str | None = None,
    timeout_sec: int = _SETUP_ENV_TIMEOUT_SEC,
) -> ToolOutput:
    """Run an env setup command in shadow_root (reads patched dep files),
    installing binaries permanently to real_workspace.

    shadow_root is cwd so the package manager reads YOUR patched pyproject.toml
    / package.json / requirements.txt. Real-workspace targeting is achieved via
    env vars or explicit path args per package manager.

    Ecosystem-aware fallback when the requested PM itself is missing on PATH:
      - Python (uv/pip): transparent fallback to system python3 -m venv + pip
      - Python (poetry): hard fail (lockfile semantics can't be faked)
      - Node (npm/yarn/pnpm): sniff shadow for alternative lockfile + PM
      - Rust/Go: hard fail with install URL + revision_needed hint
    """
    parts = command.strip().split()
    if not parts:
        return ToolOutput(output="Error: command is required", is_error=True)

    binary = parts[0]
    if binary not in _SETUP_ENV_BINARIES:
        return ToolOutput(
            output=(
                f"Error: '{binary}' not allowed for setup_env. "
                f"Allowed: {', '.join(sorted(_SETUP_ENV_BINARIES))}"
            ),
            is_error=True,
        )

    # Probe the requested PM and dispatch by ecosystem.
    binary_present = await _which(binary)

    if not binary_present:
        if binary in _PYTHON_PMS:
            return await _python_pm_fallback(binary, parts, shadow_root, real_workspace, timeout_sec)
        if binary in _NODE_PMS:
            return await _node_pm_alternative(binary, shadow_root)
        if binary in _RUST_PMS | _GO_PMS:
            return _structured_pm_missing(binary)
        # Unreachable given allowlist, but defensive.
        return _structured_pm_missing(binary)

    env = os.environ.copy()
    cmd_parts = list(parts)

    if binary == "uv":
        env["UV_PROJECT_ENVIRONMENT"] = str(real_workspace / ".venv")

    elif binary in ("pip3", "pip"):
        real_pip = real_workspace / ".venv" / "bin" / binary
        if real_pip.exists():
            cmd_parts[0] = str(real_pip)
        else:
            # No .venv present — bootstrap one via the same Python that hosts pip,
            # then re-target. Avoids polluting system Python while keeping the
            # agent's "pip install X" command working.
            return await _python_pm_fallback(binary, parts, shadow_root, real_workspace, timeout_sec)

    elif binary == "npm":
        env["npm_config_prefix"] = str(real_workspace)

    elif binary == "yarn":
        if "--modules-dir" not in command:
            cmd_parts += ["--modules-dir", str(real_workspace / "node_modules")]

    elif binary == "pnpm":
        if "--modules-dir" not in command:
            cmd_parts += ["--modules-dir", str(real_workspace / "node_modules")]

    # cargo/go/poetry/rustup: cwd=shadow_root is sufficient; they use global caches/toolchains

    resolved_cwd = _resolve_workspace_cwd(shadow_root, cwd)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd_parts,
            cwd=str(resolved_cwd),
            stdout=PIPE,
            stderr=STDOUT,
            env=env,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    except TimeoutError:
        return ToolOutput(
            output=f"Error: setup_env '{command}' timed out after {timeout_sec}s",
            is_error=True,
        )
    except FileNotFoundError:
        # Race: passed _which but vanished before exec. Treat as missing.
        if binary in _PYTHON_PMS:
            return await _python_pm_fallback(binary, parts, shadow_root, real_workspace, timeout_sec)
        if binary in _NODE_PMS:
            return await _node_pm_alternative(binary, shadow_root)
        return _structured_pm_missing(binary)
    except Exception as exc:
        return ToolOutput(output=f"Error running setup_env '{command}': {exc}", is_error=True)

    output = stdout.decode("utf-8", errors="replace")
    exit_code = proc.returncode or 0
    header = f"$ {command}\n(exit code: {exit_code})\n"
    full = header + output
    if len(full) > _MAX_OUTPUT_CHARS:
        keep = _MAX_OUTPUT_CHARS - len(header) - 80
        full = header + "...(truncated)...\n" + output[-keep:]
    return ToolOutput(output=full, is_error=exit_code != 0)


async def init_workspace(
    *,
    ecosystem: str,
    dev_deps: list[str],
    shadow_root: Path,
) -> ToolOutput:
    """Emit a minimal manifest into the shadow workspace for a bare project.

    Creates exactly the smallest valid manifest for the ecosystem and includes
    the requested dev_deps verbatim — no extra packages, no project metadata
    bloat. Refuses to overwrite an existing manifest (the agent should
    emit_patch directly in that case).
    """
    import json

    if ecosystem not in _VALID_ECOSYSTEMS:
        return ToolOutput(
            output=(
                f"Error: unknown ecosystem '{ecosystem}'. "
                f"Allowed: {', '.join(sorted(_VALID_ECOSYSTEMS))}"
            ),
            is_error=True,
        )

    if not isinstance(dev_deps, list) or not all(isinstance(d, str) for d in dev_deps):
        return ToolOutput(output="Error: dev_deps must be a list of strings", is_error=True)

    if ecosystem == "python":
        target = shadow_root / "pyproject.toml"
        if target.exists():
            return ToolOutput(
                output=(
                    "Error: pyproject.toml already exists. "
                    "Use setup_env directly, or emit_patch to modify it."
                ),
                is_error=True,
            )
        deps = sorted(set(dev_deps) | {"pytest"})
        content = _PYTHON_PYPROJECT_TEMPLATE.format(deps=json.dumps(deps))
        target.write_text(content)
        return ToolOutput(
            output=(
                f"Created {target.name} with {len(deps)} deps: {deps}\n"
                f'Next: setup_env "uv sync"'
            )
        )

    if ecosystem == "node":
        target = shadow_root / "package.json"
        if target.exists():
            return ToolOutput(
                output=(
                    "Error: package.json already exists. "
                    "Use setup_env directly, or emit_patch to modify it."
                ),
                is_error=True,
            )
        # Default test script: prefer vitest if declared, else jest, else node --test.
        if "vitest" in dev_deps:
            test_cmd = "vitest run"
        elif "jest" in dev_deps:
            test_cmd = "jest"
        else:
            test_cmd = "node --test"
        dev_deps_obj = {name: "*" for name in sorted(set(dev_deps))}
        content = _NODE_PACKAGE_JSON_TEMPLATE.format(
            test_cmd=test_cmd, dev_deps_json=json.dumps(dev_deps_obj, indent=2)
        )
        target.write_text(content)
        return ToolOutput(
            output=(
                f"Created {target.name} with {len(dev_deps_obj)} devDependencies: "
                f"{list(dev_deps_obj)}\nNext: setup_env \"npm install\""
            )
        )

    if ecosystem == "rust":
        target = shadow_root / "Cargo.toml"
        if target.exists():
            return ToolOutput(
                output="Error: Cargo.toml already exists.",
                is_error=True,
            )
        # dev_deps strings are taken as crate names (versions left to user via emit_patch).
        deps_lines = ""
        dev_lines = "\n".join(f'{name} = "*"' for name in sorted(set(dev_deps)))
        content = _RUST_CARGO_TOML_TEMPLATE.format(deps=deps_lines, dev_deps=dev_lines)
        target.write_text(content)
        # Rust requires a src/lib.rs or src/main.rs to be a valid crate; create lib.
        lib_rs = shadow_root / "src" / "lib.rs"
        lib_rs.parent.mkdir(parents=True, exist_ok=True)
        if not lib_rs.exists():
            lib_rs.write_text("// workspace lib\n")
        return ToolOutput(
            output=(
                f"Created Cargo.toml + src/lib.rs.\n"
                f'Next: cargo must be on PATH; then setup_env "cargo build" or run_command cargo test'
            )
        )

    if ecosystem == "go":
        target = shadow_root / "go.mod"
        if target.exists():
            return ToolOutput(output="Error: go.mod already exists.", is_error=True)
        target.write_text(_GO_MOD_TEMPLATE)
        if dev_deps:
            return ToolOutput(
                output=(
                    f"Created go.mod (Go has no dev_deps concept; "
                    f"the {len(dev_deps)} requested dep(s) were ignored). "
                    f'Next: setup_env "go mod download" then run_command go test ./...'
                )
            )
        return ToolOutput(
            output='Created go.mod.\nNext: setup_env "go mod download" then run_command go test ./...'
        )

    # Unreachable.
    return ToolOutput(output=f"Error: ecosystem '{ecosystem}' fell through dispatch", is_error=True)
