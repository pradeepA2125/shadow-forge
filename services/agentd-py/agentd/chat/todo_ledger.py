"""TodoLedger — per-request checklist the controller grinds to completion.

Mutated by the write_todos tool (full-list rewrite), re-surfaced into every loop
iteration's payload tail, enforced by ControllerLoop's submit_changes gate, and
shown to the user via /live. Plain state object: no I/O (storage persists via
to_json/from_json). Five states; blocked/cancelled adopted from the Agenda plan.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

# blocked = parked with an unblock reason in `note`; cancelled = abandoned but kept
# in the list (audit). The gate's pending set is pending|in_progress ONLY, so neither
# blocked nor cancelled nor done can deadlock the loop.
_STATUSES: tuple[str, ...] = ("pending", "in_progress", "done", "blocked", "cancelled")
_GLYPH: dict[str, str] = {
    "pending": "☐", "in_progress": "▶", "done": "✓", "blocked": "⛔", "cancelled": "~"}


@dataclass
class TodoItem:
    title: str
    status: str = "pending"
    note: str = ""  # holds evidence (done), unblock reason (blocked), or cancel reason


@dataclass
class TodoLedger:
    items: list[TodoItem] = field(default_factory=list)

    def replace(self, items: list[TodoItem]) -> None:
        """Full-list rewrite — the model resends the whole list each write_todos call."""
        self.items = list(items)

    def pending(self) -> list[TodoItem]:
        return [i for i in self.items if i.status in ("pending", "in_progress")]

    def render(self) -> str:
        """Compact one-line status for the payload tail; '' when no list exists."""
        if not self.items:
            return ""
        cells = " ".join(f"[{_GLYPH.get(i.status, '☐')} {i.title}]" for i in self.items)
        n_done = sum(1 for i in self.items if i.status == "done")
        return f"{len(self.items)} items ({n_done} done) — {cells}"

    def to_json(self) -> str:
        return json.dumps(
            [{"title": i.title, "status": i.status, "note": i.note} for i in self.items])

    @classmethod
    def from_json(cls, raw: str | None) -> "TodoLedger":
        if not raw:
            return cls()
        return cls(items=[
            TodoItem(
                title=str(d["title"]),
                status=str(d.get("status", "pending")),
                note=str(d.get("note", "")),
            )
            for d in json.loads(raw)
        ])
