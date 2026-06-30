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
