from pathlib import Path

import pytest

from agentd.chat.edit_session import TurnEditSession
from agentd.patch.engine import PatchEngine
from agentd.workspace.shadow import ShadowWorkspaceManager


def _sr(file: str, search: str, replace: str) -> list[dict]:
    return [
        {"op": "search_replace", "file": file, "search": search, "replace": replace, "reason": "r"}
    ]


@pytest.mark.asyncio
async def test_accept_promotes_to_real_and_reject_restores(tmp_path: Path):
    real = tmp_path / "ws"
    real.mkdir()
    (real / "f.py").write_text("x = 1\n")
    sess = TurnEditSession(
        turn_id="t1",
        real_path=real,
        workspace_manager=ShadowWorkspaceManager(tmp_path / "shadows"),
        patch_engine=PatchEngine(),
    )
    diff = await sess.apply(_sr("f.py", "x = 1", "x = 2"))
    assert any(e.path == "f.py" for e in diff)
    await sess.accept()
    assert (real / "f.py").read_text() == "x = 2\n"  # instant-promoted to real
    # reject leaves real untouched (patch applied to shadow only, not yet promoted)
    await sess.apply(_sr("f.py", "x = 2", "x = 999"))
    await sess.reject()
    assert (real / "f.py").read_text() == "x = 2\n"
    await sess.close()


@pytest.mark.asyncio
async def test_reject_then_edit_different_file_keeps_invariant(tmp_path: Path):
    real = tmp_path / "ws"
    real.mkdir()
    (real / "a.py").write_text("a = 1\n")
    (real / "b.py").write_text("b = 1\n")
    sess = TurnEditSession(
        turn_id="t2",
        real_path=real,
        workspace_manager=ShadowWorkspaceManager(tmp_path / "sh"),
        patch_engine=PatchEngine(),
    )
    await sess.apply(_sr("a.py", "a = 1", "a = 9"))
    await sess.reject()  # a.py rejected
    await sess.apply(_sr("b.py", "b = 1", "b = 2"))
    await sess.accept()  # b.py accepted
    assert (real / "a.py").read_text() == "a = 1\n"  # untouched
    assert (real / "b.py").read_text() == "b = 2\n"  # promoted
    await sess.close()
