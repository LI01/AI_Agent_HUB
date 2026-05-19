"""Chat training platform — database operations."""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Optional

from .database import get_db


def _now() -> str:
    return datetime.now().isoformat()


def _to_json(value, default=None):
    if value is None:
        return json.dumps(default or {})
    if isinstance(value, str):
        return value
    return json.dumps(value)


def _from_json(value, default=None):
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default or {}


# ============== User Operations ==============

def upsert_user(email: str, name: Optional[str] = None, admin_emails: Optional[list[str]] = None) -> dict:
    """Create or update a user from CF Access JWT claims."""
    email = email.lower().strip()
    now = _now()
    is_admin = email in [e.lower().strip() for e in (admin_emails or [])]
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM chat_users WHERE email = ?", (email,))
        row = cursor.fetchone()
        if row:
            cursor.execute(
                "UPDATE chat_users SET name = ?, last_seen_at = ?, is_admin = ? WHERE email = ?",
                (name, now, int(is_admin), email),
            )
            user_id = row[0]
        else:
            user_id = str(uuid.uuid4())
            cursor.execute(
                "INSERT INTO chat_users (id, email, name, is_admin, created_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, email, name, int(is_admin), now, now),
            )
        conn.commit()
    return get_user(user_id)


def get_user(user_id: str) -> Optional[dict]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM chat_users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_user_by_email(email: str) -> Optional[dict]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM chat_users WHERE email = ?", (email.lower().strip(),))
        row = cursor.fetchone()
        return dict(row) if row else None


def list_users() -> list[dict]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM chat_users ORDER BY last_seen_at DESC")
        return [dict(row) for row in cursor.fetchall()]


def update_user_role(user_id: str, role: Optional[str], group_number: Optional[int]) -> bool:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE chat_users SET role = ?, group_number = ? WHERE id = ?",
            (role, group_number, user_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def disable_user(user_id: str) -> bool:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE chat_users SET is_admin = 0 WHERE id = ?", (user_id,))
        conn.commit()
        return cursor.rowcount > 0


def enable_user(user_id: str) -> bool:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE chat_users SET is_admin = 1 WHERE id = ?", (user_id,))
        conn.commit()
        return cursor.rowcount > 0


# ============== Conversation Operations ==============

def create_conversation(user_id: str, agent_id: str, title: Optional[str] = None, task_template_id: Optional[str] = None) -> dict:
    now = _now()
    conv_id = str(uuid.uuid4())
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO chat_conversations (id, user_id, agent_id, title, task_template_id, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
            (conv_id, user_id, agent_id, title, task_template_id, now, now),
        )
        conn.commit()
    return get_conversation(conv_id)


def get_conversation(conv_id: str) -> Optional[dict]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM chat_conversations WHERE id = ?", (conv_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def list_conversations(user_id: str) -> list[dict]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM chat_conversations WHERE user_id = ? ORDER BY updated_at DESC", (user_id,))
        return [dict(row) for row in cursor.fetchall()]


def list_all_conversations() -> list[dict]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM chat_conversations ORDER BY updated_at DESC")
        return [dict(row) for row in cursor.fetchall()]


def update_conversation_status(conv_id: str, status: str) -> bool:
    now = _now()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE chat_conversations SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, conv_id),
        )
        conn.commit()
        return cursor.rowcount > 0


# ============== Message Operations ==============

def create_message(conversation_id: str, role: str, content: Optional[str] = None, files: Optional[list] = None, task_id: Optional[str] = None) -> dict:
    now = _now()
    msg_id = str(uuid.uuid4())
    files_json = _to_json(files, [])
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO chat_messages (id, conversation_id, role, content, files, task_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (msg_id, conversation_id, role, content, files_json, task_id, now),
        )
        cursor.execute("UPDATE chat_conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
        conn.commit()
    return get_message(msg_id)


def get_message(msg_id: str) -> Optional[dict]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM chat_messages WHERE id = ?", (msg_id,))
        row = cursor.fetchone()
        if row:
            d = dict(row)
            d["files"] = _from_json(d.get("files"), [])
            return d
        return None


def list_messages(conversation_id: str) -> list[dict]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM chat_messages WHERE conversation_id = ? ORDER BY created_at ASC", (conversation_id,))
        result = []
        for row in cursor.fetchall():
            d = dict(row)
            d["files"] = _from_json(d.get("files"), [])
            result.append(d)
        return result


def get_recent_messages(conversation_id: str, limit: int = 5) -> list[dict]:
    """Get the N most recent messages for context injection."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM chat_messages WHERE conversation_id = ? ORDER BY created_at DESC LIMIT ?",
            (conversation_id, limit),
        )
        result = []
        for row in cursor.fetchall():
            d = dict(row)
            d["files"] = _from_json(d.get("files"), [])
            result.append(d)
        return list(reversed(result))


# ============== Submission Operations ==============

def create_submission(conversation_id: str, user_id: str) -> dict:
    now = _now()
    sub_id = str(uuid.uuid4())
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO chat_submissions (id, conversation_id, user_id, submitted_at, status) VALUES (?, ?, ?, ?, 'submitted')",
            (sub_id, conversation_id, user_id, now),
        )
        conn.commit()
    return get_submission(sub_id)


def get_submission(sub_id: str) -> Optional[dict]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM chat_submissions WHERE id = ?", (sub_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def list_submissions(status: Optional[str] = None) -> list[dict]:
    with get_db() as conn:
        cursor = conn.cursor()
        if status:
            cursor.execute("SELECT * FROM chat_submissions WHERE status = ? ORDER BY submitted_at DESC", (status,))
        else:
            cursor.execute("SELECT * FROM chat_submissions ORDER BY submitted_at DESC")
        return [dict(row) for row in cursor.fetchall()]


def score_submission(sub_id: str, score: int, feedback: Optional[str] = None) -> bool:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE chat_submissions SET score = ?, feedback = ?, status = 'reviewed' WHERE id = ?",
            (score, feedback, sub_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def get_user_conversations_with_submissions(user_id: str) -> list[dict]:
    """Get conversations with their submission status for a user."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT c.*, s.id as submission_id, s.status as submission_status, s.score, s.feedback
            FROM chat_conversations c
            LEFT JOIN chat_submissions s ON c.id = s.conversation_id
            WHERE c.user_id = ?
            ORDER BY c.updated_at DESC
        """, (user_id,))
        return [dict(row) for row in cursor.fetchall()]


# ============== Task Template Operations ==============

def list_task_templates(role: Optional[str] = None) -> list[dict]:
    with get_db() as conn:
        cursor = conn.cursor()
        if role:
            cursor.execute("SELECT * FROM task_templates WHERE role = ? ORDER BY sort_order", (role,))
        else:
            cursor.execute("SELECT * FROM task_templates ORDER BY role, sort_order")
        return [dict(row) for row in cursor.fetchall()]


def get_task_template(template_id: str) -> Optional[dict]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM task_templates WHERE id = ?", (template_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
