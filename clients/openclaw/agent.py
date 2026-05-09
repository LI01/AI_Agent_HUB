#!/usr/bin/env python3
"""
OpenClaw Agent Client
Connect OpenClaw to the Agent Hub
"""

import sys
import os
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Use the existing agent SDK
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agent_sdk.client import AgentHub


def openclaw_handler(task: dict) -> dict:
    """
    Handle tasks using OpenClaw capabilities.
    """
    task_id = task.get("task_id")
    payload = task.get("payload", {})
    task_text = payload.get("task", "")

    print(f"[OpenClaw] Executing: {task_text}")

    # OpenClaw-specific logic:
    # - Use OpenClaw tools: glob, grep, read, write, bash, etc.
    # - Execute tasks using available skills
    # - Return results

    # Example:
    # from openclaw import tools
    # result = tools.bash.run_command(task_text)

    return {
        "agent": "openclaw",
        "task_id": task_id,
        "status": "completed",
        "message": "Task executed by OpenClaw"
    }


class OpenClawAgent(AgentHub):
    """OpenClaw-specific agent."""

    def __init__(self, hub_url: str, agent_id: str, auth_token: str = None):
        super().__init__(
            hub_url=hub_url,
            agent_id=agent_id,
            capabilities=["code", "glob", "grep", "read", "write", "bash", "edit"],
            task_handler=openclaw_handler
        )
        if auth_token:
            self.auth_token = auth_token


def main():
    import argparse

    parser = argparse.ArgumentParser(description="OpenClaw Agent Client")
    parser.add_argument("--hub", required=True, help="Hub URL")
    parser.add_argument("--id", default="openclaw-agent", help="Agent ID")
    parser.add_argument("--key", help="API key")

    args = parser.parse_args()

    # Using HTTP fallback (WebSocket needs proper library)
    import requests

    # Register first
    resp = requests.post(
        f"{args.hub}/register",
        json={
            "agent_id": args.id,
            "capabilities": ["code", "glob", "grep", "read", "write", "bash", "edit"]
        },
        headers={"Authorization": f"Bearer {args.key}"} if args.key else {}
    )

    print(f"Registered: {resp.json()}")

    # Then poll for tasks
    from base import HTTPAgentClient
    from agent import openclaw_handler

    client = HTTPAgentClient(
        hub_url=args.hub,
        agent_id=args.id,
        capabilities=["code", "glob", "grep", "read", "write", "bash", "edit"],
        auth_token=args.key,
        task_handler=openclaw_handler
    )

    print(f"Starting OpenClaw agent: {args.id}")
    client.start()

    try:
        import time
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        client.stop()


if __name__ == "__main__":
    main()
