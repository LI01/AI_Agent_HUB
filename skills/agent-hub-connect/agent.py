#!/usr/bin/env python3
"""
Agent Hub Connect Skill for OpenCode

Usage:
    from agent import connect_to_hub
    connect_to_hub(hub_url="http://10.9.0.10:8080", agent_id="opencode-machine-1")
"""

import json
import socket
import uuid
import os
import sys
import threading
import time
from typing import Optional, Callable

# Try to import websocket, install if needed
try:
    import websocket
except ImportError:
    print("Installing websocket-client...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "websocket-client", "-q"])
    import websocket


class AgentHubSkill:
    """Skill to connect OpenCode to the Agent Hub."""

    def __init__(
        self,
        hub_url: str,
        agent_id: Optional[str] = None,
        capabilities: list[str] = None,
        auth_token: Optional[str] = None,
        task_handler: Optional[Callable] = None
    ):
        self.hub_url = hub_url.rstrip("/").replace("http", "ws")
        self.agent_id = agent_id or f"opencode-{socket.gethostname()}"
        self.capabilities = capabilities or ["code", "search", "bash", "read", "write"]
        self.auth_token = auth_token
        self.task_handler = task_handler
        self._ws = None
        self._running = False
        self._connected = False

    def _on_message(self, ws, message):
        """Handle incoming message from hub."""
        try:
            msg = json.loads(message)
            msg_type = msg.get("type")

            if msg_type == "registered":
                print(f"[agent-hub] Registered as {self.agent_id}")
                self._connected = True

            elif msg_type == "task":
                task = msg.get("task", {})
                task_id = task.get("task_id")
                print(f"[agent-hub] Received task: {task_id}")

                # Send running status
                ws.send(json.dumps({
                    "type": "status",
                    "task_id": task_id,
                    "status": "running"
                }))

                # Execute task
                if self.task_handler:
                    try:
                        result = self.task_handler(task)
                        ws.send(json.dumps({
                            "type": "result",
                            "task_id": task_id,
                            "status": "completed",
                            "result": result
                        }))
                    except Exception as e:
                        ws.send(json.dumps({
                            "type": "result",
                            "task_id": task_id,
                            "status": "failed",
                            "result": {"error": str(e)}
                        }))
                else:
                    ws.send(json.dumps({
                        "type": "result",
                        "task_id": task_id,
                        "status": "failed",
                        "result": {"error": "No task handler configured"}
                    }))

            elif msg_type == "ping":
                ws.send(json.dumps({"type": "pong"}))

            elif msg_type == "echo":
                # Echo test
                print(f"[agent-hub] Echo: {msg.get('message')}")

        except json.JSONDecodeError:
            print(f"[agent-hub] Invalid JSON: {message}")

    def _on_error(self, ws, error):
        print(f"[agent-hub] Error: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        print(f"[agent-hub] Disconnected: {close_status_code} - {close_msg}")
        self._connected = False
        self._running = False

    def _on_open(self, ws):
        """Send registration on connect."""
        print(f"[agent-hub] Connecting to hub...")
        ws.send(json.dumps({
            "type": "register",
            "agent_id": self.agent_id,
            "capabilities": self.capabilities,
            "auth_token": self.auth_token
        }))

    def connect(self) -> bool:
        """Connect to hub and register."""
        try:
            self._ws = websocket.WebSocketApp(
                f"{self.hub_url}/ws",
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
                on_open=self._on_open
            )

            self._running = True
            self._ws.run_forever(ping_interval=30)
            return True

        except Exception as e:
            print(f"[agent-hub] Connection failed: {e}")
            return False

    def send_log(self, task_id: str, log: str):
        """Send log message to hub."""
        if self._ws and self._connected:
            self._ws.send(json.dumps({
                "type": "log",
                "task_id": task_id,
                "log": log
            }))

    def send_result(self, task_id: str, status: str, result: dict = None):
        """Send task result to hub."""
        if self._ws and self._connected:
            self._ws.send(json.dumps({
                "type": "result",
                "task_id": task_id,
                "status": status,
                "result": result or {}
            }))

    def stop(self):
        """Disconnect from hub."""
        self._running = False
        if self._ws:
            self._ws.close()


# Default task handler that can be overridden
def default_task_handler(task: dict) -> dict:
    """Default task handler - logs task and returns mock result."""
    print(f"[agent-hub] Handling task: {task}")
    return {
        "status": "completed",
        "message": "Task received",
        "task": task
    }


# Connection instance (singleton)
_hub_connection: Optional[AgentHubSkill] = None


def connect_to_hub(
    hub_url: str,
    agent_id: str = None,
    capabilities: list[str] = None,
    auth_token: str = None,
    task_handler: Callable = None,
    background: bool = True
) -> AgentHubSkill:
    """
    Connect OpenCode to the Agent Hub.

    Args:
        hub_url: Hub URL (e.g., http://10.9.0.10:8080)
        agent_id: Unique identifier for this agent
        capabilities: List of capabilities
        task_handler: Function to handle tasks
        background: Run connection in background thread

    Returns:
        AgentHubSkill instance
    """
    global _hub_connection

    handler = task_handler or default_task_handler

    _hub_connection = AgentHubSkill(
        hub_url=hub_url,
        agent_id=agent_id,
        capabilities=capabilities,
        auth_token=auth_token,
        task_handler=handler
    )

    if background:
        thread = threading.Thread(target=_hub_connection.connect, daemon=True)
        thread.start()
        print(f"[agent-hub] Started connection to {hub_url} in background")
    else:
        _hub_connection.connect()

    return _hub_connection


def disconnect_from_hub():
    """Disconnect from the hub."""
    global _hub_connection
    if _hub_connection:
        _hub_connection.stop()
        _hub_connection = None
        print("[agent-hub] Disconnected from hub")


def is_connected() -> bool:
    """Check if connected to hub."""
    global _hub_connection
    return _hub_connection and _hub_connection._connected


# CLI entry point
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Agent Hub Connect Skill")
    parser.add_argument("--hub", required=True, help="Hub URL")
    parser.add_argument("--id", help="Agent ID (default: hostname)")
    parser.add_argument("--capabilities", help="Comma-separated capabilities")
    parser.add_argument("--key", help="API key")
    parser.add_argument("--foreground", action="store_true", help="Run in foreground")

    args = parser.parse_args()

    capabilities = args.capabilities.split(",") if args.capabilities else None

    hub = connect_to_hub(
        hub_url=args.hub,
        agent_id=args.id,
        capabilities=capabilities,
        auth_token=args.key,
        background=not args.foreground
    )

    if args.foreground:
        print("Press Ctrl+C to stop")
        try:
            while hub._running:
                time.sleep(1)
        except KeyboardInterrupt:
            hub.stop()
