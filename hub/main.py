import asyncio
import json
import os
import contextlib
import pathlib
import uuid
from datetime import datetime
from typing import Dict, Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse as EventStreamResponse

from . import database as db
from . import delegation
from .auth import Permission, access_control
from .capabilities import (
    _agent_can_handle,
    _resolve_cap_version,
    _resolve_payload_schema,
    aggregate_capabilities,
    current_mode,
    normalize_capabilities,
    validate_payload,
)
from .models import (
    Agent,
    AgentStatus,
    Event,
    HeartbeatRequest,
    MachineInfo,
    RegisterRequest,
    ReportRequest,
    SubmitChildRequest,
    Task,
    TaskStatus,
    TaskSubmitRequest,
    TaskSubmitResponse,
)
from .queue import TaskQueue
from .registry import AgentRegistry
from .router import Router
from .mcp_protocol import InProcessHubBackend, handle_jsonrpc
from . import cf_access as _cf_access_mod
from pydantic import ValidationError
from .chat_routes import chat_router, admin_chat_router
from .chat_ws import chat_websocket_endpoint


app = FastAPI(title="Agent Hub")


def _cors_origins() -> list[str]:
    configured = os.getenv("AGENT_HUB_CORS_ORIGINS")
    if configured:
        return [origin.strip() for origin in configured.split(",") if origin.strip()]
    return [
        "http://localhost",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
    ]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat_router)
app.include_router(admin_chat_router)

registry = AgentRegistry()
queue = TaskQueue()
router = Router(registry, queue)
event_subscribers: list[asyncio.Queue] = []
timeout_watcher_task: Optional[asyncio.Task] = None
TERMINAL_STATUSES = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.TIMEOUT}

# Optional alternate auth path for read endpoints: a verified Cloudflare
# Access JWT in Cf-Access-Jwt-Assertion. Configured via AGENT_HUB_CF_ACCESS_*.
# None when not configured -> JWT path is disabled entirely.
cf_access_verifier = _cf_access_mod.from_env()

# Wire delegation maps to the queue's lock so map mutations share the queue
# lock (per design §2.4 — no new lock).
delegation.set_lock(queue._lock)


