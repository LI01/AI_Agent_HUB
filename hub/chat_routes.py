"""Chat training platform — REST API routes."""
from __future__ import annotations

import base64
import os
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from . import chat_db
from . import database as db
from .chat_files import WorkspaceManager, sanitize_path
from .chat_models import (
    ChatMessageRequest,
    CreateAgentRequest,
    CreateConversationRequest,
    ScoreSubmissionRequest,
)
from .models import AgentStatus, TaskStatus

chat_router = APIRouter(prefix="/chat")

_workspace: WorkspaceManager | None = None


def _get_main_objects():
    """Lazy import to avoid circular dependency with hub.main."""
    from .main import cf_access_verifier, manager, queue, registry
    return cf_access_verifier, manager, queue, registry


def get_workspace() -> WorkspaceManager:
    global _workspace
    if _workspace is None:
        root = os.getenv("AGENT_HUB_CHAT_WORKSPACE_ROOT", "/data/user-workspaces")
        template_root = os.getenv("AGENT_HUB_CHAT_TEMPLATE_ROOT", "/data/training-templates")
        _workspace = WorkspaceManager(root, template_root)
    return _workspace


def _get_admin_emails() -> list[str]:
    raw = os.getenv("AGENT_HUB_ADMIN_EMAILS", "")
    return [e.strip().lower() for e in raw.split(",") if e.strip()]


def _require_chat_jwt(cf_access_jwt: Optional[str]) -> str:
    """Verify CF Access JWT and return email. Raises 403 on failure."""
    cf_access_verifier, _, _, _ = _get_main_objects()
    if not cf_access_jwt or cf_access_verifier is None:
        raise HTTPException(status_code=403, detail="CF Access JWT required")
    claims = cf_access_verifier.verify(cf_access_jwt)
    if not claims:
        raise HTTPException(status_code=403, detail="CF Access JWT invalid")
    email = (claims.get("email") or "").lower().strip()
    if not email:
        raise HTTPException(status_code=403, detail="JWT missing email")
    return email


def _ensure_user(email: str, name: Optional[str] = None) -> dict:
    """Upsert user from JWT and return user dict."""
    return chat_db.upsert_user(email, name, _get_admin_emails())


def _require_admin_jwt(cf_access_jwt: Optional[str]) -> dict:
    """Verify CF Access JWT and ensure user is admin. Returns user dict."""
    email = _require_chat_jwt(cf_access_jwt)
    user = chat_db.get_user_by_email(email)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# ============== User Endpoints ==============

@chat_router.get("/templates")
def api_list_templates(
    role: Optional[str] = Query(None),
    cf_access_jwt: Optional[str] = Header(None, alias="Cf-Access-Jwt-Assertion"),
):
    _require_chat_jwt(cf_access_jwt)
    return chat_db.list_task_templates(role)


