"""Test chat WebSocket endpoint."""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from hub.main import app


def test_chat_ws_requires_auth():
    """WS /chat/ws rejects unauthenticated connections with close code 4003."""
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/chat/ws"):
            pass
    assert exc_info.value.code == 4003


def test_chat_ws_rejects_invalid_token():
    """WS /chat/ws rejects invalid tokens with close code 4003."""
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/chat/ws?token=invalid-token"):
            pass
    assert exc_info.value.code == 4003
