from agentd.chat.controller_prompts import format_controller_system_prompt


def test_task_modes_present_when_enabled():
    p = format_controller_system_prompt([], task_subsystem_enabled=True)
    assert "create_task" in p


def test_task_modes_absent_when_disabled():
    p = format_controller_system_prompt([], task_subsystem_enabled=False)
    assert "create_task" not in p
    assert "resume" not in p
    # edit + explain remain the offered modes
    assert "edit" in p and "explain" in p


def test_edit_not_framed_as_small_only():
    # The reframe: edit is the primary path for any-size change (small AND large),
    # present regardless of the flag.
    for enabled in (True, False):
        p = format_controller_system_prompt([], task_subsystem_enabled=enabled).lower()
        assert "any size" in p or "small and large" in p or "small or large" in p