@chat_router.get("/templates/{template_id}")
def api_get_template(
    template_id: str,
    cf_access_jwt: Optional[str] = Header(None, alias="Cf-Access-Jwt-Assertion"),
):
    _require_chat_jwt(cf_access_jwt)
    tpl = chat_db.get_task_template(template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    return tpl


@chat_router.get("/conversations")
def api_list_conversations(
    cf_access_jwt: Optional[str] = Header(None, alias="Cf-Access-Jwt-Assertion"),
):
    email = _require_chat_jwt(cf_access_jwt)
    user = _ensure_user(email)
    return chat_db.list_conversations(user["id"])


@chat_router.post("/conversations")
def api_create_conversation(
    req: CreateConversationRequest,
    cf_access_jwt: Optional[str] = Header(None, alias="Cf-Access-Jwt-Assertion"),
):
    email = _require_chat_jwt(cf_access_jwt)
    user = _ensure_user(email)
    conv = chat_db.create_conversation(
        user_id=user["id"],
        agent_id=req.agent_id,
        title=req.title,
        task_template_id=req.task_template_id,
    )
    if req.task_template_id:
        ws = get_workspace()
        user_path = ws.get_user_path(user["id"])
        ws.copy_template_files(req.task_template_id, user_path)
        tpl = chat_db.get_task_template(req.task_template_id)
        if tpl and tpl.get("description"):
            chat_db.create_message(
                conversation_id=conv["id"],
                role="system",
                content=tpl["description"],
            )
    return conv


@chat_router.get("/conversations/{conv_id}/messages")
def api_get_messages(
    conv_id: str,
    cf_access_jwt: Optional[str] = Header(None, alias="Cf-Access-Jwt-Assertion"),
):
    email = _require_chat_jwt(cf_access_jwt)
    user = _ensure_user(email)
    conv = chat_db.get_conversation(conv_id)
    if not conv or conv["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return chat_db.list_messages(conv_id)


@chat_router.post("/conversations/{conv_id}/submit")
def api_submit_conversation(
    conv_id: str,
    cf_access_jwt: Optional[str] = Header(None, alias="Cf-Access-Jwt-Assertion"),
):
    email = _require_chat_jwt(cf_access_jwt)
    user = _ensure_user(email)
    conv = chat_db.get_conversation(conv_id)
    if not conv or conv["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conv["status"] == "submitted":
        raise HTTPException(status_code=409, detail="Already submitted")
    chat_db.update_conversation_status(conv_id, "submitted")
    return chat_db.create_submission(conv_id, user["id"])


@chat_router.get("/agents/available")
def api_get_available_agents(
    cf_access_jwt: Optional[str] = Header(None, alias="Cf-Access-Jwt-Assertion"),
):
    _require_chat_jwt(cf_access_jwt)
    _, _, _, registry = _get_main_objects()
    agents = registry.list_all()
    return [
        {
            "agent_id": a.agent_id,
            "name": a.name,
            "capabilities": [c.name if hasattr(c, "name") else c for c in (a.capabilities or [])],
            "status": a.status.value if hasattr(a.status, "value") else str(a.status),
        }
        for a in agents
        if (a.status == AgentStatus.IDLE or str(a.status) == "idle")
    ]


# ============== File Endpoints ==============

@chat_router.get("/files")
def api_list_files(
    path: str = Query(default=""),
    cf_access_jwt: Optional[str] = Header(None, alias="Cf-Access-Jwt-Assertion"),
):
    email = _require_chat_jwt(cf_access_jwt)
    user = _ensure_user(email)
    ws = get_workspace()
    user_path = ws.get_user_path(user["id"])
    try:
        return ws.list_directory(user_path, path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@chat_router.post("/upload")
async def api_upload_file(
    request: Request,
    path: str = Query(...),
    cf_access_jwt: Optional[str] = Header(None, alias="Cf-Access-Jwt-Assertion"),
):
    """Upload a file (raw bytes in request body)."""
    email = _require_chat_jwt(cf_access_jwt)
    user = _ensure_user(email)
    ws = get_workspace()
    user_path = ws.get_user_path(user["id"])
    body = await request.body()
    max_size = int(os.getenv("AGENT_HUB_CHAT_MAX_FILE_SIZE", "10485760"))
    if len(body) > max_size:
        raise HTTPException(status_code=413, detail=f"File too large (max {max_size} bytes)")
    try:
        ws.write_file(user_path, path, body)
        return {"status": "uploaded", "path": path}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@chat_router.delete("/files")
def api_delete_file(
    path: str = Query(...),
    cf_access_jwt: Optional[str] = Header(None, alias="Cf-Access-Jwt-Assertion"),
):
    email = _require_chat_jwt(cf_access_jwt)
    user = _ensure_user(email)
    ws = get_workspace()
    user_path = ws.get_user_path(user["id"])
    try:
        ws.delete_file(user_path, path)
        return {"status": "deleted"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@chat_router.post("/files/copy-template")
def api_copy_template(
    template_id: str = Query(...),
    cf_access_jwt: Optional[str] = Header(None, alias="Cf-Access-Jwt-Assertion"),
):
    email = _require_chat_jwt(cf_access_jwt)
    user = _ensure_user(email)
    ws = get_workspace()
    user_path = ws.get_user_path(user["id"])
    ws.copy_template_files(template_id, user_path)
    return {"status": "copied"}


# ============== Admin Endpoints ==============

admin_chat_router = APIRouter(prefix="/admin/chat")


@admin_chat_router.get("/users")
def admin_list_users(
    cf_access_jwt: Optional[str] = Header(None, alias="Cf-Access-Jwt-Assertion"),
):
    _require_admin_jwt(cf_access_jwt)
    return chat_db.list_users()


@admin_chat_router.post("/users/{user_id}/role")
def admin_set_user_role(
    user_id: str,
    role: Optional[str] = Query(None),
    group_number: Optional[int] = Query(None),
    cf_access_jwt: Optional[str] = Header(None, alias="Cf-Access-Jwt-Assertion"),
):
    _require_admin_jwt(cf_access_jwt)
    ok = chat_db.update_user_role(user_id, role, group_number)
    if not ok:
        raise HTTPException(status_code=404, detail="User not found")
    return {"status": "updated"}


@admin_chat_router.get("/users/{user_id}/conversations")
def admin_view_user_conversations(
    user_id: str,
    cf_access_jwt: Optional[str] = Header(None, alias="Cf-Access-Jwt-Assertion"),
):
    _require_admin_jwt(cf_access_jwt)
    return chat_db.list_conversations(user_id)


@admin_chat_router.get("/agents")
def admin_list_agents(
    cf_access_jwt: Optional[str] = Header(None, alias="Cf-Access-Jwt-Assertion"),
):
    _require_admin_jwt(cf_access_jwt)
    _, _, _, registry = _get_main_objects()
    agents = registry.list_all()
    return [
        {
            "agent_id": a.agent_id,
            "name": a.name,
            "status": a.status.value if hasattr(a.status, "value") else str(a.status),
            "capabilities": [c.name if hasattr(c, "name") else c for c in (a.capabilities or [])],
        }
        for a in agents
    ]


@admin_chat_router.post("/agents")
def admin_create_agent(
    req: CreateAgentRequest,
    cf_access_jwt: Optional[str] = Header(None, alias="Cf-Access-Jwt-Assertion"),
):
    """Create a new agent: generate API key, register, return install command."""
    _require_admin_jwt(cf_access_jwt)
    import hashlib
    import secrets

    raw_key = f"sk-chat-{secrets.token_hex(24)}"
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

    agent_key_name = f"chat-agent-{req.name}"
    db.save_api_key({
        "key_hash": key_hash,
        "name": agent_key_name,
        "can_register": True,
        "can_assign_tasks": False,
        "can_view_agents": True,
        "can_view_tasks": True,
        "is_admin": False,
        "active": True,
    }, owner="chat-admin")

    hub_url = os.getenv("AGENT_HUB_URL", "http://localhost:8080")
    install_cmd = f"AGENT_HUB_API_KEY={raw_key} AGENT_HUB_URL={hub_url} bash scripts/install_agent_services.sh"

    return {
        "agent_id": f"{req.cli}-{req.role}-{os.uname().nodename}",
        "api_key": raw_key,
        "install_command": install_cmd,
    }


@admin_chat_router.get("/submissions")
def admin_list_submissions(
    status: Optional[str] = Query(None),
    cf_access_jwt: Optional[str] = Header(None, alias="Cf-Access-Jwt-Assertion"),
):
    _require_admin_jwt(cf_access_jwt)
    return chat_db.list_submissions(status)


@admin_chat_router.post("/submissions/{sub_id}/score")
def admin_score_submission(
    sub_id: str,
    req: ScoreSubmissionRequest,
    cf_access_jwt: Optional[str] = Header(None, alias="Cf-Access-Jwt-Assertion"),
):
    _require_admin_jwt(cf_access_jwt)
    ok = chat_db.score_submission(sub_id, req.score, req.feedback)
    if not ok:
        raise HTTPException(status_code=404, detail="Submission not found")
    return {"status": "scored"}


@admin_chat_router.get("/dashboard")
def admin_dashboard(
    cf_access_jwt: Optional[str] = Header(None, alias="Cf-Access-Jwt-Assertion"),
):
    """Training dashboard: user counts, group progress, submission stats."""
    _require_admin_jwt(cf_access_jwt)
    users = chat_db.list_users()
    conversations = chat_db.list_all_conversations()
    submissions = chat_db.list_submissions()

    by_role = {}
    for u in users:
        role = u.get("role") or "unassigned"
        if role not in by_role:
            by_role[role] = {"users": 0, "conversations": 0, "submitted": 0}
        by_role[role]["users"] += 1

    for c in conversations:
        user = chat_db.get_user(c["user_id"])
        if user:
            role = user.get("role") or "unassigned"
            if role in by_role:
                by_role[role]["conversations"] += 1

    for s in submissions:
        user = chat_db.get_user(s["user_id"])
        if user:
            role = user.get("role") or "unassigned"
            if role in by_role:
                by_role[role]["submitted"] += 1

    return {
        "total_users": len(users),
        "total_conversations": len(conversations),
        "total_submissions": len(submissions),
        "by_role": by_role,
    }
