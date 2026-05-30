from __future__ import annotations

import pytest

from agentd.domain.models import PatchCandidateV2
from agentd.patch.engine import PatchEngine


def _candidate(ops: list[dict]) -> PatchCandidateV2:
    return PatchCandidateV2(candidate_id="c1", patch_ops=ops)


@pytest.mark.asyncio
async def test_split_try_except_across_ops_is_accepted(tmp_path):
    # op0 opens a try: (invalid alone). op1 adds the matching except: .
    # Both anchors match; the FINAL file is valid -> must apply.
    f = tmp_path / "m.py"
    f.write_text("def g():\n    do_thing()\n    after()\n", encoding="utf-8")
    engine = PatchEngine()
    candidate = _candidate([
        {"op": "search_replace", "file": "m.py",
         "search": "    do_thing()\n    after()",
         "replace": "    try:\n        do_thing()\n        after()",
         "reason": "open try"},
        {"op": "search_replace", "file": "m.py",
         "search": "        after()",
         "replace": "        after()\n    except Exception:\n        pass",
         "reason": "close with except"},
    ])

    result = await engine.apply_patch_candidate(tmp_path, candidate, allowed_files={"m.py"})

    assert result.touched_files == ["m.py"]
    text = f.read_text(encoding="utf-8")
    assert "try:" in text and "except Exception:" in text
    # final content is valid python
    compile(text, "m.py", "exec")


@pytest.mark.asyncio
async def test_malformed_final_result_is_rejected_atomically(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("x = call()\n", encoding="utf-8")
    engine = PatchEngine()
    candidate = _candidate([
        {"op": "search_replace", "file": "m.py", "search": "x = call()",
         "replace": "x = call(", "reason": "unbalanced paren"},
    ])
    with pytest.raises(RuntimeError, match="preflight failed"):
        await engine.apply_patch_candidate(tmp_path, candidate, allowed_files={"m.py"})
    # nothing written
    assert f.read_text(encoding="utf-8") == "x = call()\n"


@pytest.mark.asyncio
async def test_single_valid_search_replace_still_applies(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("A = 1\n", encoding="utf-8")
    engine = PatchEngine()
    candidate = _candidate([
        {"op": "search_replace", "file": "m.py", "search": "A = 1", "replace": "A = 2", "reason": "ok"},
    ])
    result = await engine.apply_patch_candidate(tmp_path, candidate, allowed_files={"m.py"})
    assert result.touched_files == ["m.py"]
    assert f.read_text(encoding="utf-8") == "A = 2\n"
