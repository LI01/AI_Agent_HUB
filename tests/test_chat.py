"""Integration tests for chat training platform."""
import tempfile

import pytest
from fastapi.testclient import TestClient

from hub.main import app


@pytest.fixture
def client():
    return TestClient(app)


class TestChatDb:
    """Test chat_db operations directly."""

    def test_upsert_user_creates_and_updates(self):
        from hub import chat_db

        user = chat_db.upsert_user("test@example.com", "Test User")
        assert user["email"] == "test@example.com"
        assert user["name"] == "Test User"

        user2 = chat_db.upsert_user("test@example.com", "Updated Name")
        assert user2["id"] == user["id"]
        assert user2["name"] == "Updated Name"

    def test_conversation_crud(self):
        from hub import chat_db

        user = chat_db.upsert_user("conv-test@example.com")
        conv = chat_db.create_conversation(user["id"], "agent-1", "Test Conv")
        assert conv["title"] == "Test Conv"
        assert conv["status"] == "active"

        conv2 = chat_db.get_conversation(conv["id"])
        assert conv2["title"] == "Test Conv"

        convs = chat_db.list_conversations(user["id"])
        assert len(convs) >= 1

    def test_message_crud(self):
        from hub import chat_db

        user = chat_db.upsert_user("msg-test@example.com")
        conv = chat_db.create_conversation(user["id"], "agent-1")
        msg = chat_db.create_message(conv["id"], "user", "hello")
        assert msg["content"] == "hello"
        assert msg["role"] == "user"

        msgs = chat_db.list_messages(conv["id"])
        assert len(msgs) >= 1

    def test_submission_flow(self):
        from hub import chat_db

        user = chat_db.upsert_user("sub-test@example.com")
        conv = chat_db.create_conversation(user["id"], "agent-1")
        sub = chat_db.create_submission(conv["id"], user["id"])
        assert sub["status"] == "submitted"

        chat_db.score_submission(sub["id"], 85, "good work")
        sub2 = chat_db.list_submissions(status="reviewed")
        assert any(s["id"] == sub["id"] for s in sub2)


class TestChatFiles:
    """Test workspace file management."""

    def test_sanitize_path_blocks_traversal(self):
        from hub.chat_files import sanitize_path

        with pytest.raises(ValueError):
            sanitize_path("../etc/passwd")

    def test_workspace_isolation(self):
        from hub.chat_files import WorkspaceManager

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = WorkspaceManager(tmpdir)
            p1 = mgr.get_user_path("user-1")
            p2 = mgr.get_user_path("user-2")
            assert p1 != p2
            assert "user-1" in p1
            assert "user-2" in p2


class TestChatEndpoints:
    """Test chat REST endpoints."""

    def test_templates_endpoint_requires_auth(self, client):
        resp = client.get("/chat/templates")
        assert resp.status_code == 403

    def test_conversations_requires_auth(self, client):
        resp = client.get("/chat/conversations")
        assert resp.status_code == 403

    def test_available_agents_requires_auth(self, client):
        resp = client.get("/chat/agents/available")
        assert resp.status_code == 403
