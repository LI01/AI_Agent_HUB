"""Pydantic models for chat training platform API."""
from typing import Optional
from pydantic import BaseModel, Field


class ChatMessageRequest(BaseModel):
    conversation_id: str
    content: str
    files: list[dict] = []


class CreateConversationRequest(BaseModel):
    agent_id: str
    task_template_id: Optional[str] = None
    title: Optional[str] = None


class CreateAgentRequest(BaseModel):
    name: str
    role: str
    cli: str
    capabilities: list[str] = []
    max_concurrent_tasks: int = 1
    timeout_minutes: int = 30


class ScoreSubmissionRequest(BaseModel):
    score: int = Field(ge=0, le=100)
    feedback: Optional[str] = None


class UploadFileRequest(BaseModel):
    path: str
    content_base64: str
    size: int
