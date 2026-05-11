import pytest
from pathlib import Path
from agentd.chat.models import ChatMessage, ChatThread
from agentd.chat.storage import ChatThreadStore

@pytest.fixture
def store(tmp_path: Path) -> ChatThreadStore:
    return ChatThreadStore(tmp_path / "chat.db")

def test_create_thread_returns_empty_thread(store: ChatThreadStore) -> None:
    thread = store.create_thread("/ws/project")
    assert thread.workspace_path == "/ws/project"
    assert thread.messages == []
    assert thread.title == "New Chat"

def test_multiple_threads_per_workspace(store: ChatThreadStore) -> None:
    t1 = store.create_thread("/ws/project", title="First chat")
    t2 = store.create_thread("/ws/project", title="Second chat")
    assert t1.thread_id != t2.thread_id
    threads = store.list_threads("/ws/project")
    assert len(threads) == 2

def test_list_threads_returns_newest_first(store: ChatThreadStore) -> None:
    store.create_thread("/ws/project", title="Old")
    store.create_thread("/ws/project", title="New")
    threads = store.list_threads("/ws/project")
    assert threads[0].title == "New"

def test_list_threads_isolates_by_workspace(store: ChatThreadStore) -> None:
    store.create_thread("/ws/alpha")
    store.create_thread("/ws/beta")
    assert len(store.list_threads("/ws/alpha")) == 1
    assert len(store.list_threads("/ws/beta")) == 1

def test_append_message_persists(store: ChatThreadStore) -> None:
    thread = store.create_thread("/ws/project")
    msg = ChatMessage(role="user", content="hello")
    store.append_message(thread.thread_id, msg)

    reloaded = store.get_thread(thread.thread_id)
    assert len(reloaded.messages) == 1
    assert reloaded.messages[0].content == "hello"

def test_update_touched_files(store: ChatThreadStore) -> None:
    thread = store.create_thread("/ws/project")
    store.add_touched_file(thread.thread_id, "src/foo.py")
    store.add_touched_file(thread.thread_id, "src/bar.py")

    reloaded = store.get_thread(thread.thread_id)
    assert "src/foo.py" in reloaded.touched_files
    assert "src/bar.py" in reloaded.touched_files

def test_update_title(store: ChatThreadStore) -> None:
    thread = store.create_thread("/ws/project")
    store.update_title(thread.thread_id, "Add auth layer")
    reloaded = store.get_thread(thread.thread_id)
    assert reloaded.title == "Add auth layer"
