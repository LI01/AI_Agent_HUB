from typing import Optional
import re
from .registry import AgentRegistry
from .queue import TaskQueue
from .models import AgentStatus


# Simple task type detection from natural language
TASK_PATTERNS = {
    "echo": ["echo", "repeat", "test pipe"],
    "camera-test": ["camera", "test camera", "cam test"],
    "project-management": ["task", "project", "manage"],
    "dev": ["build", "compile", "run", "test code"],
}


class Router:
    def __init__(self, registry: AgentRegistry, queue: TaskQueue):
        self._registry = registry
        self._queue = queue

    def route(
        self,
        task_id: str,
        task: str,
        task_type: Optional[str],
        payload: Optional[dict],
        target_agent: Optional[str] = None,
        min_version: int = 1,
    ) -> Optional[str]:
        """
        Route a task to an appropriate agent.
        Returns agent_id if successful, None if no agent available.
        """
        # Detect task type from natural language if not specified
        if not task_type and task:
            task_type = self.detect_task_type(task)

        # If target_agent specified, route only when it is available.
        # The capability-version gate for direct-target lives in main.py
        # (route_and_dispatch) per design §3.2 (Fix F3).
        if target_agent:
            agent = self._registry.get(target_agent)
            if agent and agent.status == AgentStatus.IDLE:
                self._queue.assign(task_id, target_agent)
                return target_agent
            return None

        # Otherwise, find an available agent with matching capability + version.
        if task_type:
            agent = self._registry.find_available(task_type, min_version=min_version or 1)
        else:
            # Fall back to any available agent
            agent = self._registry.find_available("", min_version=min_version or 1)

        if agent:
            self._queue.assign(task_id, agent.agent_id)
            return agent.agent_id

        return None

    def detect_task_type(self, task: str) -> str:
        task_lower = task.lower()
        for task_type, patterns in TASK_PATTERNS.items():
            for pattern in patterns:
                if pattern in task_lower:
                    return task_type
        return "general"
