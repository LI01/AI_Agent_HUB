#!/usr/bin/env python3
"""Compatibility CLI for Agent Hub MCP tools.

The primary Phase 1.5 integration is `python -m hub.mcp` for MCP stdio and
`POST /mcp` for embedded Streamable HTTP-style JSON-RPC. This file preserves
the old command path as a thin wrapper over the shared MCP tool registry.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from typing import Optional

from hub.mcp_protocol import HttpHubBackend, invoke_tool

HUB_URL = "http://localhost:8080"
AUTH_KEY = None


def set_hub(url: str, auth_key: str = None):
    global HUB_URL, AUTH_KEY
    HUB_URL = url.rstrip("/")
    AUTH_KEY = auth_key


def _backend() -> HttpHubBackend:
    if not AUTH_KEY:
        raise RuntimeError("API key required")
    return HttpHubBackend(HUB_URL, AUTH_KEY)


def _run_tool(name: str, arguments: Optional[dict] = None):
    return asyncio.run(invoke_tool(_backend(), name, arguments or {}))


def list_agents() -> list:
    return _run_tool("list-agents")


def get_agent(agent_id: str) -> dict:
    return _run_tool("get-agent", {"agent_id": agent_id})


def register_agent(
    agent_id: str,
    capabilities: list = None,
    description: str = None,
    name: str = None,
    owner: str = None,
) -> dict:
    args = {"agent_id": agent_id, "capabilities": capabilities or []}
    if description:
        args["description"] = description
    if name:
        args["name"] = name
    if owner:
        args["owner"] = owner
    return _run_tool("register-agent", args)


def unregister_agent(agent_id: str) -> dict:
    return _run_tool("unregister-agent", {"agent_id": agent_id})


def submit_task(
    task: str = None,
    task_type: str = None,
    payload: dict = None,
    target_agent: str = None,
    priority: int = 0,
    timeout: int = 300,
    parent_task_id: str = None,
) -> dict:
    args = {}
    if task:
        args["task"] = task
    if task_type:
        args["task_type"] = task_type
    if payload:
        args["payload"] = payload
    if target_agent:
        args["target_agent"] = target_agent
    if priority:
        args["priority"] = priority
    if timeout != 300:
        args["timeout"] = timeout
    if parent_task_id:
        args["parent_task_id"] = parent_task_id
    return _run_tool("submit-task", args)


def get_task(task_id: str) -> dict:
    return _run_tool("get-task", {"task_id": task_id})


def list_tasks(status: str = None, agent_id: str = None) -> list:
    args = {}
    if status:
        args["status"] = status
    if agent_id:
        args["agent_id"] = agent_id
    return _run_tool("list-tasks", args)


def create_api_key(
    name: str,
    can_register: bool = False,
    can_assign_tasks: bool = False,
    allowed_agents: list = None,
    is_admin: bool = False,
) -> dict:
    args = {
        "name": name,
        "can_register": can_register,
        "can_assign_tasks": can_assign_tasks,
        "is_admin": is_admin,
    }
    if allowed_agents:
        args["allowed_agents"] = allowed_agents
    return _run_tool("create-api-key", args)


def list_api_keys() -> list:
    return _run_tool("list-api-keys")


def revoke_api_key(name: str) -> dict:
    return _run_tool("revoke-api-key", {"name": name})


def get_stats() -> dict:
    return _run_tool("stats")


def health() -> dict:
    return _run_tool("health")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Agent Hub MCP compatibility client")
    parser.add_argument("--hub", default="http://localhost:8080", help="Hub URL")
    parser.add_argument("--key", help="API key")
    parser.add_argument("command", choices=[
        "agents", "tasks", "stats", "health",
        "register", "unregister", "submit", "get-task", "create-key", "list-keys", "revoke-key",
    ])
    parser.add_argument("--agent-id", help="Agent ID")
    parser.add_argument("--capabilities", help="Comma-separated capabilities")
    parser.add_argument("--task", help="Task description")
    parser.add_argument("--task-type", help="Task type")
    parser.add_argument("--target", help="Target agent ID")
    parser.add_argument("--task-id", help="Task ID")
    parser.add_argument("--name", help="Name for key")
    parser.add_argument("--key-register", action="store_true", help="Key can register")
    parser.add_argument("--key-assign", action="store_true", help="Key can assign tasks")
    parser.add_argument("--key-admin", action="store_true", help="Key is admin")

    args = parser.parse_args()
    set_hub(args.hub, args.key)

    if args.command == "agents":
        out = list_agents()
    elif args.command == "tasks":
        out = list_tasks()
    elif args.command == "stats":
        out = get_stats()
    elif args.command == "health":
        out = health()
    elif args.command == "register":
        caps = args.capabilities.split(",") if args.capabilities else []
        out = register_agent(args.agent_id, caps)
    elif args.command == "unregister":
        out = unregister_agent(args.agent_id)
    elif args.command == "submit":
        out = submit_task(args.task, task_type=args.task_type, target_agent=args.target)
    elif args.command == "get-task":
        out = get_task(args.task_id)
    elif args.command == "create-key":
        out = create_api_key(args.name, args.key_register, args.key_assign, is_admin=args.key_admin)
    elif args.command == "list-keys":
        out = list_api_keys()
    elif args.command == "revoke-key":
        out = revoke_api_key(args.name)
    print(json.dumps(out, indent=2))
