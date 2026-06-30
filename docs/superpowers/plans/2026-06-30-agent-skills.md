# Agent Skills (P2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Discover `.ai-editor/skills/<name>/SKILL.md` skills, expose an always-on catalog to the controller, let the model pull a skill body into the dynamic tail via a `read_skill` tool (or a `/skill-name` forced-load), and run bundled scripts through the existing shell gate.

**Architecture:** A mtime-cached `SkillCatalogLoader` scans + parses SKILL.md frontmatter. The catalog renders into the controller system prompt (cache-stable). A `SkillToolSource` adds `read_skill(name)`; activated bodies live in a turn-scoped `active_skills` dict that the payload builder injects into the dynamic tail each iteration. A `/v1/skills` route + a `forced_skills` message field drive deterministic explicit invocation. Everything is flag-gated (`AI_EDITOR_SKILLS_ENABLED`, default off), controller-only.

**Tech Stack:** Python 3.13 (FastAPI, Pydantic v2, PyYAML), pytest/pytest-asyncio; TypeScript (editor-client Zod contracts, vscode-extension, React webview-ui), vitest.

## Global Constraints

- **Flag default OFF:** `AI_EDITOR_SKILLS_ENABLED` truthy = `1/true/yes/on` (case-insensitive); everything else (incl. unset) = off. Mirror `is_memory_enabled` exactly.
- **Controller-only:** never touch `planning/prompts.py` or the task path.
- **Best-effort, degrade-not-raise:** any loader/parse/IO error skips that skill + `logger.warning`; never raises into a turn.
- **Frozen workspace:** the loader is built from the controller's frozen `workspace_path` (factory time), never the thread's per-turn column.
- **KV-cache discipline:** the catalog block is appended to the system prompt (cache-stable, mtime-driven); per-turn-varying skill bodies ride the **dynamic tail** of the user payload (finding #13), never the cached head.
- **Single dir, standard format:** discover only `<workspace>/.ai-editor/skills/*/SKILL.md`; parse standard agentskills.io frontmatter (`name`, `description` required; rest forward-compat ignored).
- **Names:** skill `name` ≤64 chars; a `name` ≠ parent folder is a `logger.warning`, not a rejection.
- **Run all Python tests from** `services/agentd-py` with the venv active: `source .venv/bin/activate`.
- **TS build order:** after editing `apps/editor-client`, run `npm run -w @ai-editor/editor-client build` before the extension typecheck.

**Spec §3.2 refinement (v1 cut — read before Task 2):** v1 ships the **full catalog in the cache-stable system prompt**, with an **order-truncation budget guard** (over `AI_EDITOR_SKILLS_CATALOG_MAX_CHARS` → keep the first entries that fit + a "[N more not shown]" note; still query-independent ⇒ stays cache-stable, no tail relocation). The `Embedder`-ranked selection the spec describes is delivered as a **tested pure primitive** (`rank_skills_by_relevance`) but is **not wired** into the live path in v1 — wiring it (and relocating an over-budget, query-ranked subset to the tail) is deferred until catalog size demands it. This keeps the engine uncoupled from the embedder for a path no v1 user hits, while still building the ranking the scale story needs.

---

### Task 1: SkillManifest model + SkillCatalogLoader (discovery + parse)

**Files:**
- Modify: `services/agentd-py/pyproject.toml` (add `pyyaml` to `dependencies`)
- Create: `services/agentd-py/agentd/skills/__init__.py`
- Create: `services/agentd-py/agentd/skills/models.py`
- Create: `services/agentd-py/agentd/skills/loader.py`
- Test: `services/agentd-py/tests/test_skills_loader.py`

**Interfaces:**
- Produces: `SkillManifest(name: str, description: str, body_path: Path, dir: Path)` (frozen dataclass); `SkillCatalogLoader(workspace_path: Path | str)` with `load_catalog() -> list[SkillManifest]` (mtime-cached, sorted by `name`).

- [ ] **Step 1: Add the PyYAML dependency**

In `services/agentd-py/pyproject.toml`, add to the `[project] dependencies` array (alongside `"pydantic>=2.11.0"`):

```toml
  "pyyaml>=6.0",
```

Run: `source .venv/bin/activate && pip install -e .` — Expected: succeeds (PyYAML already resolvable).

- [ ] **Step 2: Write the failing test**

```python
# services/agentd-py/tests/test_skills_loader.py
from pathlib import Path

from agentd.skills.loader import SkillCatalogLoader


def _write_skill(root: Path, name: str, description: str, body: str = "Do the thing.") -> None:
    d = root / ".ai-editor" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n", encoding="utf-8"
    )


def test_loads_valid_skills_sorted_by_name(tmp_path: Path) -> None:
    _write_skill(tmp_path, "git-commit", "Make a conventional commit. Use for commits.")
    _write_skill(tmp_path, "alpha", "First skill.")
    cat = SkillCatalogLoader(tmp_path).load_catalog()
    assert [m.name for m in cat] == ["alpha", "git-commit"]
    assert cat[1].description == "Make a conventional commit. Use for commits."
    assert cat[1].body_path == tmp_path / ".ai-editor/skills/git-commit/SKILL.md"


def test_absent_dir_returns_empty(tmp_path: Path) -> None:
    assert SkillCatalogLoader(tmp_path).load_catalog() == []


def test_skips_skill_missing_required_field(tmp_path: Path, caplog) -> None:
    d = tmp_path / ".ai-editor/skills/broken"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: broken\n---\nno description\n", encoding="utf-8")
    _write_skill(tmp_path, "good", "Valid one.")
    cat = SkillCatalogLoader(tmp_path).load_catalog()
    assert [m.name for m in cat] == ["good"]


def test_name_mismatch_warns_but_keeps(tmp_path: Path, caplog) -> None:
    d = tmp_path / ".ai-editor/skills/folder-name"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: other-name\ndescription: Mismatch.\n---\nbody\n", encoding="utf-8"
    )
    import logging
    with caplog.at_level(logging.WARNING):
        cat = SkillCatalogLoader(tmp_path).load_catalog()
    assert [m.name for m in cat] == ["other-name"]
    assert any("does not match folder" in r.message for r in caplog.records)


def test_mtime_cache_returns_same_objects_until_changed(tmp_path: Path) -> None:
    _write_skill(tmp_path, "a", "A.")
    loader = SkillCatalogLoader(tmp_path)
    first = loader.load_catalog()
    assert loader.load_catalog() is first  # cached identity, no re-scan
    _write_skill(tmp_path, "b", "B.")
    import os, time
    time.sleep(0.01)
    os.utime(tmp_path / ".ai-editor/skills", None)  # bump dir mtime
    second = loader.load_catalog()
    assert [m.name for m in second] == ["a", "b"]


def test_bad_yaml_is_skipped(tmp_path: Path) -> None:
    d = tmp_path / ".ai-editor/skills/bad"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: [unterminated\n---\nbody\n", encoding="utf-8")
    _write_skill(tmp_path, "ok", "Fine.")
    assert [m.name for m in SkillCatalogLoader(tmp_path).load_catalog()] == ["ok"]
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `source .venv/bin/activate && pytest tests/test_skills_loader.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agentd.skills'`.

- [ ] **Step 4: Create the model**

```python
# services/agentd-py/agentd/skills/__init__.py
```

```python
# services/agentd-py/agentd/skills/models.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SkillManifest:
    """A discovered skill's catalog entry. The body is read lazily by read_skill."""

    name: str
    description: str
    body_path: Path
    dir: Path
```

- [ ] **Step 5: Implement the loader**

```python
# services/agentd-py/agentd/skills/loader.py
"""mtime-cached discovery + frontmatter parse for `.ai-editor/skills/*/SKILL.md`.

Mirrors instructions/loader.py: a cheap NOOP when the skills dir has not moved,
a single re-scan when it has. Best-effort — a malformed skill is skipped with a
warning, never raising into a turn.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import yaml

from agentd.skills.models import SkillManifest

logger = logging.getLogger(__name__)

_NAME_MAX = 64
_DESC_MAX = 1024


class SkillCatalogLoader:
    SKILLS_SUBDIR = Path(".ai-editor") / "skills"

    def __init__(self, workspace_path: Path | str) -> None:
        self._root = Path(workspace_path) / self.SKILLS_SUBDIR
        self._lock = threading.Lock()
        self._cached_mtime_ns: int | None = None
        self._cached: list[SkillManifest] | None = None

    def load_catalog(self) -> list[SkillManifest]:
        with self._lock:
            try:
                mtime_ns = self._root.stat().st_mtime_ns
            except (FileNotFoundError, NotADirectoryError):
                self._cached_mtime_ns = None
                self._cached = []
                return self._cached
            except OSError as exc:
                logger.warning("[skills] cannot stat %s: %s", self._root, exc)
                return self._cached if self._cached is not None else []

            if self._cached_mtime_ns == mtime_ns and self._cached is not None:
                return self._cached

            self._cached = self._scan()
            self._cached_mtime_ns = mtime_ns
            return self._cached

    def _scan(self) -> list[SkillManifest]:
        out: list[SkillManifest] = []
        for child in sorted(self._root.iterdir()):
            if not child.is_dir():
                continue
            manifest = self._parse(child)
            if manifest is not None:
                out.append(manifest)
        out.sort(key=lambda m: m.name)
        return out

    def _parse(self, skill_dir: Path) -> SkillManifest | None:
        body_path = skill_dir / "SKILL.md"
        try:
            text = body_path.read_text(encoding="utf-8")
        except OSError:
            return None
        front = self._frontmatter(text)
        if front is None:
            logger.warning("[skills] %s: missing/invalid YAML frontmatter", body_path)
            return None
        name = front.get("name")
        description = front.get("description")
        if not isinstance(name, str) or not name.strip():
            logger.warning("[skills] %s: missing 'name'", body_path)
            return None
        if not isinstance(description, str) or not description.strip():
            logger.warning("[skills] %s: missing 'description'", body_path)
            return None
        name = name.strip()[:_NAME_MAX]
        description = description.strip()[:_DESC_MAX]
        if name != skill_dir.name:
            logger.warning("[skills] %s: name %r does not match folder %r",
                           body_path, name, skill_dir.name)
        return SkillManifest(name=name, description=description,
                             body_path=body_path, dir=skill_dir)

    @staticmethod
    def _frontmatter(text: str) -> dict[str, object] | None:
        if not text.startswith("---"):
            return None
        parts = text.split("---", 2)
        if len(parts) < 3:
            return None
        try:
            data = yaml.safe_load(parts[1])
        except yaml.YAMLError:
            return None
        return data if isinstance(data, dict) else None
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `source .venv/bin/activate && pytest tests/test_skills_loader.py -v`
Expected: PASS (6 tests).

- [ ] **Step 7: Commit**

```bash
git add services/agentd-py/pyproject.toml services/agentd-py/agentd/skills/ services/agentd-py/tests/test_skills_loader.py
git commit -m "feat(skills): SkillCatalogLoader — mtime-cached SKILL.md discovery + parse"
```

---

### Task 2: Catalog rendering + budget guard + ranking primitive

**Files:**
- Create: `services/agentd-py/agentd/skills/catalog.py`
- Test: `services/agentd-py/tests/test_skills_catalog.py`

**Interfaces:**
- Consumes: `SkillManifest` (Task 1).
- Produces: `render_skills_catalog(entries: list[SkillManifest]) -> str`; `select_catalog_for_budget(entries: list[SkillManifest], max_chars: int) -> tuple[list[SkillManifest], int]` returning `(shown, hidden_count)`; `rank_skills_by_relevance(entries: list[SkillManifest], query: str, embedder) -> list[SkillManifest]` (tested primitive, not wired in v1).

- [ ] **Step 1: Write the failing test**

```python
# services/agentd-py/tests/test_skills_catalog.py
from pathlib import Path

from agentd.skills.catalog import (
    render_skills_catalog,
    select_catalog_for_budget,
    rank_skills_by_relevance,
)
from agentd.skills.models import SkillManifest


def _m(name: str, desc: str) -> SkillManifest:
    return SkillManifest(name=name, description=desc, body_path=Path("x"), dir=Path("d"))


def test_render_lists_name_and_description() -> None:
    out = render_skills_catalog([_m("git-commit", "Make a commit.")])
    assert "git-commit: Make a commit." in out


def test_render_empty_is_empty_string() -> None:
    assert render_skills_catalog([]) == ""


def test_budget_under_shows_all() -> None:
    entries = [_m("a", "x" * 10), _m("b", "y" * 10)]
    shown, hidden = select_catalog_for_budget(entries, max_chars=10_000)
    assert shown == entries and hidden == 0


def test_budget_over_truncates_by_order() -> None:
    entries = [_m(f"s{i}", "d" * 100) for i in range(10)]
    shown, hidden = select_catalog_for_budget(entries, max_chars=250)
    assert 0 < len(shown) < 10
    assert hidden == 10 - len(shown)


def test_rank_orders_by_cosine_to_query() -> None:
    # Fake embedder: returns a 1-D "embedding" keyed by a substring marker.
    def encoder(texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] if "PDF" in t else [0.0, 1.0] for t in texts]

    from agentd.memory.embedder import Embedder
    emb = Embedder(encoder=encoder)
    entries = [_m("logs", "tail logs"), _m("pdf", "extract PDF text")]
    ranked = rank_skills_by_relevance(entries, "work with a PDF", emb)
    assert ranked[0].name == "pdf"


def test_rank_degrades_to_input_order_when_embedder_unavailable() -> None:
    from agentd.memory.embedder import Embedder

    def boom(texts: list[str]) -> list[list[float]]:
        raise RuntimeError("no model")

    entries = [_m("a", "x"), _m("b", "y")]
    assert rank_skills_by_relevance(entries, "q", Embedder(encoder=boom)) == entries
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `source .venv/bin/activate && pytest tests/test_skills_catalog.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agentd.skills.catalog'`.

- [ ] **Step 3: Implement**

```python
# services/agentd-py/agentd/skills/catalog.py
"""Render + budget-guard the skill catalog; an embedder-ranking primitive.

v1 wires render + select_catalog_for_budget (order-truncation, query-independent
⇒ cache-stable). rank_skills_by_relevance is built + tested but NOT wired live —
it's the primitive the scale path (over-budget, query-ranked → tail) will use.
"""
from __future__ import annotations

from agentd.skills.models import SkillManifest


def render_skills_catalog(entries: list[SkillManifest]) -> str:
    if not entries:
        return ""
    return "\n".join(f"- {m.name}: {m.description}" for m in entries)


def select_catalog_for_budget(
    entries: list[SkillManifest], max_chars: int
) -> tuple[list[SkillManifest], int]:
    """Keep entries (in order) whose cumulative rendered length fits max_chars.
    Returns (shown, hidden_count). Query-independent ⇒ stays cache-stable."""
    shown: list[SkillManifest] = []
    used = 0
    for m in entries:
        line = len(f"- {m.name}: {m.description}\n")
        if shown and used + line > max_chars:
            break
        shown.append(m)
        used += line
    return shown, len(entries) - len(shown)


def rank_skills_by_relevance(
    entries: list[SkillManifest], query: str, embedder: object
) -> list[SkillManifest]:
    """Order entries by cosine(query, description). Degrades to input order if the
    embedder yields nothing (degrade-not-raise). NOT wired in v1."""
    if not entries:
        return entries
    vecs = embedder.embed([query] + [m.description for m in entries])  # type: ignore[attr-defined]
    if not vecs or len(vecs) != len(entries) + 1:
        return entries
    q = vecs[0]
    scored = [
        (sum(a * b for a, b in zip(q, vecs[i + 1])), i, m)
        for i, m in enumerate(entries)
    ]
    scored.sort(key=lambda t: (-t[0], t[1]))  # desc score, stable by original index
    return [m for _, _, m in scored]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `source .venv/bin/activate && pytest tests/test_skills_catalog.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add services/agentd-py/agentd/skills/catalog.py services/agentd-py/tests/test_skills_catalog.py
git commit -m "feat(skills): catalog render + budget guard + ranking primitive"
```

---

### Task 3: `is_skills_enabled` flag + config knobs

**Files:**
- Modify: `services/agentd-py/agentd/chat/controller_factory.py` (add `is_skills_enabled`)
- Create: `services/agentd-py/agentd/skills/config.py`
- Test: `services/agentd-py/tests/test_skills_config.py`

**Interfaces:**
- Produces: `is_skills_enabled() -> bool` (default False); `skills_catalog_max_chars() -> int` (default 16000); `skills_body_max_chars() -> int` (default 20000).

- [ ] **Step 1: Write the failing test**

```python
# services/agentd-py/tests/test_skills_config.py
from agentd.chat.controller_factory import is_skills_enabled
from agentd.skills.config import skills_body_max_chars, skills_catalog_max_chars


def test_flag_default_off(monkeypatch) -> None:
    monkeypatch.delenv("AI_EDITOR_SKILLS_ENABLED", raising=False)
    assert is_skills_enabled() is False


def test_flag_truthy(monkeypatch) -> None:
    for v in ("1", "true", "YES", "on"):
        monkeypatch.setenv("AI_EDITOR_SKILLS_ENABLED", v)
        assert is_skills_enabled() is True


def test_flag_explicit_off(monkeypatch) -> None:
    monkeypatch.setenv("AI_EDITOR_SKILLS_ENABLED", "0")
    assert is_skills_enabled() is False


def test_budget_defaults(monkeypatch) -> None:
    monkeypatch.delenv("AI_EDITOR_SKILLS_CATALOG_MAX_CHARS", raising=False)
    monkeypatch.delenv("AI_EDITOR_SKILLS_BODY_MAX_CHARS", raising=False)
    assert skills_catalog_max_chars() == 16000
    assert skills_body_max_chars() == 20000
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `source .venv/bin/activate && pytest tests/test_skills_config.py -v`
Expected: FAIL — `ImportError: cannot import name 'is_skills_enabled'`.

- [ ] **Step 3: Implement the flag (in controller_factory.py)**

Look at the existing `is_memory_enabled` (around `controller_factory.py:31`) to copy its truthy helper. Add next to it:

```python
def is_skills_enabled() -> bool:
    return os.environ.get("AI_EDITOR_SKILLS_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }
```

- [ ] **Step 4: Implement the config knobs**

```python
# services/agentd-py/agentd/skills/config.py
from __future__ import annotations

import os


def _pos_int(env: str, default: int) -> int:
    raw = os.getenv(env, "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else default


def skills_catalog_max_chars() -> int:
    return _pos_int("AI_EDITOR_SKILLS_CATALOG_MAX_CHARS", 16000)


def skills_body_max_chars() -> int:
    return _pos_int("AI_EDITOR_SKILLS_BODY_MAX_CHARS", 20000)
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `source .venv/bin/activate && pytest tests/test_skills_config.py -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add services/agentd-py/agentd/chat/controller_factory.py services/agentd-py/agentd/skills/config.py services/agentd-py/tests/test_skills_config.py
git commit -m "feat(skills): AI_EDITOR_SKILLS_ENABLED flag + budget config knobs"
```

---

### Task 4: Catalog block in the controller system prompt + engine wiring

**Files:**
- Modify: `services/agentd-py/agentd/chat/controller_prompts.py` (`_SKILLS_BLOCK_TEMPLATE` + `format_controller_system_prompt` param)
- Modify: `services/agentd-py/agentd/reasoning/engine.py` (`__init__` gains `skill_catalog_loader`; `create_controller_step` resolves + passes the catalog)
- Modify: `services/agentd-py/agentd/chat/controller_factory.py` (`select_chat_handler` builds + threads the loader)
- Test: `services/agentd-py/tests/test_skills_prompt.py`

**Interfaces:**
- Consumes: `render_skills_catalog`, `select_catalog_for_budget` (Task 2), `skills_catalog_max_chars` (Task 3), `SkillCatalogLoader.load_catalog` (Task 1).
- Produces: `format_controller_system_prompt(tool_definitions, *, ..., skills_catalog: list[SkillManifest] | None = None)`; `DefaultReasoningEngine(..., skill_catalog_loader=None)`.

- [ ] **Step 1: Write the failing test**

```python
# services/agentd-py/tests/test_skills_prompt.py
from pathlib import Path

from agentd.chat.controller_prompts import format_controller_system_prompt
from agentd.skills.models import SkillManifest


def _m(name: str, desc: str) -> SkillManifest:
    return SkillManifest(name=name, description=desc, body_path=Path("x"), dir=Path("d"))


def test_catalog_block_present_when_skills_given() -> None:
    out = format_controller_system_prompt(
        [], skills_catalog=[_m("git-commit", "Make a commit.")]
    )
    assert "AVAILABLE SKILLS" in out
    assert "git-commit: Make a commit." in out
    assert "read_skill" in out  # teaching
    assert "scripts/" in out    # run_command worked example


def test_no_catalog_block_when_empty() -> None:
    out = format_controller_system_prompt([], skills_catalog=[])
    assert "AVAILABLE SKILLS" not in out
    out2 = format_controller_system_prompt([], skills_catalog=None)
    assert "AVAILABLE SKILLS" not in out2
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `source .venv/bin/activate && pytest tests/test_skills_prompt.py -v`
Expected: FAIL — `TypeError: ... unexpected keyword argument 'skills_catalog'`.

- [ ] **Step 3: Add the block template + param (controller_prompts.py)**

After `_INSTRUCTIONS_BLOCK_TEMPLATE` (around line 367), add:

```python
_SKILLS_BLOCK_HEADER = """

AVAILABLE SKILLS (specialized playbooks for this workspace):
Each line is a skill's name + when to use it. When a skill is relevant to the
current task, call read_skill(name) to load its full instructions into context.
A skill may bundle helper scripts under its scripts/ folder — run them with
run_command, e.g. run_command(command="python .ai-editor/skills/<name>/scripts/<file>.py").
"""
```

In `format_controller_system_prompt`, add the parameter and render after the instructions block:

```python
def format_controller_system_prompt(
    tool_definitions: list[dict[str, object]],
    *,
    task_subsystem_enabled: bool | None = None,
    memory_enabled: bool | None = None,
    project_instructions: str | None = None,
    skills_catalog: "list | None" = None,
) -> str:
```

…and just before `return base`:

```python
    if skills_catalog:
        from agentd.skills.catalog import render_skills_catalog
        rendered = render_skills_catalog(skills_catalog)
        if rendered:
            base += _SKILLS_BLOCK_HEADER + rendered
    return base
```

- [ ] **Step 4: Run the prompt test to verify it passes**

Run: `source .venv/bin/activate && pytest tests/test_skills_prompt.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Wire the engine (engine.py)**

In `DefaultReasoningEngine.__init__`, alongside `project_instructions_loader` (line ~71), add a parameter + store it:

```python
        skill_catalog_loader: object | None = None,
```
```python
        self._skill_catalog_loader = skill_catalog_loader
```

In `create_controller_step`, after resolving `instructions` (line ~265), resolve the budget-guarded catalog and pass it:

```python
        skills_catalog = None
        if self._skill_catalog_loader is not None:
            from agentd.skills.catalog import select_catalog_for_budget
            from agentd.skills.config import skills_catalog_max_chars
            full = self._skill_catalog_loader.load_catalog()  # type: ignore[attr-defined]
            shown, _hidden = select_catalog_for_budget(full, skills_catalog_max_chars())
            skills_catalog = shown
        system_instructions = format_controller_system_prompt(
            tool_definitions, project_instructions=instructions, skills_catalog=skills_catalog
        )
```

- [ ] **Step 6: Wire the factory (controller_factory.py)**

In `select_chat_handler`, mirroring the `project_instructions_loader` block (lines ~87-98), add:

```python
        from agentd.chat.controller_factory import is_skills_enabled  # local if needed
        from agentd.skills.loader import SkillCatalogLoader
        skill_catalog_loader = (
            SkillCatalogLoader(workspace_path) if is_skills_enabled() else None
        )
```

…and pass `skill_catalog_loader=skill_catalog_loader` into the `DefaultReasoningEngine(...)` construction (the same call that already takes `project_instructions_loader=`).

- [ ] **Step 7: Run the full controller-prompt + factory suites**

Run: `source .venv/bin/activate && pytest tests/test_skills_prompt.py tests/test_skills_config.py -v && pytest tests/ -k "controller_prompt or controller_factory" -q`
Expected: PASS, no regressions.

- [ ] **Step 8: Commit**

```bash
git add services/agentd-py/agentd/chat/controller_prompts.py services/agentd-py/agentd/reasoning/engine.py services/agentd-py/agentd/chat/controller_factory.py services/agentd-py/tests/test_skills_prompt.py
git commit -m "feat(skills): inject budget-guarded catalog into the controller system prompt"
```

---

### Task 5: SkillToolSource — the `read_skill` tool

**Files:**
- Create: `services/agentd-py/agentd/skills/tool_source.py`
- Test: `services/agentd-py/tests/test_skills_tool_source.py`

**Interfaces:**
- Consumes: `SkillCatalogLoader` (Task 1), `skills_body_max_chars` (Task 3), `ToolDefinition`/`ToolOutput` from `agentd.tools.registry`.
- Produces: `SkillToolSource(loader, active_skills: dict[str, str])` — a `ToolSource` with `name="skills"`, owning `read_skill`. `execute("read_skill", {"name": ...})` reads + caps the body, sets `active_skills[name] = body`, returns it.

- [ ] **Step 1: Write the failing test**

```python
# services/agentd-py/tests/test_skills_tool_source.py
import asyncio
from pathlib import Path

from agentd.skills.loader import SkillCatalogLoader
from agentd.skills.tool_source import SkillToolSource


def _write_skill(root: Path, name: str, body: str) -> None:
    d = root / ".ai-editor" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: A skill.\n---\n{body}\n", encoding="utf-8"
    )


def test_definitions_expose_read_skill(tmp_path: Path) -> None:
    src = SkillToolSource(SkillCatalogLoader(tmp_path), {})
    assert [d.name for d in src.definitions()] == ["read_skill"]
    assert src.owns("read_skill") and not src.owns("read_file")


def test_read_skill_returns_body_and_marks_active(tmp_path: Path) -> None:
    _write_skill(tmp_path, "git-commit", "STEP 1: stage. STEP 2: commit.")
    active: dict[str, str] = {}
    src = SkillToolSource(SkillCatalogLoader(tmp_path), active)
    out = asyncio.run(src.execute("read_skill", {"name": "git-commit"}))
    assert not out.is_error
    assert "STEP 1: stage." in out.output
    assert "git-commit" in active and "STEP 1" in active["git-commit"]


def test_read_skill_unknown_name_is_error(tmp_path: Path) -> None:
    src = SkillToolSource(SkillCatalogLoader(tmp_path), {})
    out = asyncio.run(src.execute("read_skill", {"name": "nope"}))
    assert out.is_error and "no skill" in out.output.lower()


def test_read_skill_caps_large_body(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_EDITOR_SKILLS_BODY_MAX_CHARS", "50")
    _write_skill(tmp_path, "big", "x" * 500)
    src = SkillToolSource(SkillCatalogLoader(tmp_path), {})
    out = asyncio.run(src.execute("read_skill", {"name": "big"}))
    assert "truncated" in out.output and len(out.output) < 200
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `source .venv/bin/activate && pytest tests/test_skills_tool_source.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agentd.skills.tool_source'`.

- [ ] **Step 3: Implement**

```python
# services/agentd-py/agentd/skills/tool_source.py
from __future__ import annotations

from agentd.skills.config import skills_body_max_chars
from agentd.tools.registry import ToolDefinition, ToolOutput

_READ_SKILL_DEF = ToolDefinition(
    name="read_skill",
    description=(
        "Load a skill's full SKILL.md instructions into context. Call with the skill "
        "name from the AVAILABLE SKILLS catalog when that skill is relevant to the task."
    ),
    parameters={
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    },
)


class SkillToolSource:
    """ToolSource exposing read_skill. Activated bodies land in the shared active_skills
    dict the controller loop injects into the dynamic tail each iteration."""

    name = "skills"

    def __init__(self, loader: object, active_skills: dict[str, str]) -> None:
        self._loader = loader
        self._active = active_skills

    def definitions(self) -> list[ToolDefinition]:
        return [_READ_SKILL_DEF]

    def owns(self, tool: str) -> bool:
        return tool == "read_skill"

    async def execute(self, tool: str, args: dict[str, object]) -> ToolOutput:
        if tool != "read_skill":
            return ToolOutput(output=f"Error: unknown tool '{tool}'", is_error=True)
        name = str(args.get("name", "")).strip()
        catalog = self._loader.load_catalog()  # type: ignore[attr-defined]
        manifest = next((m for m in catalog if m.name == name), None)
        if manifest is None:
            avail = ", ".join(m.name for m in catalog) or "(none)"
            return ToolOutput(
                output=f"Error: no skill named '{name}'. Available: {avail}", is_error=True
            )
        try:
            body = manifest.body_path.read_text(encoding="utf-8")
        except OSError as exc:
            return ToolOutput(output=f"Error: cannot read skill '{name}': {exc}", is_error=True)
        cap = skills_body_max_chars()
        if len(body) > cap:
            body = body[:cap] + f"\n\n[... skill '{name}' truncated at {cap} chars ...]"
        self._active[name] = body
        return ToolOutput(output=body)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `source .venv/bin/activate && pytest tests/test_skills_tool_source.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add services/agentd-py/agentd/skills/tool_source.py services/agentd-py/tests/test_skills_tool_source.py
git commit -m "feat(skills): SkillToolSource — read_skill tool + active-skill marking"
```

---

### Task 6: Active-skills tail injection + ControllerLoop wiring

**Files:**
- Modify: `services/agentd-py/agentd/chat/controller_prompts.py` (`build_controller_step_payload` — inject `active_skills`)
- Modify: `services/agentd-py/agentd/chat/controller.py` (`_run_loop` creates + seeds the shared `active_skills` dict at the `ledger`/`ControllerLoop` construction site, line ~320; `_build_registry` registers `SkillToolSource`)
- Modify: `services/agentd-py/agentd/chat/controller_loop.py` (`__init__` stores `active_skills`; the inner loop sets `plan_context["active_skills"]` each iteration)
- Test: `services/agentd-py/tests/test_skills_tail.py`

**Interfaces:**
- Consumes: `SkillToolSource` (Task 5), `SkillCatalogLoader` (Task 1).
- Produces: `build_controller_step_payload` emits `payload["active_skills"] = [{"name", "body"}]` (tail, when non-empty). The shared `active_skills: dict[str,str]` is **created in `ChatController._run_loop`** (alongside `ledger`, ~line 320), seeded from `forced_skills`, and passed to **both** `_build_registry(active_skills=...)` and `ControllerLoop(active_skills=...)` so the `SkillToolSource` and the loop mutate/read the **same object**. The loop writes `plan_context["active_skills"]` each iteration (next to the `recalled_memories`/`todo_status` sites, controller_loop.py ~lines 319/346).

- [ ] **Step 1: Write the failing test (payload tail)**

```python
# services/agentd-py/tests/test_skills_tail.py
from agentd.chat.controller_prompts import build_controller_step_payload


def test_active_skills_ride_the_tail_after_goal() -> None:
    ctx = {
        "workspace_path": "/ws",
        "goal": "do it",
        "active_skills": [{"name": "git-commit", "body": "STEP 1..."}],
    }
    payload = build_controller_step_payload(ctx, [], [], phase="DECIDE")
    assert payload["active_skills"] == [{"name": "git-commit", "body": "STEP 1..."}]
    keys = list(payload.keys())
    assert keys.index("active_skills") > keys.index("goal")  # tail, after goal


def test_active_skills_omitted_when_empty() -> None:
    payload = build_controller_step_payload(
        {"workspace_path": "/ws", "goal": "x", "active_skills": []}, [], [], phase="DECIDE"
    )
    assert "active_skills" not in payload
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `source .venv/bin/activate && pytest tests/test_skills_tail.py -v`
Expected: FAIL — `KeyError`/`assert 'active_skills' in payload` fails.

- [ ] **Step 3: Inject in the payload builder (controller_prompts.py)**

In `build_controller_step_payload`, after the `goal` line (line ~442) and near the `todo_status` tail block (~445), add:

```python
    active_skills = plan_context.get("active_skills")
    if isinstance(active_skills, list) and active_skills:
        payload["active_skills"] = active_skills
```

- [ ] **Step 4: Run the payload test to verify it passes**

Run: `source .venv/bin/activate && pytest tests/test_skills_tail.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Register SkillToolSource in the controller (controller.py)**

In `_build_registry` (line ~176), accept an optional `active_skills` dict + build the source when skills are enabled. Change the signature to add `active_skills: dict[str, str] | None = None`, and after the memory source append (line ~192):

```python
        from agentd.chat.controller_factory import is_skills_enabled
        if is_skills_enabled() and active_skills is not None:
            from agentd.skills.loader import SkillCatalogLoader
            from agentd.skills.tool_source import SkillToolSource
            sources.append(SkillToolSource(SkillCatalogLoader(self._workspace_path), active_skills))
```

- [ ] **Step 6a: Create + seed the shared dict in ChatController._run_loop (controller.py)**

At the `ControllerLoop` construction site (controller.py ~line 320, where `ledger` is already built), create the shared dict, seed it from `forced_skills` (threaded in by Task 8), and pass it to both the registry and the loop:

```python
        active_skills: dict[str, str] = {}
        if is_skills_enabled() and forced_skills:
            catalog = SkillCatalogLoader(self._workspace_path).load_catalog()
            for name in forced_skills:
                manifest = next((m for m in catalog if m.name == name), None)
                if manifest is not None:
                    try:
                        active_skills[name] = manifest.body_path.read_text(encoding="utf-8")
                    except OSError:
                        pass
```

(Add the imports at the top of `controller.py`: `from agentd.chat.controller_factory import is_skills_enabled` and `from agentd.skills.loader import SkillCatalogLoader`.) Then change the construction:

```python
        loop = ControllerLoop(
            ...,
            self._build_registry(command_cb, ledger, todo_persist_cb, active_skills=active_skills),
            ...,
            active_skills=active_skills,
        )
```

- [ ] **Step 6b: Store + inject in the loop (controller_loop.py)**

In `ControllerLoop.__init__` (line ~183), accept and store the dict:

```python
        active_skills: dict[str, str] | None = None,
```
```python
        self._active_skills = active_skills if active_skills is not None else {}
```

In the inner loop, next to where `plan_context["recalled_memories"]` / `plan_context["todo_status"]` are set (lines ~319/346, before `create_controller_step` at ~355), add:

```python
        plan_context["active_skills"] = [
            {"name": n, "body": b} for n, b in self._active_skills.items()
        ]
```

(Mirror the exact assembly site of `todo_status` — same indentation, same per-iteration position.)

- [ ] **Step 7: Run the controller + loop suites**

Run: `source .venv/bin/activate && pytest tests/ -k "controller_loop or controller and skill or skills_tail" -q`
Expected: PASS, no regressions. Then the full controller suite: `pytest tests/test_chat_controller*.py -q`.

- [ ] **Step 8: Commit**

```bash
git add services/agentd-py/agentd/chat/controller_prompts.py services/agentd-py/agentd/chat/controller.py services/agentd-py/agentd/chat/controller_loop.py services/agentd-py/tests/test_skills_tail.py
git commit -m "feat(skills): inject active-skill bodies into the dynamic payload tail"
```

---

### Task 7: `GET /v1/skills` route + `/v1/config` skillsEnabled

**Files:**
- Modify: `services/agentd-py/agentd/api/routes.py` (`GET /v1/skills`; add `skills_enabled` to `/v1/config`)
- Test: `services/agentd-py/tests/test_skills_routes.py`

**Interfaces:**
- Consumes: `is_skills_enabled` (Task 3), `SkillCatalogLoader` (Task 1).
- Produces: `GET /v1/skills?workspace=<path>` → `{"skills": [{"name", "description"}]}` (gated-empty when off); `/v1/config` includes `"skills_enabled": bool`.

- [ ] **Step 1: Write the failing test**

```python
# services/agentd-py/tests/test_skills_routes.py
from pathlib import Path

from fastapi.testclient import TestClient

from agentd.chat.app_factory import build_app  # existing test app factory


def _write_skill(ws: Path, name: str, desc: str) -> None:
    d = ws / ".ai-editor" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\nbody\n",
                                encoding="utf-8")


def test_skills_route_lists_catalog(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AI_EDITOR_SKILLS_ENABLED", "1")
    _write_skill(tmp_path, "git-commit", "Make a commit.")
    client = TestClient(build_app())
    r = client.get("/v1/skills", params={"workspace": str(tmp_path)})
    assert r.status_code == 200
    assert r.json()["skills"] == [{"name": "git-commit", "description": "Make a commit."}]


def test_skills_route_gated_empty_when_off(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("AI_EDITOR_SKILLS_ENABLED", raising=False)
    _write_skill(tmp_path, "x", "y")
    client = TestClient(build_app())
    r = client.get("/v1/skills", params={"workspace": str(tmp_path)})
    assert r.status_code == 200 and r.json()["skills"] == []


def test_config_exposes_skills_enabled(monkeypatch) -> None:
    monkeypatch.setenv("AI_EDITOR_SKILLS_ENABLED", "1")
    client = TestClient(build_app())
    assert client.get("/v1/config").json()["skills_enabled"] is True
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `source .venv/bin/activate && pytest tests/test_skills_routes.py -v`
Expected: FAIL — 404 on `/v1/skills` / `KeyError: 'skills_enabled'`.

- [ ] **Step 3: Implement the route + config field (routes.py)**

Find the existing `/v1/config` handler (grep `def .*config` / `"memory_enabled"`). Add `"skills_enabled": is_skills_enabled()` to its returned dict (import `is_skills_enabled` from `agentd.chat.controller_factory` at the top of `routes.py` where the other factory flags are imported).

Add a route next to the other read-only GETs:

```python
    @router.get("/v1/skills")
    async def list_skills(workspace: str) -> dict:
        from agentd.chat.controller_factory import is_skills_enabled
        if not is_skills_enabled():
            return {"skills": []}
        from agentd.skills.loader import SkillCatalogLoader
        catalog = SkillCatalogLoader(workspace).load_catalog()
        return {"skills": [{"name": m.name, "description": m.description} for m in catalog]}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `source .venv/bin/activate && pytest tests/test_skills_routes.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add services/agentd-py/agentd/api/routes.py services/agentd-py/tests/test_skills_routes.py
git commit -m "feat(skills): GET /v1/skills + skills_enabled in /v1/config"
```

---

### Task 8: `forced_skills` message field → deterministic forced-load

**Files:**
- Modify: `services/agentd-py/agentd/api/routes.py` (`post_chat_message` parses `forced_skills`)
- Modify: `services/agentd-py/agentd/chat/controller.py` (`handle_message` accepts `forced_skills`, threads to the loop)
- Test: `services/agentd-py/tests/test_skills_forced.py`

**Interfaces:**
- Consumes: the `active_skills` seeding in `ControllerLoop` (Task 6).
- Produces: `handle_message(thread_id, message, channel_id, step_review=None, forced_skills: list[str] | None = None)`; the message body accepts `"forced_skills": ["name", ...]`.

- [ ] **Step 1: Write the failing test**

```python
# services/agentd-py/tests/test_skills_forced.py
import inspect

from agentd.chat.controller import ChatController


def test_handle_message_accepts_forced_skills() -> None:
    sig = inspect.signature(ChatController.handle_message)
    assert "forced_skills" in sig.parameters
```

(An end-to-end forced-load assertion is covered by the live smoke; the unit gate here is the surface — keep it cheap and deterministic. A fuller integration test belongs with the controller-loop suite once the loop fixture is in scope.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `source .venv/bin/activate && pytest tests/test_skills_forced.py -v`
Expected: FAIL — `forced_skills` not in signature.

- [ ] **Step 3: Parse the field in the route (routes.py)**

In `post_chat_message` (line ~1193), after the `step_review` parse (line ~1197):

```python
            _raw_forced = request.get("forced_skills")
            forced_skills = [str(s) for s in _raw_forced] if isinstance(_raw_forced, list) else None
```

Pass `forced_skills=forced_skills` into both `handle_message(...)` call sites (the streaming + the awaited path, lines ~1216 and ~1252).

- [ ] **Step 4: Thread through the controller (controller.py)**

Add `forced_skills: list[str] | None = None` to `handle_message` (line ~244) and thread it down to `_run_loop` (the method that constructs `ControllerLoop` at ~line 320), where Task 6 Step 6a consumes it to seed the shared `active_skills` dict. Add the same `forced_skills` parameter to any intermediate method between `handle_message` and `_run_loop` (mirror how `step_review`/`turn_id` are passed down the same chain).

- [ ] **Step 5: Run the test + controller suite**

Run: `source .venv/bin/activate && pytest tests/test_skills_forced.py tests/test_chat_controller*.py -q`
Expected: PASS, no regressions.

- [ ] **Step 6: Commit**

```bash
git add services/agentd-py/agentd/api/routes.py services/agentd-py/agentd/chat/controller.py services/agentd-py/tests/test_skills_forced.py
git commit -m "feat(skills): forced_skills message field → deterministic /skill forced-load"
```

---

### Task 9: editor-client — `forcedSkills` option + `listSkills` client

**Files:**
- Modify: `apps/editor-client/src/contracts/task-contracts.ts` (`sendChatMessage` option; `SkillSummary` Zod; `listSkills` on the interface)
- Modify: `apps/editor-client/src/client/http-backend-client.ts` (`sendChatMessage` body; `listSkills` impl)
- Test: `apps/editor-client/src/client/http-backend-client.test.ts` (or the existing client test file)

**Interfaces:**
- Consumes: `GET /v1/skills` (Task 7), the `forced_skills` field (Task 8).
- Produces: `sendChatMessage(threadId, message, signal?, options?: { stepReview?: boolean; forcedSkills?: string[] })`; `listSkills(workspace: string): Promise<SkillSummary[]>` where `SkillSummary = { name: string; description: string }`.

- [ ] **Step 1: Write the failing test**

```typescript
// in the existing http-backend-client test file
import { describe, it, expect, vi } from "vitest";
import { HttpBackendClient } from "./http-backend-client";

describe("skills", () => {
  it("sendChatMessage includes forced_skills in the body", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response("", { status: 200, headers: { "content-type": "text/event-stream" } }),
    );
    const client = new HttpBackendClient("http://x", fetchMock as unknown as typeof fetch);
    const it2 = client.sendChatMessage("t1", "hi", undefined, { forcedSkills: ["git-commit"] });
    await it2[Symbol.asyncIterator]().next();
    const body = JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string);
    expect(body.forced_skills).toEqual(["git-commit"]);
  });

  it("listSkills maps the response", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ skills: [{ name: "a", description: "b" }] }), {
        status: 200, headers: { "content-type": "application/json" },
      }),
    );
    const client = new HttpBackendClient("http://x", fetchMock as unknown as typeof fetch);
    expect(await client.listSkills("/ws")).toEqual([{ name: "a", description: "b" }]);
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npm run -w @ai-editor/editor-client test -- http-backend-client`
Expected: FAIL — `listSkills` not a function / `forced_skills` undefined.

- [ ] **Step 3: Add the Zod type + interface (task-contracts.ts)**

Near the other summary schemas:

```typescript
export const SkillSummarySchema = z.object({ name: z.string(), description: z.string() });
export type SkillSummary = z.infer<typeof SkillSummarySchema>;
```

Extend the `sendChatMessage` signature option and add `listSkills` to `BackendTaskClient`:

```typescript
  sendChatMessage(threadId: string, message: string, signal?: AbortSignal,
    options?: { stepReview?: boolean; forcedSkills?: string[] }): AsyncIterable<StreamEvent>;
  listSkills(workspace: string): Promise<SkillSummary[]>;
```

- [ ] **Step 4: Implement (http-backend-client.ts)**

In `sendChatMessage` body assembly (line ~582), add:

```typescript
          ...(options?.forcedSkills && options.forcedSkills.length
            ? { forced_skills: options.forcedSkills } : {}),
```

Add the method:

```typescript
  async listSkills(workspace: string): Promise<SkillSummary[]> {
    const res = await this._fetch(`${this._baseUrl}/v1/skills?workspace=${encodeURIComponent(workspace)}`);
    const json = await res.json();
    return z.array(SkillSummarySchema).parse((json as { skills: unknown[] }).skills ?? []);
  }
```

(Match the existing `_fetch`/parse idiom in this file — use whatever the other GET methods use.)

- [ ] **Step 5: Run the test + build**

Run: `npm run -w @ai-editor/editor-client test -- http-backend-client && npm run -w @ai-editor/editor-client build`
Expected: PASS + clean build (required before the extension typecheck).

- [ ] **Step 6: Commit**

```bash
git add apps/editor-client/src/
git commit -m "feat(skills): editor-client forcedSkills option + listSkills client"
```

---

### Task 10: VS Code composer — `/skill` autocomplete + forced-load + collision rule

**Files:**
- Modify: `apps/vscode-extension/src/controller.ts` (`listSkills` passthrough; `skillsEnabled` from `/v1/config`)
- Modify: `apps/vscode-extension/src/chat-panel.ts` + `src/extension.ts` (route a `listSkills` webview message → post `skillList`)
- Modify: `apps/vscode-extension/webview-ui/src/components/InputArea.tsx` (merge skills into `/` autocomplete; selecting a skill sets `forcedSkills`, not inline expansion; prompt-file wins on name collision)
- Test: `apps/vscode-extension/webview-ui/src/components/InputArea.test.tsx` (or the existing composer test) + a `controller.ts` unit test

**Interfaces:**
- Consumes: `listSkills` (Task 9), `skills_enabled` config (Task 7).
- Produces: composer `/` autocomplete lists prompt files + skills (skills badged); choosing a skill adds its name to the turn's `forcedSkills` and clears the `/token` (no inline body expansion); a name present as BOTH resolves to the prompt-file inline expansion.

- [ ] **Step 1: Write the failing test (collision + selection behavior)**

```typescript
// webview-ui composer test — adapt to the existing InputArea test harness
import { describe, it, expect } from "vitest";
import { mergeSlashSuggestions, resolveSlashSelection } from "./InputArea";

describe("slash suggestions", () => {
  it("lists prompts and skills, badging skills", () => {
    const out = mergeSlashSuggestions(["review"], [{ name: "git-commit", description: "x" }]);
    expect(out).toEqual([
      { name: "review", kind: "prompt" },
      { name: "git-commit", kind: "skill" },
    ]);
  });

  it("prompt file wins on name collision", () => {
    const out = mergeSlashSuggestions(["deploy"], [{ name: "deploy", description: "x" }]);
    expect(out).toEqual([{ name: "deploy", kind: "prompt" }]);
  });

  it("selecting a skill returns a forcedSkill action, not inline expansion", () => {
    const action = resolveSlashSelection({ name: "git-commit", kind: "skill" });
    expect(action).toEqual({ type: "forceSkill", name: "git-commit" });
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npm run -w @ai-editor/vscode-extension test -- InputArea`
Expected: FAIL — `mergeSlashSuggestions`/`resolveSlashSelection` not exported.

- [ ] **Step 3: Implement the pure helpers (InputArea.tsx)**

```typescript
export type SlashSuggestion = { name: string; kind: "prompt" | "skill" };

export function mergeSlashSuggestions(
  promptNames: string[], skills: { name: string; description: string }[],
): SlashSuggestion[] {
  const prompts = new Set(promptNames);
  const out: SlashSuggestion[] = promptNames.map((name) => ({ name, kind: "prompt" }));
  for (const s of skills) {
    if (!prompts.has(s.name)) out.push({ name: s.name, kind: "skill" }); // prompt wins on collision
  }
  return out;
}

export function resolveSlashSelection(
  s: SlashSuggestion,
): { type: "expandPrompt"; name: string } | { type: "forceSkill"; name: string } {
  return s.kind === "prompt"
    ? { type: "expandPrompt", name: s.name }
    : { type: "forceSkill", name: s.name };
}
```

- [ ] **Step 4: Wire selection into the composer state**

In the `InputArea` component, when the user selects a `/` suggestion, call `resolveSlashSelection`:
- `expandPrompt` → the existing P1 path (post `expandPrompt`, replace draft inline).
- `forceSkill` → push `name` into a component-level `forcedSkills: string[]` state, remove the `/token` from the draft, and show a small chip ("skill: git-commit"). On send, include `forcedSkills` in the `sendMessage` payload to the host (which forwards to `sendChatMessage(..., { forcedSkills })`).

(Reference the existing P1 `promptExpanded` handling for the host round-trip; the skill list arrives via a `skillList` webview message — Step 5.)

- [ ] **Step 5: Host plumbing (controller.ts / chat-panel.ts / extension.ts)**

- `controller.ts`: add `async listSkills(): Promise<{name,string}[]>` delegating to `client.listSkills(this.workspacePath)`; expose `skillsEnabled` from the `/v1/config` fetch (mirror `memoryEnabled`/`taskSubsystemEnabled`).
- `chat-panel.ts`: on a `listSkills` webview message, call `controller.listSkills()` and `postMessage({ type: "skillList", skills })`. Gate the affordance behind `skillsEnabled`.
- `extension.ts`: register the `aiEditor.skillsEnabled` `when`-context key from `/v1/config` (mirror the `memoryEnabled` registration).

- [ ] **Step 6: Run composer test + extension typecheck + builds**

Run:
```bash
npm run -w @ai-editor/editor-client build
npm run -w @ai-editor/vscode-extension test -- InputArea
npm run -w @ai-editor/vscode-extension typecheck
npm run build
```
Expected: PASS + clean typecheck/build.

- [ ] **Step 7: Commit**

```bash
git add apps/vscode-extension/src/ apps/vscode-extension/webview-ui/src/
git commit -m "feat(skills): /skill autocomplete + forced-load + prompt-file collision rule"
```

---

### Task 11: Full-suite green + CLAUDE.md docs

**Files:**
- Modify: `CLAUDE.md` (new subsection under the chat controller area)

- [ ] **Step 1: Run the full Python suite**

Run: `cd services/agentd-py && source .venv/bin/activate && pytest -q`
Expected: all pass (read the actual FAILED/summary lines — never trust a piped exit code). Investigate any regression in isolation.

- [ ] **Step 2: Run the full TS suites + typecheck**

Run: `npm run test && npm run typecheck && npm run build`
Expected: all green.

- [ ] **Step 3: Document in CLAUDE.md**

Add a subsection "Agent Skills (P2, copilot-parity roadmap)" near the project-instructions/prompt-files section, covering: discovery dir (`.ai-editor/skills/<name>/SKILL.md`), the always-on catalog → `read_skill` → `active_skills` tail flow, scripts via `run_command`, `/skill` forced-load via `forced_skills`, the budget guard + dormant ranking primitive, and the flags (`AI_EDITOR_SKILLS_ENABLED` default off, `*_CATALOG_MAX_CHARS`, `*_BODY_MAX_CHARS`). Mirror the depth of the existing P1 subsection.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(skills): document P2 agent-skills architecture in CLAUDE.md"
```

---

### Task 12: Live smoke (manual, per spec §6)

Not a code task — drive a real backend + dev host. Mirror the P1 live-smoke recipe (`scripts/stress/start-backend.sh` with `AI_EDITOR_SKILLS_ENABLED=1`, then the VS Code dev host via CDP).

- [ ] **1. Model-driven activation:** drop `.ai-editor/skills/git-commit/SKILL.md` with a distinctive directive; a matching chat turn calls `read_skill` and the directive changes behavior (confirm in the controller-turn artifact).
- [ ] **2. Script execution:** a skill body that says to run `scripts/check.sh` → the model emits `run_command` for it and it runs through the shell-policy gate.
- [ ] **3. Forced-load:** `/git-commit` in the composer → the body is active from turn 1 (artifact shows `active_skills` populated at iteration -00) without the model choosing it.
- [ ] **4. Self-updating:** add a second skill mid-session → the next turn's catalog includes it (no restart).
- [ ] **5. Kill-switch:** `AI_EDITOR_SKILLS_ENABLED=0` → no catalog block, `read_skill` absent from tools, composer skills affordance gone.

---

## Notes for the implementer

- **Reference patterns, don't reinvent:** the loader mirrors `agentd/instructions/loader.py`; the tool source mirrors `agentd/memory/tool_source.py`; the catalog block mirrors `_MEMORY_BLOCK`/`_INSTRUCTIONS_BLOCK_TEMPLATE`; the tail injection mirrors `recalled_memories` in `build_controller_step_payload`; the flag mirrors `is_memory_enabled`.
- **`InMemoryTaskStore.get()` returns the same object** — fine here; no task-store semantics in this feature.
- **Read the exact `controller_loop.py` plan_context assembly** before Task 6 Step 6 — the `recalled_memories`/`todo_status` sites are the template; match them so the KV tail ordering is preserved.
- **Never `pytest | tail`** — it masks the exit code; read the summary lines.
</content>
</invoke>
