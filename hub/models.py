from pydantic import BaseModel, Field
from typing import Optional, Union
from datetime import datetime
from enum import Enum


class Capability(BaseModel):
    """Versioned, schema-bearing capability advertisement (Phase 2.2 F3)."""
    name: str = Field(..., min_length=1)
    version: int = Field(default=1, ge=1)
    payload_schema: Optional[dict] = None
    result_schema: Optional[dict] = None

    model_config = {"extra": "ignore"}


class AgentStatus(str, Enum):
    IDLE = "idle"
    BUSY = "busy"
    OFFLINE = "offline"


class TaskStatus(str, Enum):
    QUEUED = "queued"
    ASSIGNED = "assigned"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"


class MachineInfo(BaseModel):
    hostname: Optional[str] = None
    ip: Optional[str] = None
    platform: str = "unknown"  # docker, pc, cloud


class Agent(BaseModel):
    agent_id: str
    name: Optional[str] = None
    description: Optional[str] = None
    owner: Optional[str] = None
    capabilities: list[Union[str, Capability, dict]] = Field(default_factory=list)
    status: AgentStatus = AgentStatus.OFFLINE
    last_heartbeat: datetime = Field(default_factory=datetime.now)
    created_at: datetime = Field(default_factory=datetime.now)
    machine_info: MachineInfo = Field(default_factory=MachineInfo)


class Task(BaseModel):
    task_id: str
    task: str = ""
    submitted_by: str
    target_agent: Optional[str] = None
    assigned_agent_id: Optional[str] = None
    task_type: str
    payload: dict = Field(default_factory=dict)
    status: TaskStatus = TaskStatus.QUEUED
    created_at: datetime = Field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result: Optional[dict] = None
    logs: list[str] = Field(default_factory=list)
    priority: int = 0
    timeout: int = 300
    parent_task_id: Optional[str] = None
    min_version: int = 1

    class Config:
        use_enum_values = True


class TaskSubmitRequest(BaseModel):
    task: str = ""  # Natural language or structured
    target_agent: Optional[str] = None
    timeout: int = 300
    task_type: Optional[str] = None  # For structured tasks
    payload: Optional[dict] = None  # For structured tasks
    priority: int = 0
    parent_task_id: Optional[str] = None
    min_version: int = 1


class TaskSubmitResponse(BaseModel):
    task_id: str
    status: TaskStatus


class SubmitChildRequest(BaseModel):
    """Pydantic shape-validator for the `submit_child` WS message (per design §2.2 step 4).

    `parent_task_id` is required and authoritative: the SDK passes the id of
    the task the calling agent is currently executing. The hub validates that
    the parent is owned by the caller and is still in an active state.
    """
    request_id: str
    parent_task_id: str = Field(..., min_length=1)
    target_agent: str = Field(..., min_length=1)
    task: str = ""
    task_type: Optional[str] = None
    payload: dict = Field(default_factory=dict)
    priority: int = 0
    timeout: int = 300
    min_version: int = 1


class RegisterRequest(BaseModel):
    agent_id: str
    capabilities: list[str] = Field(default_factory=list)
    name: Optional[str] = None
    description: Optional[str] = None
    owner: Optional[str] = None
    machine_info: Optional[MachineInfo] = None


class HeartbeatRequest(BaseModel):
    agent_id: str
    status: AgentStatus


class ReportRequest(BaseModel):
    task_id: str
    agent_id: Optional[str] = None
    status: TaskStatus
    result: Optional[dict] = None
    logs: list[str] = Field(default_factory=list)


class Event(BaseModel):
    type: str
    data: dict
