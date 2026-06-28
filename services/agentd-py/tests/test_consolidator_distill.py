import pytest

from agentd.memory.consolidator import _CONSOLIDATION_SYSTEM, make_engine_consolidator


def test_prompt_has_research_driven_elements():
    p = _CONSOLIDATION_SYSTEM
    assert "EXAMPLE" in p  # few-shot input→output (Mem0 technique)
    assert "NEVER output these example notes" in p  # anti-leak guard
    assert "present-tense" in p  # declarative phrasing
    assert all(k in p for k in ("episodic", "semantic", "procedural"))  # taxonomy intact


class _FakeTransport:
    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    async def generate_json(self, *, model, schema_name, schema, system_instructions,
                            user_payload, on_thinking=None):
        self.calls.append((model, system_instructions, user_payload))
        return self._payload


@pytest.mark.asyncio
async def test_distill_parses_candidates():
    t = _FakeTransport({"memories": [
        {"kind": "semantic", "content": "patch ops in patch/engine.py",
         "entities": ["patch/engine.py"], "importance": 8, "contradicts": None}]})
    distill = make_engine_consolidator(t, "m1")
    out = await distill("transcript text", [])
    assert len(out) == 1 and out[0].kind == "semantic" and out[0].importance == 8
    # single-key transcript payload (echo-hardening)
    _model, _sys, payload = t.calls[0]
    assert list(payload.keys()) == ["transcript"]


@pytest.mark.asyncio
async def test_distill_best_effort_on_garbage():
    class Boom:
        async def generate_json(self, **kw):
            raise RuntimeError("provider down")

    distill = make_engine_consolidator(Boom(), "m1")
    assert await distill("x", []) == []


@pytest.mark.asyncio
async def test_distill_skips_bad_items_keeps_good():
    # partial garbage: a malformed item must not discard the valid ones.
    t = _FakeTransport({"memories": [
        {"kind": "semantic", "content": "good", "entities": [], "importance": 7},
        {"nonsense": True},  # invalid candidate
        "not even a dict",
    ]})
    distill = make_engine_consolidator(t, "m1")
    out = await distill("x", [])
    assert len(out) == 1 and out[0].content == "good"


@pytest.mark.asyncio
async def test_distill_clamps_importance():
    t = _FakeTransport({"memories": [
        {"kind": "semantic", "content": "a", "entities": [], "importance": 99},
        {"kind": "semantic", "content": "b", "entities": [], "importance": -3},
    ]})
    distill = make_engine_consolidator(t, "m1")
    out = await distill("x", [])
    assert {m.importance for m in out} == {10, 1}  # clamped to [1, 10]
