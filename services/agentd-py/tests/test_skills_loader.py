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
    import os
    import time
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
