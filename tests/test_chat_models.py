"""Test chat_models Pydantic validation."""
import pytest
from hub.chat_models import (
    ChatMessageRequest,
    CreateConversationRequest,
    CreateAgentRequest,
    ScoreSubmissionRequest,
)


def test_chat_message_request_minimal():
    req = ChatMessageRequest(conversation_id="conv-123", content="hello")
    assert req.conversation_id == "conv-123"
    assert req.content == "hello"
    assert req.files == []


def test_chat_message_request_with_files():
    req = ChatMessageRequest(
        conversation_id="conv-123",
        content="check this",
        files=[{"path": "main.py", "content_base64": "cHJpbnQoJ2hpJyk=", "size": 14}],
    )
    assert len(req.files) == 1
    assert req.files[0]["path"] == "main.py"


def test_create_conversation_request():
    req = CreateConversationRequest(agent_id="claude-coder-01", task_template_id="pm-proposal")
    assert req.agent_id == "claude-coder-01"
    assert req.task_template_id == "pm-proposal"


def test_create_agent_request():
    req = CreateAgentRequest(
        name="my-agent",
        role="coder",
        cli="opencode",
        capabilities=["python"],
        max_concurrent_tasks=2,
        timeout_minutes=45,
    )
    assert req.name == "my-agent"
    assert req.cli == "opencode"
    assert req.timeout_minutes == 45


def test_score_submission_request():
    req = ScoreSubmissionRequest(score=85, feedback="good work")
    assert req.score == 85
    assert req.feedback == "good work"


def test_score_out_of_range():
    with pytest.raises(Exception):
        ScoreSubmissionRequest(score=101)
