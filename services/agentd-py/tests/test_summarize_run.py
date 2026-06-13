import pytest

from agentd.orchestrator.scripted_engine import ScriptedReasoningEngine


@pytest.mark.asyncio
async def test_scripted_summarize_run_returns_headline_and_points():
    eng = ScriptedReasoningEngine(
        plan={"analysis": "a", "steps": [], "expected_files": [], "stop_conditions": []},
        patches=[],
        tool_step_responses=[],
        run_narrative={"headline": "Did the thing", "points": ["added foo", "ran tests"]},
    )
    out = await eng.summarize_run(
        goal="g", outcome="succeeded", run_events=[], deviations=[], modified_files=["a.py"],
    )
    assert out["headline"] == "Did the thing"
    assert out["points"] == ["added foo", "ran tests"]
