from agentd.chat.controller_prompts import format_controller_system_prompt

TOOLS: list[dict[str, object]] = []


def test_no_instructions_no_block() -> None:
    out = format_controller_system_prompt(
        TOOLS, task_subsystem_enabled=False, memory_enabled=False
    )
    assert "PROJECT INSTRUCTIONS" not in out


def test_instructions_appended_when_present() -> None:
    out = format_controller_system_prompt(
        TOOLS,
        task_subsystem_enabled=False,
        memory_enabled=False,
        project_instructions="Always use tabs, never spaces.",
    )
    assert "PROJECT INSTRUCTIONS" in out
    assert "Always use tabs, never spaces." in out
    # Appended at the end (after any memory block), mirroring _MEMORY_BLOCK.
    assert out.rstrip().endswith("Always use tabs, never spaces.")


def test_blank_instructions_no_block() -> None:
    out = format_controller_system_prompt(
        TOOLS,
        task_subsystem_enabled=False,
        memory_enabled=False,
        project_instructions="   \n  ",
    )
    assert "PROJECT INSTRUCTIONS" not in out


def test_instructions_value_with_braces_does_not_crash() -> None:
    # AGENTS.md may contain literal { } — must not be treated as format fields.
    out = format_controller_system_prompt(
        TOOLS,
        task_subsystem_enabled=False,
        memory_enabled=False,
        project_instructions="Use {curly} braces in JSON examples.",
    )
    assert "Use {curly} braces in JSON examples." in out
