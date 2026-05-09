#!/usr/bin/env python3
"""Echo Agent - connects to hub via WebSocket and receives tasks."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_sdk import AgentHub


def echo_handler(task: dict) -> dict:
    """Handle tasks - echo back what was sent."""
    payload = task.get("payload", {})
    task_text = payload.get("task", "no task")
    task_id = task.get("task_id")

    print(f"Received task {task_id}: {task_text}")

    return {
        "echoed": task_text,
        "agent": "echo-agent",
        "task_id": task_id,
        "status": "success"
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Echo Agent")
    parser.add_argument("--hub", default="http://localhost:8080", help="Hub URL")
    parser.add_argument("--id", default="echo-agent", help="Agent ID")
    parser.add_argument("--key", help="API key")

    args = parser.parse_args()

    agent = AgentHub(
        hub_url=args.hub,
        agent_id=args.id,
        capabilities=["echo", "test"],
        auth_token=args.key,
        task_handler=echo_handler
    )

    print(f"Starting echo agent: {args.id}")
    print(f"Hub: {args.hub}")

    try:
        agent.start()
    except KeyboardInterrupt:
        agent.stop()
        print("Agent stopped")