def get_auth_key(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    if authorization.startswith("Bearer "):
        return authorization[7:]
    return authorization


def require_register(authorization: Optional[str]) -> str:
    auth_key = get_auth_key(authorization)
    if not access_control.can_agent_register(auth_key):
        raise HTTPException(status_code=403, detail="Registration not permitted")
    return auth_key


def require_assign(authorization: Optional[str], target_agent: str = "") -> str:
    auth_key = get_auth_key(authorization)
    if not access_control.can_assign_task(auth_key, target_agent):
        raise HTTPException(status_code=403, detail="Not permitted to assign tasks")
    return auth_key


def _cf_access_authed(jwt_header: Optional[str]) -> bool:
    """Return True if jwt_header is a valid CF Access JWT under the configured
    verifier. Always False when the verifier isn't configured."""
    if not jwt_header or cf_access_verifier is None:
        return False
    return cf_access_verifier.verify(jwt_header) is not None


def require_view_agents(
    authorization: Optional[str],
    cf_access_jwt: Optional[str] = None,
) -> str:
    auth_key = get_auth_key(authorization)
    if access_control.can_view_agents(auth_key):
        return auth_key
    if _cf_access_authed(cf_access_jwt):
        return ""
    raise HTTPException(status_code=403, detail="Agent view permission required")


def require_view_tasks(
    authorization: Optional[str],
    cf_access_jwt: Optional[str] = None,
) -> str:
    auth_key = get_auth_key(authorization)
    if access_control.can_view_tasks(auth_key):
        return auth_key
    if _cf_access_authed(cf_access_jwt):
        return ""
    raise HTTPException(status_code=403, detail="Task view permission required")


def require_admin(authorization: Optional[str]) -> Permission:
    auth_key = get_auth_key(authorization)
    perm = access_control.validate_key(auth_key)
    if not perm or not perm.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return perm


def require_jwt_email(cf_access_jwt: Optional[str]) -> str:
    """Verify the CF Access JWT and return the lowercased email.

    Raises 403 if the JWT is absent, the verifier isn't configured, the JWT
    is invalid, or the claims lack an email. Per design v3 §3.
    """
    if not cf_access_jwt or cf_access_verifier is None:
        raise HTTPException(status_code=403, detail="CF Access JWT required")
    claims = cf_access_verifier.verify(cf_access_jwt)
    if not claims:
        raise HTTPException(status_code=403, detail="CF Access JWT invalid")
    email = (claims.get("email") or "").lower().strip()
    if not email:
        raise HTTPException(status_code=403, detail="JWT missing email")
    return email


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_json(value, default):
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _status_value(status) -> str:
    return status.value if hasattr(status, "value") else str(status)


def _agent_from_row(row: dict) -> Agent:
    status = row.get("status") or "offline"
    if status == "online":
        status = "idle"
    try:
        agent_status = AgentStatus(status)
    except ValueError:
        agent_status = AgentStatus.OFFLINE
    raw_caps = _parse_json(row.get("skills"), [])
    return Agent(
        agent_id=row["id"],
        name=row.get("name") or row["id"],
        description=row.get("description"),
        owner=row.get("owner"),
        capabilities=normalize_capabilities(raw_caps),
        status=agent_status,
        machine_info=MachineInfo(**_parse_json(row.get("position"), {})),
        created_at=_parse_datetime(row.get("created_at")) or datetime.now(),
        last_heartbeat=_parse_datetime(row.get("last_seen_at")) or datetime.now(),
    )


def _task_from_row(row: dict) -> Task:
    parsed = db.parse_task_row(row)
    status = parsed["status"]
    started_at = _parse_datetime(parsed.get("started_at"))
    if status in ("assigned", "running"):
        status = "queued"
        started_at = None
    return Task(
        task_id=parsed["task_id"],
        task=parsed.get("task") or "",
        submitted_by=parsed["submitted_by"],
        target_agent=parsed.get("target_agent"),
        assigned_agent_id=None if status == "queued" else parsed.get("assigned_agent_id"),
        task_type=parsed["task_type"],
        payload=parsed["payload"],
        status=TaskStatus(status),
        created_at=_parse_datetime(parsed.get("created_at")) or datetime.now(),
        started_at=started_at,
        completed_at=_parse_datetime(parsed.get("completed_at")),
        result=parsed.get("result"),
        logs=parsed["logs"],
        priority=parsed["priority"],
        timeout=parsed["timeout"],
        min_version=parsed.get("min_version") or 1,
        parent_task_id=parsed.get("parent_task_id"),
    )


def load_state():
    agents = [_agent_from_row(row) for row in db.list_agents()]
    for agent in agents:
        agent.status = AgentStatus.OFFLINE
        db.update_agent_status(agent.agent_id, AgentStatus.OFFLINE.value)
    registry.load(agents)

    for row in db.list_tasks():
        if row.get("status") in ("assigned", "running") and row.get("assigned_agent_id"):
            db.requeue_tasks_for_agent(row["assigned_agent_id"])

    restored_tasks = []
    for row in db.list_tasks():
        task = _task_from_row(row)
        restored_tasks.append(task)
    queue.load(restored_tasks)

    # Rebuild parent/child maps for non-terminal restored tasks (per design §2.3).
    delegation.rebuild_from_db(
        (t.task_id, t.parent_task_id)
        for t in restored_tasks
        if t.status not in TERMINAL_STATUSES
    )
    print(f"[hub] Restored {len(agents)} agents and {len(restored_tasks)} tasks from SQLite")


class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.ws_to_agent: Dict[WebSocket, str] = {}
        # Per design §2.3 / §2.7 — raw API key bound at register time, used
        # by `_handle_submit_child` to revalidate permissions on every call.
        self.session_keys: Dict[WebSocket, str] = {}
        self._lock = asyncio.Lock()

    async def connect(self, agent_id: str, websocket: WebSocket):
        old_websocket = None
        async with self._lock:
            old_websocket = self.active_connections.get(agent_id)
            if old_websocket and old_websocket is not websocket:
                self.ws_to_agent.pop(old_websocket, None)
                self.session_keys.pop(old_websocket, None)
            self.active_connections[agent_id] = websocket
            self.ws_to_agent[websocket] = agent_id
        if old_websocket and old_websocket is not websocket:
            with contextlib.suppress(Exception):
                await old_websocket.close(code=4000, reason="agent reconnected")
        print(f"Agent {agent_id} connected via WebSocket")

    def register_session_key(self, websocket: WebSocket, raw_key: str) -> None:
        """Bind the raw API key used at register-time for later submit_child checks."""
        if raw_key:
            self.session_keys[websocket] = raw_key

    def session_key_for(self, agent_id: str) -> Optional[str]:
        ws = self.active_connections.get(agent_id)
        if ws is None:
            return None
        return self.session_keys.get(ws)

    async def disconnect(self, websocket: WebSocket) -> Optional[str]:
        async with self._lock:
            agent_id = self.ws_to_agent.pop(websocket, None)
            self.session_keys.pop(websocket, None)
            if agent_id and self.active_connections.get(agent_id) is websocket:
                self.active_connections.pop(agent_id, None)
        return agent_id

    @property
    def connections(self) -> Dict[str, WebSocket]:
        """Read-only view used by `_notify_parent_of_child` (design §2.6)."""
        return self.active_connections

    async def send(self, agent_id: str, message: dict) -> bool:
        websocket = self.active_connections.get(agent_id)
        if not websocket:
            return False
        try:
            await websocket.send_json(message)
            return True
        except RuntimeError:
            return False


manager = ConnectionManager()
load_state()


def emit_event(event_type: str, data: dict):
    event = Event(type=event_type, data=data)
    payload = {"type": event.type, "data": event.data, "time": datetime.now().isoformat()}
    for subscriber in list(event_subscribers):
        subscriber.put_nowait(payload)


def task_event_data(task_id: str, **extra) -> dict:
    """Canonical SSE payload builder for task_* events (design §3.3).

    Always includes `task_id` and `parent_task_id` (None for top-level).
    Extra kwargs are merged in.
    """
    task = queue.get(task_id)
    parent_task_id = task.parent_task_id if task is not None else db.get_parent_task_id(task_id)
    return {"task_id": task_id, "parent_task_id": parent_task_id, **extra}


def save_agent(agent: Agent):
    db.save_agent({
        "id": agent.agent_id,
        "name": agent.name or agent.agent_id,
        "description": agent.description or "",
        "skills": agent.capabilities,
        "position": agent.machine_info.model_dump(),
        "owner": agent.owner or "system",
        "status": agent.status.value,
        "created_at": agent.created_at.isoformat(),
        "last_seen_at": agent.last_heartbeat.isoformat(),
    })


def save_task(task: Task):
    db.save_task({
        "id": task.task_id,
        "name": task.task or f"Task {task.task_id[:8]}",
        "description": task.task,
        "status": task.status.value if hasattr(task.status, "value") else task.status,
        "task_type": task.task_type,
        "payload": task.payload,
        "result": task.result,
        "logs": task.logs,
        "target_agent": task.target_agent,
        "assigned_agent_id": task.assigned_agent_id,
        "requested_by": task.submitted_by,
        "priority": task.priority,
        "timeout": task.timeout,
        "min_version": task.min_version,
        "parent_task_id": task.parent_task_id,
        "created_at": task.created_at.isoformat(),
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    })


def task_message(task: Task) -> dict:
    return {
        "task_id": task.task_id,
        "task": task.task,
        "task_type": task.task_type,
        "payload": task.payload,
        "timeout": task.timeout,
        "priority": task.priority,
        "parent_task_id": task.parent_task_id,
    }


async def send_task_to_agent(agent_id: str, task_id: str):
    task = queue.get(task_id)
    if not task:
        return False
    return await manager.send(agent_id, {"type": "task", "task": task_message(task)})


async def route_and_dispatch(task_id: str, min_version: Optional[int] = None) -> Optional[str]:
    task = queue.get(task_id)
    if not task:
        return None

    floor = min_version if min_version is not None else (task.min_version or 1)

    # F3: Direct-target capability + version gate.
    if task.target_agent:
        target_agent = registry.get(task.target_agent)
        if target_agent is None or target_agent.status != AgentStatus.IDLE:
            return None
        if not _agent_can_handle(target_agent, task):
            db.log_activity(
                "task",
                task.task_id,
                "direct_target_capability_mismatch",
                {
                    "target_agent": task.target_agent,
                    "task_type": task.task_type,
                    "min_version": floor,
                },
            )
            return None

    agent_id = router.route(
        task.task_id,
        task.task,
        task.task_type,
        task.payload,
        task.target_agent,
        min_version=floor,
    )
    if not agent_id:
        return None

    registry.heartbeat(agent_id, AgentStatus.BUSY)
    db.update_agent_status(agent_id, AgentStatus.BUSY.value)
    task = queue.get(task_id)
    save_task(task)

    # F4 dispatch-time validation: resolve the selected agent's payload_schema
    # for the chosen (task_type, version) and run the validator.
    selected = registry.get(agent_id)
    if selected is not None:
        chosen_version = _resolve_cap_version(selected, task.task_type, floor) or floor
        schema = _resolve_payload_schema(selected, task.task_type, chosen_version)
        err = validate_payload(task.payload or {}, schema)
        if err is not None:
            mode = current_mode()
            detail = {
                "error": "payload_schema_violation",
                "capability": task.task_type,
                "capability_version": chosen_version,
                "message": err,
            }
            if mode == "strict":
                # Roll back the BUSY transition we just made.
                registry.heartbeat(agent_id, AgentStatus.IDLE)
                db.update_agent_status(agent_id, AgentStatus.IDLE.value)
                # Requeue the task BEFORE finalize so finalize can transition
                # it cleanly. The queue.assign happened in router.route.
                queued_task = queue.get(task_id)
                if queued_task is not None:
                    queued_task.assigned_agent_id = None
                    queued_task.started_at = None
                    queued_task.status = TaskStatus.QUEUED
                    save_task(queued_task)
                db.log_activity(
                    "task", task_id, "payload_schema_violation", detail
                )
                await finalize_task(
                    task_id,
                    TaskStatus.FAILED,
                    result={"error": "payload_schema_violation", "details": detail},
                )
                return None
            else:
                # warn-mode: log + dispatch anyway.
                db.log_activity(
                    "task", task_id, "payload_schema_warn", detail
                )

    emit_event(
        "task_updated",
        task_event_data(task_id, status=TaskStatus.ASSIGNED.value, agent_id=agent_id),
    )
    await send_task_to_agent(agent_id, task_id)
    return agent_id


async def dispatch_pending_for_agent(agent_id: str):
    dispatched = []
    for task in queue.list_pending():
        if task.target_agent and task.target_agent != agent_id:
            continue
        agent = registry.get(agent_id)
        if not agent or agent.status != AgentStatus.IDLE:
            break
        if task.target_agent or _agent_can_handle(agent, task):
            routed = await route_and_dispatch(task.task_id)
            if routed:
                dispatched.append(task.task_id)
    return dispatched


def update_task_state(task_id: str, status: TaskStatus, result: Optional[dict] = None, logs: list[str] = None) -> bool:
    current = queue.get(task_id)
    if not current:
        return False
    if current.status in TERMINAL_STATUSES:
        db.log_activity(
            "task",
            task_id,
            "terminal_update_rejected",
            {"current_status": _status_value(current.status), "requested_status": status.value},
        )
        return False
    if not queue.update_status(task_id, status, result):
        return False
    for log in logs or []:
        queue.add_log(task_id, log)
    db.update_task_status(task_id, status.value, result, logs or [])
    return True


def reporter_owns_task(task_id: str, agent_id: Optional[str]) -> bool:
    if not agent_id:
        return False
    task = queue.get(task_id)
    return bool(task and task.assigned_agent_id == agent_id)


async def _notify_parent_of_child(child_task: Task) -> None:
    """Push `child_completed` to the parent agent. Drop+log on orphan (design §2.6)."""
    parent_id = delegation._parent_of_child.get(child_task.task_id)
    if parent_id is None:
        return
    # Remove the bookkeeping entry now that the child has reached terminal status.
    delegation.release_child(child_task.task_id)

    parent = queue.get(parent_id)
    if parent is None or parent.status in TERMINAL_STATUSES:
        db.log_activity(
            "task",
            child_task.task_id,
            "child_completed_dropped",
            {"parent_task_id": parent_id, "reason": "parent_terminal"},
        )
        return
    pa = parent.assigned_agent_id
    if pa is None or pa not in manager.connections:
        db.log_activity(
            "task",
            child_task.task_id,
            "child_completed_dropped",
            {"parent_task_id": parent_id, "reason": "parent_disconnected"},
        )
        return
    status_value = (
        child_task.status.value if hasattr(child_task.status, "value") else str(child_task.status)
    )
    await manager.send(
        pa,
        {
            "type": "child_completed",
            "parent_task_id": parent_id,
            "child_task_id": child_task.task_id,
            "status": status_value,
            "result": child_task.result,
            "logs": list(child_task.logs or []),
        },
    )


def _write_task_usage(task: Task) -> None:
    """Insert a task_usage row for a finalized task. Per design §2.8 / §3.6.

    Pops the submitter row (so it's idempotent on re-finalize), extracts
    cost/tokens via cost_extract, and INSERT OR IGNOREs into task_usage.
    Raises only when the underlying DB layer raises — the caller wraps in
    try/except so the task is never failed by a usage-write error.
    """
    from . import cost_extract as _cost_extract  # local import: keeps top simple

    cli = _cost_extract.cli_from_agent_id(task.assigned_agent_id)
    cost = _cost_extract.cost_extract(task.result, cli)
    submitter = db.pop_submitter(task.task_id) or {}
    api_key_name = submitter.get("api_key_name")
    owner_email = submitter.get("owner_email")

    started_iso = task.started_at.isoformat() if task.started_at else None
    completed_dt = task.completed_at or datetime.now()
    completed_iso = completed_dt.isoformat()
    duration = None
    if task.started_at and task.completed_at:
        duration = (task.completed_at - task.started_at).total_seconds()

    status_val = (
        task.status.value if hasattr(task.status, "value") else str(task.status)
    )
    row = {
        "task_id": task.task_id,
        "parent_task_id": task.parent_task_id,
        "api_key_name": api_key_name,
        "owner_email": owner_email,
        "agent_id": task.assigned_agent_id,
        "cli": cli,
        "task_type": task.task_type,
        "status": status_val,
        "started_at": started_iso,
        "completed_at": completed_iso,
        "duration_seconds": duration,
        "cost_usd": cost.get("cost_usd"),
        "input_tokens": cost.get("input_tokens"),
        "output_tokens": cost.get("output_tokens"),
        "web_search_requests": cost.get("web_search_requests"),
        "created_at": datetime.now().isoformat(),
    }
    db.save_task_usage(row)


async def finalize_task(
    task_id: str,
    status: TaskStatus,
    result: Optional[dict] = None,
    logs: Optional[list[str]] = None,
) -> bool:
    """Single terminal-state writer (design §2.6).

    Owns:
      - persistence to live state + DB,
      - parent notification when this task has a parent,
      - terminal SSE emit (`task_completed` / `task_updated`),
      - multi-task idle accounting: only flip BUSY→IDLE + dispatch when
        the agent has zero remaining ASSIGNED/RUNNING tasks.
    """
    # Capture the assigned agent BEFORE any state changes so we can do the
    # idle-accounting check after the terminal write.
    current = queue.get(task_id)
    agent_id = current.assigned_agent_id if current is not None else None

    if not update_task_state(task_id, status, result, logs):
        return False

    refreshed = queue.get(task_id)
    if refreshed is None:
        return True

    # Parent notification (only for child tasks).
    if refreshed.parent_task_id is not None:
        await _notify_parent_of_child(refreshed)

    # Terminal SSE.
    status_value = status.value if hasattr(status, "value") else str(status)
    if status == TaskStatus.COMPLETED:
        emit_event(
            "task_completed",
            task_event_data(task_id, status=status_value, result=refreshed.result),
        )
    else:
        emit_event(
            "task_updated",
            task_event_data(task_id, status=status_value, result=refreshed.result),
        )

    # task_usage write — never fails the task on error (design §3.6).
    try:
        _write_task_usage(refreshed)
    except Exception as exc:
        db.log_activity(
            "task", task_id, "task_usage_write_failed", {"error": str(exc)}
        )

    # Multi-task idle accounting (design §2.6).
    if agent_id is not None:
        remaining = [
            t
            for t in queue.list_by_agent(agent_id)
            if t.status in (TaskStatus.ASSIGNED, TaskStatus.RUNNING)
        ]
        if not remaining:
            registry.heartbeat(agent_id, AgentStatus.IDLE)
            db.update_agent_status(agent_id, AgentStatus.IDLE.value)
            await dispatch_pending_for_agent(agent_id)
    return True


async def scan_timeouts_once():
    policy = os.getenv("AGENT_HUB_TIMEOUT_POLICY", "terminal").lower()
    now = datetime.now()
    for task in queue.list_all():
        if task.status not in (TaskStatus.ASSIGNED, TaskStatus.RUNNING) or not task.started_at:
            continue
        if (now - task.started_at).total_seconds() < task.timeout:
            continue
        if policy == "requeue":
            task.status = TaskStatus.QUEUED
            task.assigned_agent_id = None
            task.started_at = None
            save_task(task)
            db.log_activity("task", task.task_id, "timeout_requeued", {"timeout": task.timeout})
            emit_event(
                "task_updated",
                task_event_data(task.task_id, status=TaskStatus.QUEUED.value),
            )
        else:
            await finalize_task(
                task.task_id,
                TaskStatus.TIMEOUT,
                {"error": "Task timed out", "timeout": task.timeout},
            )


async def timeout_watcher():
    interval = int(os.getenv("AGENT_HUB_TIMEOUT_SCAN_INTERVAL", "5"))
    while True:
        await scan_timeouts_once()
        await asyncio.sleep(max(interval, 1))


@app.on_event("startup")
async def start_timeout_watcher():
    global timeout_watcher_task
    if timeout_watcher_task is None or timeout_watcher_task.done():
        timeout_watcher_task = asyncio.create_task(timeout_watcher())


@app.on_event("shutdown")
async def stop_timeout_watcher():
    global timeout_watcher_task
    if timeout_watcher_task:
        timeout_watcher_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await timeout_watcher_task
        timeout_watcher_task = None


def _scan_capability_schema_conflicts(new_agent_id: str) -> None:
    """F7: at registration time, compare the new agent's schema-bearing caps
    against other online agents' caps for the same (name, version). Log one
    activity_log entry per unique conflict pair. Never blocks registration.
    """
    new_agent = registry.get(new_agent_id)
    if new_agent is None or not new_agent.capabilities:
        return
    others = [
        a for a in registry.list_all()
        if a.agent_id != new_agent_id and a.status != AgentStatus.OFFLINE
    ]
    if not others:
        return
    seen_pairs: set[tuple[str, str, int]] = set()
    for new_cap in new_agent.capabilities:
        new_schema = getattr(new_cap, "payload_schema", None)
        if new_schema is None:
            # Skip bare advertisements (rule 1: collapses to null on aggregation).
            continue
        new_name = getattr(new_cap, "name", None)
        new_version = getattr(new_cap, "version", None)
        if new_name is None or new_version is None:
            continue
        new_key = json.dumps(new_schema, sort_keys=True)
        for other in others:
            for other_cap in (other.capabilities or []):
                if (
                    getattr(other_cap, "name", None) != new_name
                    or getattr(other_cap, "version", None) != new_version
                ):
                    continue
                other_schema = getattr(other_cap, "payload_schema", None)
                if other_schema is None:
                    continue
                other_key = json.dumps(other_schema, sort_keys=True)
                if other_key == new_key:
                    continue
                pair_key = (new_name, other.agent_id, new_version)
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                db.log_activity(
                    "agent",
                    new_agent_id,
                    "capability_schema_conflict",
                    {
                        "capability": new_name,
                        "version": new_version,
                        "other_agent_id": other.agent_id,
                        "delta": "byte_diff",
                    },
                )


@app.post("/register")
async def register_agent(req: RegisterRequest, authorization: Optional[str] = Header(None)):
    require_register(authorization)
    capabilities = normalize_capabilities(req.capabilities)
    agent = registry.register(
        req.agent_id,
        capabilities,
        req.machine_info,
        name=req.name,
        description=req.description,
        owner=req.owner,
    )
    save_agent(agent)
    _scan_capability_schema_conflicts(agent.agent_id)
    db.log_activity("agent", agent.agent_id, "registered", {"capabilities": req.capabilities})
    emit_event("agent_registered", {"agent_id": agent.agent_id, "capabilities": req.capabilities})
    dispatched = await dispatch_pending_for_agent(agent.agent_id)
    return {"status": "registered", "agent_id": agent.agent_id, "dispatched": dispatched}


@app.post("/heartbeat")
async def heartbeat(req: HeartbeatRequest, authorization: Optional[str] = Header(None)):
    require_register(authorization)
    if not registry.heartbeat(req.agent_id, req.status):
        raise HTTPException(status_code=404, detail="Agent not found")
    db.update_agent_status(req.agent_id, req.status.value)
    emit_event("agent_status", {"agent_id": req.agent_id, "status": req.status.value})
    dispatched = []
    if req.status == AgentStatus.IDLE:
        dispatched = await dispatch_pending_for_agent(req.agent_id)
    return {"status": "ok", "dispatched": dispatched}


@app.post("/unregister")
def unregister_agent(agent_id: str, authorization: Optional[str] = Header(None)):
    require_register(authorization)
    if not registry.unregister(agent_id):
        raise HTTPException(status_code=404, detail="Agent not found")
    db.delete_agent(agent_id)
    queue.requeue_assigned_to(agent_id)
    db.requeue_tasks_for_agent(agent_id)
    emit_event("agent_unregistered", {"agent_id": agent_id})
    return {"status": "unregistered"}


@app.post("/report")
async def report_task(req: ReportRequest, authorization: Optional[str] = Header(None)):
    require_register(authorization)
    task = queue.get(req.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if not reporter_owns_task(req.task_id, req.agent_id):
        raise HTTPException(status_code=403, detail="Reporter is not assigned to this task")

    if req.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.TIMEOUT):
        if not await finalize_task(req.task_id, req.status, req.result, req.logs):
            raise HTTPException(status_code=409, detail="Task is already terminal")
        return {"status": "reported"}

    if not update_task_state(req.task_id, req.status, req.result, req.logs):
        raise HTTPException(status_code=409, detail="Task is already terminal")
    if req.agent_id and req.status == TaskStatus.RUNNING:
        registry.heartbeat(req.agent_id, AgentStatus.BUSY)
        db.update_agent_status(req.agent_id, AgentStatus.BUSY.value)
    emit_event(
        "task_updated",
        task_event_data(req.task_id, status=req.status.value, result=req.result),
    )
    return {"status": "reported"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    agent_id = None
    await websocket.accept()
    try:
        first_msg = await websocket.receive_json()
        if first_msg.get("type") != "register":
            await websocket.send_json({"type": "error", "message": "First message must be register"})
            await websocket.close()
            return

        agent_id = first_msg.get("agent_id")
        capabilities = first_msg.get("capabilities", [])
        auth_token = first_msg.get("auth_token")
        perm = access_control.validate_key(auth_token)
        if not perm or not (perm.is_admin or perm.can_register):
            await websocket.send_json({"type": "error", "message": "Registration not permitted"})
            await websocket.close()
            return
        if not agent_id:
            await websocket.send_json({"type": "error", "message": "agent_id required"})
            await websocket.close()
            return

        machine_info = first_msg.get("machine_info") or {}
        normalized_caps = normalize_capabilities(capabilities)
        agent = registry.register(
            agent_id,
            normalized_caps,
            MachineInfo(**machine_info),
            name=first_msg.get("name"),
            description=first_msg.get("description"),
            owner=first_msg.get("owner"),
        )
        save_agent(agent)
        _scan_capability_schema_conflicts(agent_id)
        await manager.connect(agent_id, websocket)
        manager.register_session_key(websocket, auth_token)
        await websocket.send_json({"type": "registered", "agent_id": agent_id})
        emit_event("agent_registered", {"agent_id": agent_id, "capabilities": capabilities})
        await dispatch_pending_for_agent(agent_id)

        while True:
            msg = await websocket.receive_json()
            await handle_agent_message(agent_id, msg)

    except WebSocketDisconnect:
        pass
    finally:
        if agent_id:
            disconnected = await manager.disconnect(websocket)
            if disconnected:
                registry.heartbeat(disconnected, AgentStatus.OFFLINE)
                db.update_agent_status(disconnected, AgentStatus.OFFLINE.value)
                requeued = queue.requeue_assigned_to(disconnected)
                db.requeue_tasks_for_agent(disconnected)
                db.log_activity("agent", disconnected, "disconnected", {"requeued": [t.task_id for t in requeued]})
                emit_event("agent_status", {"agent_id": disconnected, "status": AgentStatus.OFFLINE.value})


@app.websocket("/chat/ws")
async def chat_ws(websocket: WebSocket, token: str = Query(None)):
    await chat_websocket_endpoint(
        websocket,
        token,
        cf_access_verifier=cf_access_verifier,
        admin_emails=_get_admin_emails_for_chat(),
    )


def _get_admin_emails_for_chat() -> list[str]:
    raw = os.getenv("AGENT_HUB_ADMIN_EMAILS", "")
    return [e.strip().lower() for e in raw.split(",") if e.strip()]


async def handle_agent_message(agent_id: str, msg: dict):
    msg_type = msg.get("type")

    if msg_type == "heartbeat":
        status = AgentStatus(msg.get("status", "idle"))
        registry.heartbeat(agent_id, status)
        db.update_agent_status(agent_id, status.value)
        emit_event("agent_status", {"agent_id": agent_id, "status": status.value})
        if status == AgentStatus.IDLE:
            await dispatch_pending_for_agent(agent_id)

    elif msg_type == "status":
        task_id = msg.get("task_id")
        status = TaskStatus(msg.get("status", "running"))
        if task_id and reporter_owns_task(task_id, agent_id):
            if not update_task_state(task_id, status):
                return
            emit_event(
                "task_updated",
                task_event_data(task_id, status=status.value),
            )

    elif msg_type == "result":
        task_id = msg.get("task_id")
        status = TaskStatus(msg.get("status", "completed"))
        result = msg.get("result", {})
        logs = msg.get("logs", [])
        if task_id and reporter_owns_task(task_id, agent_id):
            await finalize_task(task_id, status, result, logs)

    elif msg_type == "log":
        task_id = msg.get("task_id")
        log = msg.get("log", "")
        if task_id and log and reporter_owns_task(task_id, agent_id):
            queue.add_log(task_id, log)
            db.add_task_log(task_id, log)
            emit_event(
                "task_updated",
                task_event_data(task_id, log=log),
            )

    elif msg_type == "activity_log":
        task_id = msg.get("task_id")
        action = msg.get("action")
        details = msg.get("details") or {}
        if task_id and action and reporter_owns_task(task_id, agent_id):
            db.log_activity("task", task_id, action, details)

    elif msg_type == "submit_child":
        await _handle_submit_child(agent_id, msg)


def _send_child_rejected(agent_id: str, request_id: Optional[str], reason: str, message: str = ""):
    return manager.send(
        agent_id,
        {
            "type": "child_rejected",
            "request_id": request_id or "",
            "reason": reason,
            "message": message,
        },
    )


async def _handle_submit_child(parent_agent_id: str, msg: dict):
    """Handle a `submit_child` WS message per design §2.2.1."""
    request_id = msg.get("request_id")

    # 1) Schema validation.
    try:
        req = SubmitChildRequest(**{k: v for k, v in msg.items() if k != "type"})
    except ValidationError as exc:
        await _send_child_rejected(parent_agent_id, request_id, "bad_request", str(exc))
        return

    # 2) Resolve the parent task — strict: req.parent_task_id is authoritative.
    candidate = queue.get(req.parent_task_id)
    if candidate is None or candidate.assigned_agent_id != parent_agent_id:
        await _send_child_rejected(
            parent_agent_id, request_id, "not_owner",
            "parent_task_id not owned by agent",
        )
        return
    if candidate.status not in (TaskStatus.ASSIGNED, TaskStatus.RUNNING):
        await _send_child_rejected(
            parent_agent_id, request_id, "parent_terminal",
            f"parent task is {candidate.status}",
        )
        return
    parent_task = candidate
    parent_task_id = parent_task.task_id

    # 3) Cycle / depth check via delegation helper. The walker prefers
    # the live in-memory queue (attribute access) and falls back to the DB.
    def _lookup_task(tid: str):
        live = queue.get(tid)
        if live is not None:
            return live
        row = db.get_task(tid)
        if row is None:
            return None
        return _task_from_row(row)

    rejection = delegation.check_delegation(
        parent_task_id,
        req.target_agent,
        parent_agent_id,
        _lookup_task,
    )
    if rejection is not None:
        await _send_child_rejected(parent_agent_id, request_id, rejection)
        return

    # 4) Permission check on the *current* session key (immediate revocation).
    session_key = manager.session_key_for(parent_agent_id)
    if not access_control.can_assign_task(session_key, req.target_agent, requester="agent"):
        await _send_child_rejected(parent_agent_id, request_id, "forbidden")
        return

    # 5) Create the child row.
    child_id = str(uuid.uuid4())
    payload = req.payload or {}
    task_text = req.task or payload.get("task", "")
    if task_text and "task" not in payload:
        payload = {**payload, "task": task_text}
    task_type = req.task_type or (router.detect_task_type(task_text) if task_text else "general")

    child = queue.create(
        child_id,
        f"agent:{parent_agent_id}",
        task_type,
        payload,
        req.target_agent,
        task=task_text,
        priority=req.priority,
        timeout=req.timeout,
        min_version=req.min_version or 1,
        parent_task_id=parent_task_id,
    )
    save_task(child)
    db.log_activity(
        "task",
        child_id,
        "delegated_by_agent",
        {"parent_task_id": parent_task_id, "parent_agent_id": parent_agent_id},
    )

    # Propagate root submitter from parent → child (design §3.8).
    # peek_submitter is non-destructive; if the parent has no row (anonymous
    # / pre-phase), we still record (child_id, None, None) so the row
    # invariant for live child tasks holds.
    try:
        parent_row = db.peek_submitter(parent_task_id) or {}
        db.record_submitter(
            child_id,
            parent_row.get("api_key_name"),
            parent_row.get("owner_email"),
        )
    except Exception as exc:
        db.log_activity(
            "task", child_id, "record_submitter_failed", {"error": str(exc)}
        )

    emit_event(
        "task_created",
        task_event_data(
            child_id,
            task_type=task_type,
            submitted_by=child.submitted_by,
        ),
    )

    delegation.register_child(parent_task_id, child_id)

    def _rollback_child():
        """Roll back queue + DB + delegation bookkeeping for a failed child create."""
        delegation.release_child(child_id)
        with queue._lock:
            queue._tasks.pop(child_id, None)
        try:
            with db.get_db() as conn:
                conn.cursor().execute("DELETE FROM tasks WHERE id = ?", (child_id,))
                conn.commit()
        except Exception:
            pass
        # v3 Finding 2: drop the task_submitters row inserted above so a
        # rejected child doesn't leak attribution state. pop_submitter is a
        # no-op when the row is already absent, so it's safe to call
        # unconditionally across every rejection branch.
        try:
            db.pop_submitter(child_id)
        except Exception:
            pass

    accepted_status = "queued"

    # 6) Dispatch — self-child fast path bypasses Router.route() (design §2.2.1 step 7).
    if req.target_agent == parent_agent_id:
        # F3 self-fast-path capability gate.
        parent_agent = registry.get(parent_agent_id)
        if parent_agent is None or not _agent_can_handle(parent_agent, child):
            _rollback_child()
            await _send_child_rejected(
                parent_agent_id,
                request_id,
                "capability_unavailable",
                f"self does not advertise {task_type}@v{req.min_version or 1}+",
            )
            return
        if not queue.assign(child_id, parent_agent_id):
            _rollback_child()
            await _send_child_rejected(
                parent_agent_id, request_id, "target_unavailable", "self-assign failed"
            )
            return
        save_task(queue.get(child_id))
        emit_event(
            "task_updated",
            task_event_data(
                child_id,
                status=TaskStatus.ASSIGNED.value,
                agent_id=parent_agent_id,
            ),
        )
        sent = await send_task_to_agent(parent_agent_id, child_id)
        if not sent:
            _rollback_child()
            await _send_child_rejected(
                parent_agent_id, request_id, "target_unavailable", "could not send to self"
            )
            return
        accepted_status = "assigned"
    else:
        # F3 other-target capability gate (before dispatch).
        target_agent = registry.get(req.target_agent)
        if target_agent is None or not _agent_can_handle(target_agent, child):
            _rollback_child()
            await _send_child_rejected(
                parent_agent_id,
                request_id,
                "capability_unavailable",
                f"{req.target_agent} cannot handle {task_type}@v{req.min_version or 1}+",
            )
            return
        routed = await route_and_dispatch(child_id, min_version=req.min_version or 1)
        if routed:
            accepted_status = "assigned"
        else:
            # Target not idle → child stays queued. This is acceptable per design;
            # it will dispatch when the target frees up via dispatch_pending_for_agent.
            accepted_status = "queued"

    # 7) Send child_accepted to parent.
    await manager.send(
        parent_agent_id,
        {
            "type": "child_accepted",
            "request_id": request_id,
            "child_task_id": child_id,
            "status": accepted_status,
        },
    )


def _resolve_known_payload_schema(task_type: str, min_version: int) -> tuple[Optional[dict], Optional[int]]:
    """Walk online agents for the highest matching capability with a non-null
    payload_schema for `(task_type, version >= min_version)`. Used by
    submit-time preflight (Fix F4). Returns (schema, chosen_version) or
    (None, None) if no schema-bearing agent is online."""
    best_version: Optional[int] = None
    best_schema: Optional[dict] = None
    for agent in registry.list_all():
        if agent.status == AgentStatus.OFFLINE:
            continue
        ver = _resolve_cap_version(agent, task_type, min_version or 1)
        if ver is None:
            continue
        schema = _resolve_payload_schema(agent, task_type, ver)
        if schema is None:
            continue
        if best_version is None or ver > best_version:
            best_version = ver
            best_schema = schema
    return best_schema, best_version


@app.post("/tasks", response_model=TaskSubmitResponse)
async def submit_task(req: TaskSubmitRequest, authorization: Optional[str] = Header(None)):
    require_assign(authorization, req.target_agent or "")
    if not req.task and not req.payload:
        raise HTTPException(status_code=400, detail="task or payload required")

    task_id = str(uuid.uuid4())
    payload = req.payload or {}
    task_text = req.task or payload.get("task", "")
    if task_text and "task" not in payload:
        payload = {**payload, "task": task_text}
    task_type = req.task_type or (router.detect_task_type(task_text) if task_text else "general")
    min_version = req.min_version or 1

    # F4 submit preflight: only when a schema-bearing matching agent is already
    # known. In strict mode this is a hard reject before task creation.
    schema, chosen_version = _resolve_known_payload_schema(task_type, min_version)
    if schema is not None:
        err = validate_payload(payload or {}, schema)
        if err is not None:
            mode = current_mode()
            detail = {
                "error": "payload_schema_violation",
                "capability": task_type,
                "capability_version": chosen_version,
                "message": err,
            }
            if mode == "strict":
                raise HTTPException(status_code=400, detail=detail)
            elif mode == "warn":
                # log + continue with creation
                db.log_activity("task", task_id, "payload_schema_warn", detail)

    task = queue.create(
        task_id,
        "human",
        task_type,
        payload,
        req.target_agent,
        task=task_text,
        priority=req.priority,
        timeout=req.timeout,
        min_version=min_version,
        parent_task_id=req.parent_task_id,
    )
    save_task(task)

    # Record submitter attribution (design §3.7). Resolve the bearer key →
    # (name, owner) via the same hash lookup auth.validate_key uses; tolerate
    # missing/anon bearers by recording (None, None).
    raw_bearer = get_auth_key(authorization) or ""
    submitter_name: Optional[str] = None
    submitter_owner: Optional[str] = None
    if raw_bearer:
        try:
            key_row = db.get_api_key_by_hash(access_control.hash_key(raw_bearer))
        except Exception:
            key_row = None
        if key_row:
            submitter_name = key_row.get("name")
            submitter_owner = key_row.get("owner")
    try:
        db.record_submitter(task_id, submitter_name, submitter_owner)
    except Exception as exc:
        db.log_activity(
            "task", task_id, "record_submitter_failed", {"error": str(exc)}
        )

    db.log_activity("task", task_id, "created", {"task_type": task_type})
    emit_event("task_created", task_event_data(task_id, task_type=task_type))

    agent_id = await route_and_dispatch(task_id, min_version=min_version)
    status = TaskStatus.ASSIGNED if agent_id else TaskStatus.QUEUED
    return TaskSubmitResponse(task_id=task_id, status=status)


@app.get("/tasks/{task_id}")
def get_task(
    task_id: str,
    authorization: Optional[str] = Header(None),
    cf_access_jwt: Optional[str] = Header(None, alias="Cf-Access-Jwt-Assertion"),
):
    require_view_tasks(authorization, cf_access_jwt)
    task = queue.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, authorization: Optional[str] = Header(None)):
    """Cancel a non-terminal task. Marks it FAILED with `result.error="cancelled"`.

    A task already cancelled while assigned/running stays in the agent's
    process until the agent finishes naturally; the late result is dropped
    by `finalize_task`'s terminal-status guard.
    """
    require_assign(authorization)
    task = queue.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status in TERMINAL_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Task is already terminal (status={task.status.value})",
        )
    ok = await finalize_task(
        task_id,
        TaskStatus.FAILED,
        result={"error": "cancelled", "cancelled_via": "api"},
    )
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to cancel task")
    return {"status": "cancelled", "task_id": task_id}


