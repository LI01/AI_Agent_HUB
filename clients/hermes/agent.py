#!/usr/bin/env python3
"""
Hermes Agent Client
Connect any AI agent to the Agent Hub
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from base import BaseAgentClient, HTTPAgentClient


class HermesAgentClient(HTTPAgentClient):
    """Hermes-specific agent client."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.agent_type = "hermes"

    def log(self, message: str):
        print(f"[Hermes:{self.agent_id}] {message}")


def hermes_handler(task: dict) -> dict:
    """
    Handle tasks using Hermes.
    Replace with actual Hermes integration.
    """
    task_id = task.get("task_id")
    payload = task.get("payload", {})
    task_text = payload.get("task", "")

    print(f"[Hermes] Executing: {task_text}")

    # Your Hermes-specific logic here
    # Example: call Hermes API or CLI

    return {
        "agent": "hermes",
        "task_id": task_id,
        "status": "completed",
        "result": "Task executed by Hermes"
    }


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Hermes Agent Client")
    parser.add_argument("--hub", required=True, help="Hub URL")
    parser.add_argument("--id", default="hermes-agent", help="Agent ID")
    parser.add_argument("--key", help="API key")
    parser.add_argument("--type", default="default", help="Hermes type")

    args = parser.parse_args()

    client = HermesAgentClient(
        hub_url=args.hub,
        agent_id=args.id,
        capabilities=["code", "reasoning", "analysis"],
        auth_token=args.key,
        task_handler=hermes_handler
    )

    # Override agent type if specified
    client.agent_type = args.type

    print(f"Starting Hermes agent: {args.id}")
    client.start()

    try:
        import time
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        client.stop()


if __name__ == "__main__":
    main()