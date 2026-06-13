from agentd.reasoning.tool_prompts import AGENT_STEP_RESPONSE_SCHEMA
from agentd.tools.loop import VerifyResult


def test_schema_has_step_summary_field():
    props = AGENT_STEP_RESPONSE_SCHEMA["properties"]
    assert "step_summary" in props


def test_verify_result_carries_step_summary():
    vr = VerifyResult(patch_document={}, touched_files=[], verified=True,
                      test_output="", tool_trace=None, step_summary="did the thing")
    assert vr.step_summary == "did the thing"