@app.get("/tasks")
def list_tasks(
    status: Optional[str] = None,
    agent_id: Optional[str] = None,
    parent_task_id: Optional[str] = None,
    top_level: bool = False,
    authorization: Optional[str] = Header(None),
    cf_access_jwt: Optional[str] = Header(None, alias="Cf-Access-Jwt-Assertion"),
):
    require_view_tasks(authorization, cf_access_jwt)
    try:
        return queue.list_all(status, agent_id, parent_task_id=parent_task_id, top_level=top_level)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="top_level cannot be combined with parent_task_id",
        )


@app.get("/agents")
def list_agents(
    authorization: Optional[str] = Header(None),
    cf_access_jwt: Optional[str] = Header(None, alias="Cf-Access-Jwt-Assertion"),
):
    require_view_agents(authorization, cf_access_jwt)
    return registry.list_all()


@app.get("/capabilities")
def list_capabilities(
    authorization: Optional[str] = Header(None),
    cf_access_jwt: Optional[str] = Header(None, alias="Cf-Access-Jwt-Assertion"),
):
    """Phase 2.2 Fix F6: top-level JSON array of aggregated capability rows."""
    require_view_agents(authorization, cf_access_jwt)
    return aggregate_capabilities(registry.list_all())


@app.get("/agents/{agent_id}")
def get_agent(
    agent_id: str,
    authorization: Optional[str] = Header(None),
    cf_access_jwt: Optional[str] = Header(None, alias="Cf-Access-Jwt-Assertion"),
):
    require_view_agents(authorization, cf_access_jwt)
    agent = registry.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@app.get("/events")
