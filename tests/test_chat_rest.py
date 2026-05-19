"""Test chat REST API endpoints."""
import pytest
from fastapi.testclient import TestClient
from hub.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_get_chat_templates(client):
    """GET /chat/templates returns template list."""
    resp = client.get("/chat/templates")
    # Should return 200 (empty list if no templates seeded, or populated list)
    assert resp.status_code == 200


def test_create_conversation_requires_auth(client):
    """POST /chat/conversations requires CF Access JWT."""
    resp = client.post("/chat/conversations", json={"agent_id": "test"})
    # Without auth, should return 403
    assert resp.status_code == 403


def test_list_conversations_requires_auth(client):
    """GET /chat/conversations requires CF Access JWT."""
    resp = client.get("/chat/conversations")
    assert resp.status_code == 403


def test_get_available_agents_requires_auth(client):
    """GET /chat/agents/available requires auth."""
    resp = client.get("/chat/agents/available")
    assert resp.status_code == 403
