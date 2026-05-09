"""
Access control for Agent Hub.
"""

import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta
from typing import Optional

from pydantic import BaseModel, Field

from . import database as db


class Permission(BaseModel):
    can_register: bool = False
    can_assign_tasks: bool = False
    can_view_agents: bool = True
    can_view_tasks: bool = True
    allowed_agents: list[str] = Field(default_factory=list)
    is_admin: bool = False


class ApiKey(BaseModel):
    key_hash: str
    name: str
    permission: Permission
    created_at: datetime
    expires_at: Optional[datetime] = None
    active: bool = True


class AccessControl:
    """Manages persistent API keys."""

    def create_api_key(
        self,
        name: str,
        permission: Permission = None,
        expires_days: int = None,
        raw_key: str = None,
    ) -> tuple[str, ApiKey]:
        raw_key = raw_key or secrets.token_urlsafe(32)
        key_hash = self.hash_key(raw_key)
        created_at = datetime.now()
        expires_at = created_at + timedelta(days=expires_days) if expires_days else None
        permission = permission or Permission(is_admin=True)

        api_key = ApiKey(
            key_hash=key_hash,
            name=name,
            permission=permission,
            created_at=created_at,
            expires_at=expires_at,
            active=True,
        )
        db.save_api_key({
            "key_hash": api_key.key_hash,
            "name": api_key.name,
            "can_register": permission.can_register,
            "can_assign_tasks": permission.can_assign_tasks,
            "can_view_agents": permission.can_view_agents,
            "can_view_tasks": permission.can_view_tasks,
            "allowed_agents": permission.allowed_agents,
            "is_admin": permission.is_admin,
            "active": api_key.active,
            "created_at": api_key.created_at.isoformat(),
            "expires_at": api_key.expires_at.isoformat() if api_key.expires_at else None,
        })
        db.log_activity("key", name, "created", {"is_admin": permission.is_admin})
        return raw_key, api_key

    @staticmethod
    def hash_key(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode()).hexdigest()

    def validate_key(self, raw_key: str) -> Optional[Permission]:
        if not raw_key:
            return None

        row = db.get_api_key_by_hash(self.hash_key(raw_key))
        if not row or not row.get("active"):
            return None

        expires_at = row.get("expires_at")
        if expires_at and datetime.fromisoformat(expires_at) < datetime.now():
            return None

        return Permission(
            can_register=bool(row.get("can_register")),
            can_assign_tasks=bool(row.get("can_assign_tasks")),
            can_view_agents=bool(row.get("can_view_agents")),
            can_view_tasks=bool(row.get("can_view_tasks")),
            allowed_agents=json.loads(row.get("allowed_agents") or "[]"),
            is_admin=bool(row.get("is_admin")),
        )

    def revoke_key(self, name: str) -> bool:
        revoked = db.revoke_api_key(name)
        if revoked:
            db.log_activity("key", name, "revoked")
        return revoked

    def list_keys(self) -> list[dict]:
        keys = []
        for row in db.list_api_keys():
            keys.append({
                "name": row["name"],
                "permission": {
                    "can_register": bool(row["can_register"]),
                    "can_assign_tasks": bool(row["can_assign_tasks"]),
                    "can_view_agents": bool(row["can_view_agents"]),
                    "can_view_tasks": bool(row["can_view_tasks"]),
                    "allowed_agents": json.loads(row.get("allowed_agents") or "[]"),
                    "is_admin": bool(row["is_admin"]),
                },
                "active": bool(row["active"]),
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
            })
        return keys

    def can_agent_register(self, auth_key: str) -> bool:
        perm = self.validate_key(auth_key)
        return bool(perm and (perm.is_admin or perm.can_register))

    def can_assign_task(self, auth_key: str, target_agent: str, requester: str = "human") -> bool:
        perm = self.validate_key(auth_key)
        if perm and perm.is_admin:
            return True
        if not perm or not perm.can_assign_tasks:
            return False
        if target_agent and perm.allowed_agents and target_agent not in perm.allowed_agents:
            return False
        return True

    def can_view_agents(self, auth_key: str) -> bool:
        perm = self.validate_key(auth_key)
        return bool(perm and (perm.is_admin or perm.can_view_agents))

    def can_view_tasks(self, auth_key: str) -> bool:
        perm = self.validate_key(auth_key)
        return bool(perm and (perm.is_admin or perm.can_view_tasks))

    def ensure_bootstrap_admin(self):
        if db.list_api_keys():
            print(
                "[auth] DB already has API keys; bootstrap skipped. "
                "If you've lost the seed admin key, delete the DB or "
                "set AGENT_HUB_ADMIN_KEY to a known value before next start."
            )
            return

        env_key = os.getenv("AGENT_HUB_ADMIN_KEY")
        raw_key, _ = self.create_api_key(
            "seed-admin",
            Permission(
                can_register=True,
                can_assign_tasks=True,
                can_view_agents=True,
                can_view_tasks=True,
                is_admin=True,
            ),
            raw_key=env_key,
        )
        if env_key:
            print("[auth] Seed admin key loaded from AGENT_HUB_ADMIN_KEY")
        else:
            print("[auth] Generated one-time seed admin key. Store it securely:")
            print(raw_key)


access_control = AccessControl()
access_control.ensure_bootstrap_admin()