async def events(
    request: Request,
    authorization: Optional[str] = Header(None),
    cf_access_jwt: Optional[str] = Header(None, alias="Cf-Access-Jwt-Assertion"),
):
    require_view_tasks(authorization, cf_access_jwt)
    subscriber = asyncio.Queue()
    event_subscribers.append(subscriber)

    async def event_generator():
        try:
            yield {"event": "message", "data": json.dumps({"type": "connected", "time": datetime.now().isoformat()})}
            while not await request.is_disconnected():
                event = await subscriber.get()
                yield {"event": "message", "data": json.dumps(event)}
        finally:
            if subscriber in event_subscribers:
                event_subscribers.remove(subscriber)

    return EventStreamResponse(event_generator())


@app.get("/health")
def health():
    stats = db.get_stats()
    return {
        "status": "ok",
        "agents": len(registry.list_all()),
        "tasks": len(queue.list_all()),
        "db": stats,
    }


@app.get("/stats")
def get_stats(
    authorization: Optional[str] = Header(None),
    cf_access_jwt: Optional[str] = Header(None, alias="Cf-Access-Jwt-Assertion"),
):
    require_view_tasks(authorization, cf_access_jwt)
    return db.get_stats()


WWW_AUTH = 'Bearer realm="agent-hub"'


