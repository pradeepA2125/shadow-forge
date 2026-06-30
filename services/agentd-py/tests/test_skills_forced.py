import inspect

from agentd.chat.controller import ChatController


def test_handle_message_accepts_forced_skills() -> None:
    sig = inspect.signature(ChatController.handle_message)
    assert "forced_skills" in sig.parameters


def test_run_loop_accepts_forced_skills() -> None:
    sig = inspect.signature(ChatController._run_loop)
    assert "forced_skills" in sig.parameters
