"""Test chat file management."""
import os
import tempfile
import pytest
from hub.chat_files import (
    WorkspaceManager,
    sanitize_path,
)


@pytest.fixture
def workspace_root():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def manager(workspace_root):
    return WorkspaceManager(workspace_root)


def test_sanitize_path_rejects_traversal():
    with pytest.raises(ValueError):
        sanitize_path("../etc/passwd")


def test_sanitize_path_rejects_absolute():
    with pytest.raises(ValueError):
        sanitize_path("/etc/passwd")


def test_sanitize_path_accepts_relative():
    assert sanitize_path("src/main.py") == "src/main.py"


def test_get_user_workspace(manager):
    path = manager.get_user_path("user-123")
    assert "user-123" in path
    assert os.path.isdir(path)


def test_copy_template_files(manager):
    # Create a template source
    template_dir = os.path.join(manager.template_root, "pm-proposal")
    os.makedirs(template_dir, exist_ok=True)
    with open(os.path.join(template_dir, "template.md"), "w") as f:
        f.write("# Template")

    user_path = manager.get_user_path("user-123")
    manager.copy_template_files("pm-proposal", user_path)

    assert os.path.exists(os.path.join(user_path, "pm-proposal", "template.md"))


def test_list_directory(manager):
    user_path = manager.get_user_path("user-123")
    os.makedirs(os.path.join(user_path, "src"))
    with open(os.path.join(user_path, "src", "main.py"), "w") as f:
        f.write("print('hi')")

    entries = manager.list_directory(user_path, "src/")
    assert len(entries) == 1
    assert entries[0]["name"] == "main.py"


def test_write_file(manager):
    user_path = manager.get_user_path("user-123")
    manager.write_file(user_path, "test.txt", b"hello")
    content = manager.read_file(user_path, "test.txt")
    assert content == b"hello"


def test_delete_file(manager):
    user_path = manager.get_user_path("user-123")
    manager.write_file(user_path, "to_delete.txt", b"bye")
    manager.delete_file(user_path, "to_delete.txt")
    assert not os.path.exists(os.path.join(user_path, "to_delete.txt"))


def test_resolve_path_blocks_prefix_collision():
    """user-123 must not match user-123-evil via startswith."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = WorkspaceManager(tmpdir)
        user_path = mgr.get_user_path("user")
        evil_path = mgr.get_user_path("user-evil")
        # Even though "user-evil" starts with "user", resolve_path must block it
        with pytest.raises(ValueError):
            mgr.resolve_path(user_path, "../user-evil/file.txt")


def test_resolve_path_blocks_mixed_traversal():
    """Mixed traversal like foo/../../etc must be blocked."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = WorkspaceManager(tmpdir)
        user_path = mgr.get_user_path("user-123")
        with pytest.raises(ValueError):
            mgr.resolve_path(user_path, "foo/../../etc/passwd")


def test_sanitize_path_rejects_empty():
    with pytest.raises(ValueError):
        sanitize_path("")