@app.post("/mcp")
async def mcp_endpoint(request: Request, authorization: Optional[str] = Header(None)):
    auth_key = get_auth_key(authorization)
    if not auth_key or access_control.validate_key(auth_key) is None:
        # MCP 2025-03-26 auth § Bearer challenge
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32001, "message": "authentication required"}},
            status_code=401,
            headers={"WWW-Authenticate": WWW_AUTH},
        )
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}},
            status_code=400,
        )
    response = await handle_jsonrpc(payload, InProcessHubBackend(auth_key))
    if response is None:
        return JSONResponse({}, status_code=202)
    return response


class CreateKeyRequest(BaseModel):
    name: str
    can_register: bool = False
    can_assign_tasks: bool = False
    can_view_agents: bool = True
    can_view_tasks: bool = True
    allowed_agents: list[str] = []
    is_admin: bool = False
    expires_days: Optional[int] = None


class CreateKeyResponse(BaseModel):
    api_key: str
    name: str
    permission: dict


@app.post("/admin/keys", response_model=CreateKeyResponse)
def create_api_key(req: CreateKeyRequest, authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    permission = Permission(
        can_register=req.can_register,
        can_assign_tasks=req.can_assign_tasks,
        can_view_agents=req.can_view_agents,
        can_view_tasks=req.can_view_tasks,
        allowed_agents=req.allowed_agents,
        is_admin=req.is_admin,
    )
    raw_key, api_key = access_control.create_api_key(req.name, permission, expires_days=req.expires_days)
    return CreateKeyResponse(api_key=raw_key, name=api_key.name, permission=api_key.permission.model_dump())


@app.get("/admin/keys")
def list_api_keys(authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    return access_control.list_keys()


@app.delete("/admin/keys/{key_name}")
def revoke_api_key(key_name: str, authorization: Optional[str] = Header(None)):
    require_admin(authorization)
    if access_control.revoke_key(key_name):
        return {"status": "revoked", "name": key_name}
    raise HTTPException(status_code=404, detail="Key not found")


# ============================================================================
# Phase user-tokens — /me/* and /admin/usage endpoints (design v3 §3)
# ============================================================================

import re as _re_user_tokens
from datetime import timedelta as _td_user_tokens

_USER_KEY_NAME_RE = _re_user_tokens.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_USER_KEY_MAX_ACTIVE = 10
_USER_KEY_TTL_DAYS = 90


class CreateUserKeyRequest(BaseModel):
    name: str


def _user_key_permission() -> Permission:
    """Fixed non-admin scope for user-minted keys (design §3.1)."""
    return Permission(
        can_register=False,
        can_assign_tasks=True,
        can_view_agents=True,
        can_view_tasks=True,
        allowed_agents=[],
        is_admin=False,
    )


def _key_active(row: dict) -> bool:
    """True iff the api_keys row is currently active and not past expiry."""
    if not row.get("active"):
        return False
    expires_at = row.get("expires_at")
    if expires_at:
        try:
            if datetime.fromisoformat(expires_at) < datetime.now():
                return False
        except (TypeError, ValueError):
            pass
    return True


def _list_keys_for_owner(email: str) -> list[dict]:
    return [row for row in db.list_api_keys() if (row.get("owner") or "") == email]


def _key_row_to_public(row: dict) -> dict:
    """Public shape: no raw key, no key_hash."""
    return {
        "name": row["name"],
        "owner": row.get("owner"),
        "permission": {
            "can_register": bool(row.get("can_register")),
            "can_assign_tasks": bool(row.get("can_assign_tasks")),
            "can_view_agents": bool(row.get("can_view_agents")),
            "can_view_tasks": bool(row.get("can_view_tasks")),
            "allowed_agents": json.loads(row.get("allowed_agents") or "[]"),
            "is_admin": bool(row.get("is_admin")),
        },
        "active": bool(row.get("active")),
        "created_at": row.get("created_at"),
        "expires_at": row.get("expires_at"),
        "revoked": not bool(row.get("active")),
    }


@app.post("/me/keys")
def create_me_key(
    req: CreateUserKeyRequest,
    cf_access_jwt: Optional[str] = Header(None, alias="Cf-Access-Jwt-Assertion"),
):
    email = require_jwt_email(cf_access_jwt)
    if not req.name or not _USER_KEY_NAME_RE.match(req.name):
        raise HTTPException(
            status_code=422,
            detail="name must be 1-64 chars of [A-Za-z0-9_.-]",
        )

    # Soft cap on active keys per email.
    owned = _list_keys_for_owner(email)
    active_count = sum(1 for r in owned if _key_active(r))
    if active_count >= _USER_KEY_MAX_ACTIVE:
        raise HTTPException(
            status_code=429, detail=f"Max {_USER_KEY_MAX_ACTIVE} active keys per user"
        )

    # Duplicate name: 409 if the SAME owner already has this name. (Cross-owner
    # name collisions would also collide on the global UNIQUE(name) constraint,
    # which surfaces as a generic 500; we don't expose other users' name
    # existence here.)
    for r in owned:
        if r.get("name") == req.name:
            raise HTTPException(status_code=409, detail="Key name already in use")

    permission = _user_key_permission()
    import secrets as _secrets
    raw_key = _secrets.token_urlsafe(32)
    key_hash = access_control.hash_key(raw_key)
    created_at = datetime.now()
    expires_at = created_at + _td_user_tokens(days=_USER_KEY_TTL_DAYS)
    try:
        db.save_api_key(
            {
                "key_hash": key_hash,
                "name": req.name,
                "can_register": permission.can_register,
                "can_assign_tasks": permission.can_assign_tasks,
                "can_view_agents": permission.can_view_agents,
                "can_view_tasks": permission.can_view_tasks,
                "allowed_agents": permission.allowed_agents,
                "is_admin": permission.is_admin,
                "active": True,
                "created_at": created_at.isoformat(),
                "expires_at": expires_at.isoformat(),
            },
            owner=email,
        )
    except Exception as exc:
        # Most likely a UNIQUE(name) collision against another owner's key.
        raise HTTPException(status_code=409, detail=f"Could not create key: {exc}")
    db.log_activity("key", req.name, "created", {"owner": email})

    return {
        "api_key": raw_key,
        "name": req.name,
        "owner": email,
        "permission": permission.model_dump(),
        "created_at": created_at.isoformat(),
        "expires_at": expires_at.isoformat(),
    }


@app.get("/me/keys")
def list_me_keys(
    cf_access_jwt: Optional[str] = Header(None, alias="Cf-Access-Jwt-Assertion"),
):
    email = require_jwt_email(cf_access_jwt)
    return [_key_row_to_public(r) for r in _list_keys_for_owner(email)]


@app.delete("/me/keys/{name}")
def revoke_me_key(
    name: str,
    cf_access_jwt: Optional[str] = Header(None, alias="Cf-Access-Jwt-Assertion"),
):
    email = require_jwt_email(cf_access_jwt)
    # 404 if not present OR not owned by caller (don't leak existence).
    owned_names = {r.get("name") for r in _list_keys_for_owner(email)}
    if name not in owned_names:
        raise HTTPException(status_code=404, detail="Key not found")
    if not access_control.revoke_key(name):
        raise HTTPException(status_code=404, detail="Key not found")
    return {"status": "revoked", "name": name}


def _usage_aggregates(rows: list[dict]) -> dict:
    """Compute aggregates over already-filtered rows.

    by_day uses Python `completed_at[:10]` bucketing (design v3 Finding 2 — do
    NOT use SQLite substr).
    """
    by_day: dict[str, dict] = {}
    by_agent: dict[str, dict] = {}
    total_cost = 0.0
    total_input = 0
    total_output = 0
    total_duration = 0.0
    top_level_n = 0
    delegated_n = 0
    for row in rows:
        completed = row.get("completed_at")
        day = (
            completed[:10]
            if isinstance(completed, str) and len(completed) >= 10
            else None
        )
        cost = row.get("cost_usd") or 0.0
        if day is not None:
            slot = by_day.setdefault(day, {"day": day, "cost_usd": 0.0, "count": 0})
            slot["cost_usd"] += cost
            slot["count"] += 1
        agent_id = row.get("agent_id") or ""
        a_slot = by_agent.setdefault(
            agent_id, {"agent_id": agent_id, "cost_usd": 0.0, "count": 0}
        )
        a_slot["cost_usd"] += cost
        a_slot["count"] += 1
        total_cost += cost
        if row.get("input_tokens"):
            total_input += int(row["input_tokens"])
        if row.get("output_tokens"):
            total_output += int(row["output_tokens"])
        if row.get("duration_seconds"):
            total_duration += float(row["duration_seconds"])
        if row.get("parent_task_id"):
            delegated_n += 1
        else:
            top_level_n += 1
    return {
        "by_day": sorted(by_day.values(), key=lambda x: x["day"]),
        "by_agent": sorted(by_agent.values(), key=lambda x: x["agent_id"]),
        "total_cost_usd": total_cost,
        "total_count": len(rows),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_duration_seconds": total_duration,
        "top_level_row_count": top_level_n,
        "delegated_row_count": delegated_n,
    }


def _parse_usage_query(
    from_: Optional[str], to_: Optional[str], limit: Optional[int]
) -> tuple[datetime, datetime, int]:
    now = datetime.now()
    from_dt = _parse_datetime(from_) if from_ else now - _td_user_tokens(days=30)
    to_dt = _parse_datetime(to_) if to_ else now
    eff_limit = limit if limit is not None else 1000
    try:
        eff_limit = int(eff_limit)
    except (TypeError, ValueError):
        eff_limit = 1000
    eff_limit = max(1, min(eff_limit, 1000))
    return from_dt, to_dt, eff_limit


@app.get("/me/usage")
def get_me_usage(
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    agent: Optional[str] = None,
    top_level_only: bool = False,
    limit: Optional[int] = None,
    cf_access_jwt: Optional[str] = Header(None, alias="Cf-Access-Jwt-Assertion"),
):
    email = require_jwt_email(cf_access_jwt)
    from_dt, to_dt, eff_limit = _parse_usage_query(from_, to, limit)
    rows = db.list_task_usage(
        owner=email,
        agent=agent,
        from_dt=from_dt.isoformat() if from_dt else None,
        to_dt=to_dt.isoformat() if to_dt else None,
        top_level_only=top_level_only,
        limit=eff_limit,
    )
    aggs = _usage_aggregates(rows)
    return {
        "rows": rows,
        "by_day": aggs["by_day"],
        "by_agent": aggs["by_agent"],
        "total_cost_usd": aggs["total_cost_usd"],
        "total_count": aggs["total_count"],
        "total_input_tokens": aggs["total_input_tokens"],
        "total_output_tokens": aggs["total_output_tokens"],
        "total_duration_seconds": aggs["total_duration_seconds"],
        "top_level_row_count": aggs["top_level_row_count"],
        "delegated_row_count": aggs["delegated_row_count"],
        "from": from_dt.isoformat() if from_dt else None,
        "to": to_dt.isoformat() if to_dt else None,
        "limit": eff_limit,
    }


@app.get("/admin/usage")
def get_admin_usage(
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    agent: Optional[str] = None,
    email: Optional[str] = None,
    top_level_only: bool = False,
    limit: Optional[int] = None,
    authorization: Optional[str] = Header(None),
):
    require_admin(authorization)
    from_dt, to_dt, eff_limit = _parse_usage_query(from_, to, limit)
    filt_email = (email or "").lower().strip() or None
    rows = db.list_task_usage(
        owner=filt_email,
        agent=agent,
        from_dt=from_dt.isoformat() if from_dt else None,
        to_dt=to_dt.isoformat() if to_dt else None,
        top_level_only=top_level_only,
        limit=eff_limit,
    )
    aggs = _usage_aggregates(rows)
    return {
        "rows": rows,
        "by_day": aggs["by_day"],
        "by_agent": aggs["by_agent"],
        "total_cost_usd": aggs["total_cost_usd"],
        "total_count": aggs["total_count"],
        "total_input_tokens": aggs["total_input_tokens"],
        "total_output_tokens": aggs["total_output_tokens"],
        "total_duration_seconds": aggs["total_duration_seconds"],
        "top_level_row_count": aggs["top_level_row_count"],
        "delegated_row_count": aggs["delegated_row_count"],
        "from": from_dt.isoformat() if from_dt else None,
        "to": to_dt.isoformat() if to_dt else None,
        "limit": eff_limit,
    }


# --- Dashboard static mount (phase dashboard-v1) ---
# Unconditional `/` -> `/ui/` redirect, registered even when `web/` is absent
# so a missing SPA bundle still produces a redirect from the root URL.
@app.get("/")
def _root():
    return RedirectResponse("/ui/", status_code=307)


WEB_DIR = pathlib.Path(__file__).resolve().parent.parent / "web"
if WEB_DIR.exists():
    # Explicit SPA route registered BEFORE the StaticFiles mount so the bare
    # trailing-slash URL serves index.html rather than getting 404'd by
    # StaticFiles (which does not serve directory indexes when html=False).
    @app.get("/ui/")
    def _ui_index():
        return FileResponse(WEB_DIR / "index.html", media_type="text/html")

    app.mount("/ui", StaticFiles(directory=str(WEB_DIR), html=False), name="ui")

    @app.get("/chat")
    def _chat_page():
        return FileResponse(WEB_DIR / "chat.html", media_type="text/html")
