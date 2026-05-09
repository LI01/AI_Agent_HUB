#!/usr/bin/env python3
"""
Codex Agent Client
Connects Codex to the Agent Hub
"""

import sys
import os

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from base import BaseAgentClient, HTTPAgentClient


def codex_handler(task: dict) -> dict:
    """
    Handle tasks using Codex.
    This would need to call codex CLI or API.
    """
    import subprocess
    import json

    task_id = task.get("task_id")
    payload = task.get("payload", {})
    task_text = payload.get("task", "")

    print(f"[codex] Handling task: {task_text}")

    # Example: call codex CLI
    # result = subprocess.run(
    #     ["codex", "exec", "--prompt", task_text],
    #     capture_output=True,
    #     text=True
    # )
    # return {"output": result.stdout, "error": result.stderr}

    return {
        "agent": "codex",
        "task_id": task_id,
        "status": "executed",
        "message": "Codex executed task"
    }


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Codex Agent Client")
    parser.add_argument("--hub", required=True, help="Hub URL")
    parser.add_argument("--id", default="codex-agent", help="Agent ID")
    parser.add_argument("--key", help="API key")
    parser.add_argument("--http", action="store_true", help="Use HTTP fallback")

    args = parser.parse_args()

    if args.http:
        client = HTTPAgentClient(
            hub_url=args.hub,
            agent_id=args.id,
            capabilities=["code", "review", "refactor", "test"],
            auth_token=args.key,
            task_handler=codex_handler
        )
    else:
        # WebSocket version would need proper websocket library
        # For now, use HTTP
        client = HTTPAgentClient(
            hub_url=args.hub,
            agent_id=args.id,
            capabilities=["code", "review", "refactor", "test"],
            auth_token=args.key,
            task_handler=codex_handler
        )

    print(f"Starting Codex agent: {args.id}")
    client.start()

    try:
        import time
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        client.stop()


if __name__ == "__main__":
    main()