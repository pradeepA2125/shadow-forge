from __future__ import annotations

from pathlib import Path

import pytest

from agentd.domain.models import PatchDocument
from agentd.patch.engine import PatchEngine
from agentd.patch.policy import PatchPolicyViolation


@pytest.mark.asyncio
async def test_patch_engine_applies_operations(tmp_path: Path) -> None:
    engine = PatchEngine()

    target = tmp_path / "a.txt"
    target.write_text("line1\nline2\nline3", encoding="utf-8")

    patch = PatchDocument.model_validate(
        {
            "patch_ops": [
                {
                    "op": "replace_range",
                    "file": "a.txt",
                    "anchor": {"start_line": 2, "end_line": 2},
                    "content": "replaced",
                    "reason": "test",
                },
                {
                    "op": "create_file",
                    "file": "nested/b.txt",
                    "content": "hello",
                    "reason": "test",
                },
            ]
        }
    )

    result = await engine.apply_patch_document(tmp_path, patch)
    assert set(result.touched_files) == {"a.txt", "nested/b.txt"}
    assert target.read_text(encoding="utf-8") == "line1\nreplaced\nline3"
    assert (tmp_path / "nested/b.txt").read_text(encoding="utf-8") == "hello"


@pytest.mark.asyncio
async def test_patch_engine_rejects_forbidden_paths(tmp_path: Path) -> None:
    engine = PatchEngine()
    patch = PatchDocument.model_validate(
        {
            "patch_ops": [
                {
                    "op": "create_file",
                    "file": ".env",
                    "content": "SECRET=1",
                    "reason": "bad",
                }
            ]
        }
    )

    with pytest.raises(PatchPolicyViolation):
        await engine.apply_patch_document(tmp_path, patch)
