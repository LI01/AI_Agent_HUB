#!/usr/bin/env python3
"""
Base Agent Client for Agent Hub
Shared functionality for all agents
"""

import json
import time
import threading
from typing import Optional, Callable, Any

import requests


class BaseAgentClient:
    """Base class for agents connecting to the hub."""

    def __init__(
        self,
        hub_url: str,
        agent_id: str,
        capabilities: list[str],
        auth_token: str = None,
        task_handler: Callable = None
    ):
        self.hub_url = hub_url.rstrip("/").replace("http", "ws")
        self.agent_id = agent_id
        self.capabilities = capabilities
        self.auth_token = auth_token
        self.task_handler = task_handler
        self._running = False
        self._ws = None
        self._executed_tasks: set[str] = set()

    def log(self, message: str):
        """Log message with timestamp."""
        print(f"[{self.agent_id}] {message}")

    def _connect(self) -> bool:
        """Connect to hub - override in subclass."""
        raise NotImplementedError

    def _on_message(self, msg: dict):
        """Handle incoming message."""
        msg_type = msg.get("type")

        if msg_type == "registered":
            self.log("Registered successfully")

        elif msg_type == "task":
            task = msg.get("task", {})
            self._execute_task(task)

        elif msg_type == "ping":
            self._send({"type": "pong"})

    def _execute_task(self, task: dict):
        """Execute a task and report result."""
        task_id = task.get("task_id")
        self.log(f"Executing task: {task_id}")

        try:
            if self.task_handler:
                result = self.task_handler(task)
                self._send_result(task_id, "completed", result)
            else:
                self._send_result(task_id, "failed", {"error": "No handler"})
        except Exception as e:
            self._send_result(task_id, "failed", {"error": str(e)})

    def _send(self, msg: dict):
        """Send message to hub - override in subclass."""
        raise NotImplementedError

    def _send_result(self, task_id: str, status: str, result: dict = None):
        """Send task result."""
        self._send({
            "type": "result",
            "task_id": task_id,
            "status": status,
            "result": result or {}
        })

    def _send_log(self, task_id: str, log: str):
        """Send log message."""
        self._send({
            "type": "log",
            "task_id": task_id,
            "log": log
        })

    def start(self):
        """Start the agent."""
        self.log(f"Starting agent...")
        self._running = True
        self._connect()

    def stop(self):
        """Stop the agent."""
        self._running = False
        self.log("Agent stopped")


class HTTPAgentClient(BaseAgentClient):
    """HTTP-based agent client (fallback when WebSocket unavailable)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.hub_url_http = self.hub_url.replace("ws", "http")
        self._poll_thread = None

    def _connect(self):
        """Register via HTTP and start polling."""
        try:
            resp = requests.post(
                f"{self.hub_url_http}/register",
                json={
                    "agent_id": self.agent_id,
                    "capabilities": self.capabilities
                },
                headers=self._auth_header()
            )

            if resp.status_code == 200:
                self.log("Registered via HTTP")
                self._start_polling()
                return True
            else:
                self.log(f"Registration failed: {resp.text}")
                return False

        except Exception as e:
            self.log(f"Connection failed: {e}")
            return False

    def _auth_header(self):
        """Get auth header."""
        if self.auth_token:
            return {"Authorization": f"Bearer {self.auth_token}"}
        return {}

    def _start_polling(self):
        """Poll for tasks."""
        def poll():
            while self._running:
                try:
                    resp = requests.get(
                        f"{self.hub_url_http}/tasks",
                        headers=self._auth_header()
                    )
                    if resp.status_code == 200:
                        tasks = resp.json()
                        for task in tasks:
                            assigned_to_me = (
                                task.get("assigned_agent_id") == self.agent_id
                                or task.get("target_agent") == self.agent_id
                            )
                            task_id = task.get("task_id") or task.get("id")
                            if assigned_to_me and task_id not in self._executed_tasks:
                                if task.get("status") in ("assigned", "running"):
                                    self._executed_tasks.add(task_id)
                                    self._execute_task(task)

                    # Also send heartbeat
                    requests.post(
                        f"{self.hub_url_http}/heartbeat",
                        json={"agent_id": self.agent_id, "status": "idle"},
                        headers=self._auth_header()
                    )

                except Exception as e:
                    self.log(f"Poll error: {e}")

                time.sleep(2)

        self._poll_thread = threading.Thread(target=poll, daemon=True)
        self._poll_thread.start()

    def _send(self, msg: dict):
        """Send result/log/status messages over HTTP fallback."""
        msg_type = msg.get("type")
        if msg_type == "result":
            requests.post(
                f"{self.hub_url_http}/report",
                json={
                    "task_id": msg.get("task_id"),
                    "agent_id": self.agent_id,
                    "status": msg.get("status", "completed"),
                    "result": msg.get("result", {}),
                    "logs": msg.get("logs", []),
                },
                headers=self._auth_header(),
            )
        elif msg_type == "log":
            requests.post(
                f"{self.hub_url_http}/report",
                json={
                    "task_id": msg.get("task_id"),
                    "agent_id": self.agent_id,
                    "status": "running",
                    "result": None,
                    "logs": [msg.get("log", "")],
                },
                headers=self._auth_header(),
            )
        elif msg_type == "pong":
            return


def register_agent(hub_url: str, agent_id: str, capabilities: list, auth_token: str = None) -> bool:
    """Simple registration function."""
    import requests

    try:
        headers = {}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"

        resp = requests.post(
            f"{hub_url}/register",
            json={"agent_id": agent_id, "capabilities": capabilities},
            headers=headers
        )
        return resp.status_code == 200
    except Exception as e:
        print(f"Registration failed: {e}")
        return False
