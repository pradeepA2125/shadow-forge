from pathlib import Path

from agentd.skills.catalog import (
    rank_skills_by_relevance,
    render_skills_catalog,
    select_catalog_for_budget,
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
    # Fake embedder: returns a 2-D "embedding" keyed by a substring marker.
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
