"""Chat training platform — WebSocket handler."""
from __future__ import annotations

import asyncio
import uuid
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect


async def chat_websocket_endpoint(websocket: WebSocket, token: Optional[str] = None, cf_access_verifier=None, admin_emails: Optional[list[str]] = None):
    """WebSocket endpoint for chat. Auth via query param `token=<cf-jwt>`."""
    if not token or cf_access_verifier is None:
        await websocket.close(code=4003, reason="authentication required")
        return

    claims = cf_access_verifier.verify(token)
    if not claims:
        await websocket.close(code=4003, reason="invalid token")
        return

    email = (claims.get("email") or "").lower().strip()
    if not email:
        await websocket.close(code=4003, reason="missing email")
        return

    from . import chat_db
    user = chat_db.upsert_user(email, claims.get("name"), admin_emails or [])
    if not user:
        await websocket.close(code=500, reason="user creation failed")
        return

    await websocket.accept()

    try:
        while True:
            msg = await websocket.receive_json()
            await handle_chat_message(websocket, user, msg)
    except WebSocketDisconnect:
        pass


async def handle_chat_message(websocket: WebSocket, user: dict, msg: dict):
    """Process a chat message: persist, create task, dispatch to agent."""
    msg_type = msg.get("type")

    if msg_type == "chat_message":
        await _handle_chat_message(websocket, user, msg)


async def _handle_chat_message(websocket: WebSocket, user: dict, msg: dict):
    """Process a chat message and dispatch to agent."""
    from . import chat_db
    from .main import manager, queue, registry, save_task, route_and_dispatch
    from .models import TaskStatus

    conversation_id = msg.get("conversation_id")
    content = msg.get("content", "")
    files = msg.get("files", [])

    if not conversation_id or not content:
        await websocket.send_json({"type": "error", "message": "conversation_id and content required"})
        return

    conv = chat_db.get_conversation(conversation_id)
    if not conv or conv["user_id"] != user["id"]:
        await websocket.send_json({"type": "error", "message": "conversation not found"})
        return
    if conv["status"] == "submitted":
        await websocket.send_json({"type": "error", "message": "conversation is submitted and read-only"})
        return

    user_msg = chat_db.create_message(
        conversation_id=conversation_id,
        role="user",
        content=content,
        files=files if files else None,
    )
    await websocket.send_json({"type": "message_ack", "message_id": user_msg["id"]})

    context_messages = chat_db.get_recent_messages(conversation_id, limit=5)
    context_text = "\n".join(
        f"{m['role']}: {m['content']}"
        for m in context_messages[:-1]
    )

    agent_id = conv["agent_id"]
    task_id = str(uuid.uuid4())
    payload = {
        "task": content,
        "conversation_id": conversation_id,
        "user_email": user["email"],
        "context": context_text,
        "files": files,
    }

    if conv.get("task_template_id"):
        tpl = chat_db.get_task_template(conv["task_template_id"])
        if tpl and tpl.get("skill_template"):
            payload["skill_context"] = tpl["skill_template"]

    task = queue.create(
        task_id,
        f"chat:{user['email']}",
        "general",
        payload,
        agent_id,
        task=content,
        timeout=1800,
    )
    save_task(task)

    with chat_db.get_db() as conn:
        conn.cursor().execute(
            "UPDATE chat_messages SET task_id = ? WHERE id = ?",
            (task_id, user_msg["id"]),
        )
        conn.commit()

    routed = await route_and_dispatch(task_id)
    if routed:
        await websocket.send_json({"type": "agent_status", "status": "thinking"})
    else:
        await websocket.send_json({
            "type": "agent_status",
            "status": "queued",
            "message": "No agent available. Your message is queued.",
        })

    asyncio.create_task(_poll_task_result(websocket, task_id, conversation_id))


async def _poll_task_result(websocket: WebSocket, task_id: str, conversation_id: str):
    """Poll for task completion and push result to chat client."""
    import time
    from . import chat_db
    from .main import queue
    from .models import TaskStatus

    max_wait = 1800
    start = time.time()

    while time.time() - start < max_wait:
        task = queue.get(task_id)
        if task is None:
            await asyncio.sleep(2)
            continue

        if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.TIMEOUT):
            if task.status == TaskStatus.COMPLETED:
                result = task.result or {}
                agent_content = result.get("content") or result.get("response") or str(result)
                agent_files = result.get("files", [])

                chat_db.create_message(
                    conversation_id=conversation_id,
                    role="agent",
                    content=agent_content,
                    files=agent_files if agent_files else None,
                    task_id=task_id,
                )
                await websocket.send_json({
                    "type": "agent_message",
                    "message_id": "msg-latest",
                    "content": agent_content,
                    "files": agent_files,
                })
            else:
                error_msg = (task.result or {}).get("error", "Agent failed")
                chat_db.create_message(
                    conversation_id=conversation_id,
                    role="system",
                    content=f"Agent error: {error_msg}",
                    task_id=task_id,
                )
                await websocket.send_json({
                    "type": "agent_status",
                    "status": "error",
                    "message": error_msg,
                })

            await websocket.send_json({"type": "agent_status", "status": "done"})
            return

        await asyncio.sleep(2)

    await websocket.send_json({
        "type": "agent_status",
        "status": "error",
        "message": "Agent timed out",
    })
