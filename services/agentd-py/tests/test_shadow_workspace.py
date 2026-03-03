from __future__ import annotations

from pathlib import Path

import pytest

from agentd.domain.models import TaskRecord, TaskStatus
from agentd.workspace.shadow import ShadowWorkspaceManager


@pytest.mark.asyncio
async def test_shadow_workspace_prepare_promote_and_cleanup(tmp_path: Path) -> None:
    real_workspace = tmp_path / "real"
    real_workspace.mkdir(parents=True)
    (real_workspace / "src").mkdir()
    (real_workspace / "src/main.py").write_text("print('real')\n", encoding="utf-8")

    manager = ShadowWorkspaceManager(root_path=tmp_path / "shadows")
    shadow = await manager.prepare("task-1", str(real_workspace))

    shadow_file = shadow.shadow_path / "src/main.py"
    assert shadow_file.exists()

    shadow_file.write_text("print('shadow')\n", encoding="utf-8")

    task = TaskRecord(
        task_id="task-1",
        goal="goal",
        workspace_path=str(real_workspace),
        shadow_workspace_path=str(shadow.shadow_path),
        status=TaskStatus.SUCCEEDED,
        modified_files=["src/main.py"],
    )

    await manager.promote(task)
    assert (real_workspace / "src/main.py").read_text(encoding="utf-8") == "print('shadow')\n"

    await manager.cleanup(task)
    assert not shadow.shadow_path.exists()
