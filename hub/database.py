"""
SQLite persistence for Agent Hub.
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Optional


DB_PATH = os.getenv("AGENT_HUB_DB_PATH", "./agent_hub.db")


def _now() -> str:
    return datetime.now().isoformat()


def _to_json(value, default):
    if value is None:
        value = default
    return json.dumps(value)


def _from_json(value, default):
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _ensure_column(cursor: sqlite3.Cursor, table: str, column: str, definition: str):
    cursor.execute(f"PRAGMA table_info({table})")
    columns = {row[1] for row in cursor.fetchall()}
    if column not in columns:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    """Initialize database tables and indexes."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS agents (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                skills TEXT,
                position TEXT,
                owner TEXT,
                status TEXT DEFAULT 'offline',
                version TEXT,
                tags TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_seen_at TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                status TEXT DEFAULT 'queued',
                task_type TEXT,
                payload TEXT,
                result TEXT,
                logs TEXT,
                target_agent TEXT,
                assigned_agent_id TEXT,
                requested_by TEXT,
                priority INTEGER DEFAULT 0,
                timeout INTEGER DEFAULT 300,
                parent_task_id TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                FOREIGN KEY (assigned_agent_id) REFERENCES agents(id),
                FOREIGN KEY (parent_task_id) REFERENCES tasks(id)
            )
        """)
        _ensure_column(cursor, "tasks", "target_agent", "TEXT")
        _ensure_column(cursor, "tasks", "min_version", "INTEGER NOT NULL DEFAULT 1")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                key_hash TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                can_register INTEGER NOT NULL DEFAULT 0,
                can_assign_tasks INTEGER NOT NULL DEFAULT 0,
                can_view_agents INTEGER NOT NULL DEFAULT 1,
                can_view_tasks INTEGER NOT NULL DEFAULT 1,
                allowed_agents TEXT,
                is_admin INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                expires_at TEXT
            )
        """)
        _ensure_column(cursor, "api_keys", "owner", "TEXT")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS agent_groups (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                created_at TEXT NOT NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS agent_group_members (
                agent_id TEXT,
                group_id TEXT,
                PRIMARY KEY (agent_id, group_id),
                FOREIGN KEY (agent_id) REFERENCES agents(id),
                FOREIGN KEY (group_id) REFERENCES agent_groups(id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS task_templates (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                task_type TEXT NOT NULL,
                payload_schema TEXT,
                default_timeout INTEGER DEFAULT 300,
                created_at TEXT NOT NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                entity_type TEXT,
                entity_id TEXT,
                action TEXT,
                details TEXT
            )
        """)

        # phase user-tokens §2.2: per-task usage attribution.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS task_usage (
                task_id           TEXT PRIMARY KEY,
                parent_task_id    TEXT,
                api_key_name      TEXT,
                owner_email       TEXT,
                agent_id          TEXT,
                cli               TEXT,
                task_type         TEXT,
                status            TEXT NOT NULL,
                started_at        TEXT,
                completed_at      TEXT NOT NULL,
                duration_seconds  REAL,
                cost_usd          REAL,
                input_tokens      INTEGER,
                output_tokens     INTEGER,
                web_search_requests INTEGER,
                created_at        TEXT NOT NULL
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_task_usage_owner ON task_usage(owner_email)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_task_usage_agent ON task_usage(agent_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_task_usage_completed ON task_usage(completed_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_task_usage_parent ON task_usage(parent_task_id)")

        # phase user-tokens §2.3: restart-safe submitter attribution.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS task_submitters (
                task_id       TEXT PRIMARY KEY,
                api_key_name  TEXT,
                owner_email   TEXT,
                created_at    TEXT NOT NULL
            )
        """)

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_agent ON tasks(assigned_agent_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_activity_entity ON activity_log(entity_type, entity_id)")

        # Chat training platform tables
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS chat_users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                name TEXT,
                avatar_url TEXT,
                role TEXT,
                group_number INTEGER,
                is_admin INTEGER DEFAULT 0,
                is_instructor INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                last_seen_at TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS chat_conversations (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES chat_users(id),
                title TEXT,
                agent_id TEXT NOT NULL,
                task_template_id TEXT,
                status TEXT DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES chat_conversations(id),
                role TEXT NOT NULL,
                content TEXT,
                files TEXT,
                task_id TEXT,
                created_at TEXT NOT NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS chat_submissions (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES chat_conversations(id),
                user_id TEXT NOT NULL REFERENCES chat_users(id),
                submitted_at TEXT NOT NULL,
                score INTEGER,
                feedback TEXT,
                status TEXT DEFAULT 'submitted'
            )
        """)

        # Extend existing task_templates for training use
        _ensure_column(cursor, "task_templates", "role", "TEXT")
        _ensure_column(cursor, "task_templates", "expected_output", "TEXT")
        _ensure_column(cursor, "task_templates", "skill_template", "TEXT")
        _ensure_column(cursor, "task_templates", "time_limit_minutes", "INTEGER")
        _ensure_column(cursor, "task_templates", "sort_order", "INTEGER DEFAULT 0")

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_conv_user ON chat_conversations(user_id, updated_at DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_msg_conv ON chat_messages(conversation_id, created_at ASC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_msg_task ON chat_messages(task_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_sub_user ON chat_submissions(user_id, status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_tpl_role ON task_templates(role, sort_order)")

        conn.commit()
    print(f"[db] Database initialized: {DB_PATH}")


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ============== Agent Operations ==============


def save_agent(agent_data: dict):
    """Save or update an agent."""
    now = _now()
    # F5 (design v2 §2.2): normalize skills to JSONable dicts before _to_json,
    # since _to_json delegates to json.dumps which cannot serialize Pydantic
    # `Capability` instances. Single chokepoint shared by REST/WS/MCP register.
    raw_skills = agent_data.get("skills", agent_data.get("capabilities")) or []
    if any(not isinstance(item, str) for item in raw_skills):
        # Lazy import: avoids circular-import risk and lets existing all-string
        # callers keep working even before Unit A's hub/capabilities.py lands.
        from hub.capabilities import capability_to_dict, normalize_capability
        skills_jsonable = [
            item if isinstance(item, str) else capability_to_dict(normalize_capability(item))
            for item in raw_skills
        ]
    else:
        skills_jsonable = list(raw_skills)
    with get_db() as conn:
        cursor = conn.cursor()
        existing = get_agent(agent_data["id"])
        created_at = agent_data.get("created_at") or (existing or {}).get("created_at") or now
        cursor.execute("""
            INSERT OR REPLACE INTO agents
            (id, name, description, skills, position, owner, status, version, tags,
             created_at, updated_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            agent_data["id"],
            agent_data.get("name") or agent_data["id"],
            agent_data.get("description", ""),
            _to_json(skills_jsonable, []),
            _to_json(agent_data.get("position", agent_data.get("machine_info")), {}),
            agent_data.get("owner", "system"),
            agent_data.get("status", "offline"),
            agent_data.get("version", ""),
            _to_json(agent_data.get("tags"), []),
            created_at,
            now,
            agent_data.get("last_seen_at", now),
        ))
        conn.commit()


def get_agent(agent_id: str) -> Optional[dict]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM agents WHERE id = ?", (agent_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def list_agents(status: str = None) -> list[dict]:
    with get_db() as conn:
        cursor = conn.cursor()
        if status:
            cursor.execute("SELECT * FROM agents WHERE status = ? ORDER BY id", (status,))
        else:
            cursor.execute("SELECT * FROM agents ORDER BY id")
        return [dict(row) for row in cursor.fetchall()]


def update_agent_status(agent_id: str, status: str):
    now = _now()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE agents SET status = ?, updated_at = ?, last_seen_at = ? WHERE id = ?",
            (status, now, now, agent_id),
        )
        conn.commit()


def delete_agent(agent_id: str):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        conn.commit()


# ============== Task Operations ==============


def save_task(task_data: dict):
    """Save or update a task."""
    now = _now()
    with get_db() as conn:
        cursor = conn.cursor()
        existing = get_task(task_data["id"])
        created_at = task_data.get("created_at") or (existing or {}).get("created_at") or now
        cursor.execute("""
            INSERT OR REPLACE INTO tasks
            (id, name, description, status, task_type, payload, result, logs,
             target_agent, assigned_agent_id, requested_by, priority, timeout,
             min_version, parent_task_id, created_at, started_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task_data["id"],
            task_data.get("name") or f"Task {task_data['id'][:8]}",
            task_data.get("description", ""),
            task_data.get("status", "queued"),
            task_data.get("task_type", "general"),
            _to_json(task_data.get("payload"), {}),
            _to_json(task_data.get("result"), None) if task_data.get("result") is not None else None,
            _to_json(task_data.get("logs"), []),
            task_data.get("target_agent"),
            task_data.get("assigned_agent_id"),
            task_data.get("requested_by", task_data.get("submitted_by", "human")),
            int(task_data.get("priority", 0)),
            int(task_data.get("timeout", 300)),
            int(task_data.get("min_version", 1)),
            task_data.get("parent_task_id"),
            created_at,
            task_data.get("started_at"),
            task_data.get("completed_at"),
        ))
        conn.commit()


def get_task(task_id: str) -> Optional[dict]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def list_tasks(
    status: str = None,
    agent_id: str = None,
    parent_task_id: Optional[str] = None,
    top_level: bool = False,
) -> list[dict]:
    if parent_task_id is not None and top_level:
        raise ValueError("parent_task_id and top_level are mutually exclusive")
    clauses: list[str] = []
    params: list = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if agent_id:
        clauses.append("assigned_agent_id = ?")
        params.append(agent_id)
    if parent_task_id is not None:
        clauses.append("parent_task_id = ?")
        params.append(parent_task_id)
    if top_level:
        clauses.append("parent_task_id IS NULL")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT * FROM tasks{where} ORDER BY priority DESC, created_at" if clauses else "SELECT * FROM tasks ORDER BY created_at"
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]


def get_parent_task_id(task_id: str) -> Optional[str]:
    """Cheap lookup used by task_event_data to populate parent_task_id in SSE payloads."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT parent_task_id FROM tasks WHERE id = ?", (task_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        return row[0]


def update_task_status(task_id: str, status: str, result: dict = None, logs: list[str] = None):
    now = _now()
    with get_db() as conn:
        cursor = conn.cursor()
        task = get_task(task_id)
        if not task:
            return False
        if task.get("status") in ("completed", "failed", "timeout"):
            log_activity(
                "task",
                task_id,
                "terminal_update_rejected",
                {"current_status": task.get("status"), "requested_status": status},
            )
            return False

        current_logs = _from_json(task.get("logs"), [])
        if logs:
            current_logs.extend(logs)

        result_json = task.get("result")
        if result is not None:
            result_json = json.dumps(result)

        if status in ("completed", "failed", "timeout"):
            cursor.execute(
                "UPDATE tasks SET status = ?, result = ?, logs = ?, completed_at = ? WHERE id = ?",
                (status, result_json, json.dumps(current_logs), now, task_id),
            )
        elif status == "running":
            started_at = task.get("started_at") or now
            cursor.execute(
                "UPDATE tasks SET status = ?, result = ?, logs = ?, started_at = ? WHERE id = ?",
                (status, result_json, json.dumps(current_logs), started_at, task_id),
            )
        else:
            cursor.execute(
                "UPDATE tasks SET status = ?, result = ?, logs = ? WHERE id = ?",
                (status, result_json, json.dumps(current_logs), task_id),
            )
        conn.commit()
        return True


def assign_task(task_id: str, agent_id: str) -> bool:
    now = _now()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE tasks SET status = 'assigned', assigned_agent_id = ?, started_at = ? WHERE id = ?",
            (agent_id, now, task_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def requeue_tasks_for_agent(agent_id: str) -> int:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE tasks
            SET status = 'queued', assigned_agent_id = NULL, started_at = NULL
            WHERE assigned_agent_id = ? AND status IN ('assigned', 'running')
        """, (agent_id,))
        conn.commit()
        return cursor.rowcount


def add_task_log(task_id: str, log: str) -> bool:
    task = get_task(task_id)
    if not task:
        return False
    logs = _from_json(task.get("logs"), [])
    logs.append(log)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE tasks SET logs = ? WHERE id = ?", (json.dumps(logs), task_id))
        conn.commit()
    return True


def parse_task_row(row: dict) -> dict:
    return {
        "task_id": row["id"],
        "task": row.get("description") or row.get("name", ""),
        "submitted_by": row.get("requested_by") or "human",
        "target_agent": row.get("target_agent"),
        "assigned_agent_id": row.get("assigned_agent_id"),
        "task_type": row.get("task_type") or "general",
        "payload": _from_json(row.get("payload"), {}),
        "status": row.get("status") or "queued",
        "created_at": row.get("created_at"),
        "started_at": row.get("started_at"),
        "completed_at": row.get("completed_at"),
        "result": _from_json(row.get("result"), None),
        "logs": _from_json(row.get("logs"), []),
        "priority": row.get("priority") or 0,
        "timeout": row.get("timeout") or 300,
        "min_version": row.get("min_version") or 1,
        "parent_task_id": row.get("parent_task_id"),
    }


# ============== API Key Operations ==============


def save_api_key(api_key: dict, owner: Optional[str] = None):
    """Persist (or replace) an api_keys row.

    `owner` may be passed positionally on the dict (`api_key["owner"]`) or as a
    kwarg; the kwarg wins when both are present and non-None. NULL is the
    pre-phase default (admin / agent / MCP / seed keys).
    """
    resolved_owner = owner if owner is not None else api_key.get("owner")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO api_keys
            (key_hash, name, can_register, can_assign_tasks, can_view_agents,
             can_view_tasks, allowed_agents, is_admin, active, created_at,
             expires_at, owner)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            api_key["key_hash"],
            api_key["name"],
            int(api_key.get("can_register", False)),
            int(api_key.get("can_assign_tasks", False)),
            int(api_key.get("can_view_agents", True)),
            int(api_key.get("can_view_tasks", True)),
            json.dumps(api_key.get("allowed_agents", [])),
            int(api_key.get("is_admin", False)),
            int(api_key.get("active", True)),
            api_key.get("created_at") or _now(),
            api_key.get("expires_at"),
            resolved_owner,
        ))
        conn.commit()


def list_api_keys() -> list[dict]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM api_keys ORDER BY created_at")
        return [dict(row) for row in cursor.fetchall()]


def get_api_key_by_hash(key_hash: str) -> Optional[dict]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM api_keys WHERE key_hash = ?", (key_hash,))
        row = cursor.fetchone()
        return dict(row) if row else None


def revoke_api_key(name: str) -> bool:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE api_keys SET active = 0 WHERE name = ?", (name,))
        conn.commit()
        return cursor.rowcount > 0


# ============== Agent Groups ==============


def create_group(name: str, description: str = "") -> str:
    import uuid

    group_id = str(uuid.uuid4())
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO agent_groups (id, name, description, created_at) VALUES (?, ?, ?, ?)",
            (group_id, name, description, _now()),
        )
        conn.commit()
    return group_id


def add_agent_to_group(agent_id: str, group_id: str):
    with get_db() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO agent_group_members (agent_id, group_id) VALUES (?, ?)",
                (agent_id, group_id),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            pass


def list_groups() -> list[dict]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM agent_groups")
        return [dict(row) for row in cursor.fetchall()]


# ============== Activity Log ==============


def log_activity(entity_type: str, entity_id: str, action: str, details: dict = None):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO activity_log (timestamp, entity_type, entity_id, action, details) VALUES (?, ?, ?, ?, ?)",
            (_now(), entity_type, entity_id, action, json.dumps(details or {})),
        )
        conn.commit()


# ============== Stats ==============


def get_stats() -> dict:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM agents WHERE status != 'offline'")
        online = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM agents")
        total_agents = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM tasks WHERE status = 'queued'")
        queued = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM tasks WHERE status = 'assigned'")
        assigned = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM tasks WHERE status = 'running'")
        running = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM tasks WHERE status = 'completed'")
        completed = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM tasks WHERE status = 'failed'")
        failed = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM tasks WHERE status = 'timeout'")
        timeout = cursor.fetchone()[0]
        return {
            "agents": {"online": online, "total": total_agents},
            "tasks": {
                "queued": queued,
                "assigned": assigned,
                "running": running,
                "completed": completed,
                "failed": failed,
                "timeout": timeout,
            },
        }


# ============== Task Usage (phase user-tokens §2.2) ==============


_TASK_USAGE_COLUMNS = (
    "task_id",
    "parent_task_id",
    "api_key_name",
    "owner_email",
    "agent_id",
    "cli",
    "task_type",
    "status",
    "started_at",
    "completed_at",
    "duration_seconds",
    "cost_usd",
    "input_tokens",
    "output_tokens",
    "web_search_requests",
    "created_at",
)


def save_task_usage(row: dict) -> None:
    """Insert one task_usage row, no-op on duplicate task_id (idempotent re-finalize)."""
    values = [row.get(c) for c in _TASK_USAGE_COLUMNS]
    # status and completed_at are NOT NULL; fill safe defaults if caller omitted.
    status_idx = _TASK_USAGE_COLUMNS.index("status")
    if not values[status_idx]:
        values[status_idx] = "completed"
    completed_idx = _TASK_USAGE_COLUMNS.index("completed_at")
    if not values[completed_idx]:
        values[completed_idx] = _now()
    created_idx = _TASK_USAGE_COLUMNS.index("created_at")
    if not values[created_idx]:
        values[created_idx] = _now()
    placeholders = ", ".join("?" for _ in _TASK_USAGE_COLUMNS)
    columns = ", ".join(_TASK_USAGE_COLUMNS)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"INSERT OR IGNORE INTO task_usage ({columns}) VALUES ({placeholders})",
            values,
        )
        conn.commit()


def list_task_usage(
    owner: Optional[str] = None,
    agent: Optional[str] = None,
    from_dt: Optional[str] = None,
    to_dt: Optional[str] = None,
    email: Optional[str] = None,
    top_level_only: bool = False,
    limit: int = 1000,
) -> list[dict]:
    """List task_usage rows, ordered by completed_at DESC.

    `owner` and `email` are aliases for filtering on `owner_email`; `email`
    wins if both are provided.
    """
    clauses: list[str] = []
    params: list = []
    effective_owner = email if email else owner
    if effective_owner:
        clauses.append("owner_email = ?")
        params.append(effective_owner)
    if agent:
        clauses.append("agent_id = ?")
        params.append(agent)
    if from_dt:
        clauses.append("completed_at >= ?")
        params.append(from_dt)
    if to_dt:
        clauses.append("completed_at <= ?")
        params.append(to_dt)
    if top_level_only:
        clauses.append("parent_task_id IS NULL")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = 1000
    n = max(1, min(1000, n))
    sql = f"SELECT * FROM task_usage{where} ORDER BY completed_at DESC LIMIT ?"
    params.append(n)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]


# ============== Task Submitters (phase user-tokens §2.3) ==============


def record_submitter(
    task_id: str,
    api_key_name: Optional[str],
    owner_email: Optional[str],
) -> None:
    """INSERT OR REPLACE — used by submit_task and submit_child (parent->child propagation)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO task_submitters (task_id, api_key_name, owner_email, created_at) VALUES (?, ?, ?, ?)",
            (task_id, api_key_name, owner_email, _now()),
        )
        conn.commit()


def peek_submitter(task_id: str) -> Optional[dict]:
    """Read submitter row without deleting. Returns None if missing."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT api_key_name, owner_email FROM task_submitters WHERE task_id = ?",
            (task_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def pop_submitter(task_id: str) -> Optional[dict]:
    """Read-and-delete in a single transaction. Idempotent: returns None on missing rows.

    Called from finalize_task and from `_rollback_child` (design §3.8) — never
    raises so it is safe to call unconditionally during rollback cleanup.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT api_key_name, owner_email FROM task_submitters WHERE task_id = ?",
            (task_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        result = dict(row)
        cursor.execute("DELETE FROM task_submitters WHERE task_id = ?", (task_id,))
        conn.commit()
        return result


init_db()
