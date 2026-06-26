import logging

from agentd.chat.controller_factory import (
    is_task_subsystem_enabled,
    warn_if_incoherent_flags,
)


def test_defaults_off(monkeypatch):
    monkeypatch.delenv("AI_EDITOR_TASK_SUBSYSTEM", raising=False)
    assert is_task_subsystem_enabled() is False


def test_truthy_values_enable(monkeypatch):
    for v in ("1", "true", "YES", "on"):
        monkeypatch.setenv("AI_EDITOR_TASK_SUBSYSTEM", v)
        assert is_task_subsystem_enabled() is True


def test_other_values_off(monkeypatch):
    monkeypatch.setenv("AI_EDITOR_TASK_SUBSYSTEM", "0")
    assert is_task_subsystem_enabled() is False


def test_warns_when_task_off_and_controller_off(monkeypatch, caplog):
    monkeypatch.setenv("AI_EDITOR_TASK_SUBSYSTEM", "0")
    monkeypatch.setenv("AI_EDITOR_CHAT_CONTROLLER", "0")
    with caplog.at_level(logging.WARNING):
        warn_if_incoherent_flags(logging.getLogger("test"))
    assert any("incoherent" in r.message.lower() for r in caplog.records)


def test_no_warn_when_task_off_but_controller_on(monkeypatch, caplog):
    monkeypatch.setenv("AI_EDITOR_TASK_SUBSYSTEM", "0")
    monkeypatch.setenv("AI_EDITOR_CHAT_CONTROLLER", "1")
    with caplog.at_level(logging.WARNING):
        warn_if_incoherent_flags(logging.getLogger("test"))
    assert not caplog.records
