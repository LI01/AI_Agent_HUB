from datetime import datetime
from threading import RLock
from typing import Optional

from .models import Task, TaskStatus


class TaskQueue:
    def __init__(self):
        self._tasks: dict[str, Task] = {}
        self._lock = RLock()

    def create(
        self,
        task_id: str,
        submitted_by: str,
        task_type: str,
        payload: dict,
        target_agent: Optional[str] = None,
        task: str = "",
        priority: int = 0,
        timeout: int = 300,
        parent_task_id: Optional[str] = None,
        min_version: int = 1,
    ) -> Task:
        with self._lock:
            new_task = Task(
                task_id=task_id,
                task=task,
                submitted_by=submitted_by,
                target_agent=target_agent,
                task_type=task_type,
                payload=payload,
                status=TaskStatus.QUEUED,
                priority=priority,
                timeout=timeout,
                min_version=min_version,
                parent_task_id=parent_task_id,
            )
            self._tasks[task_id] = new_task
            return new_task

    def get(self, task_id: str) -> Optional[Task]:
        with self._lock:
            return self._tasks.get(task_id)

    def assign(self, task_id: str, agent_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task.status != TaskStatus.QUEUED:
                return False
            task.status = TaskStatus.ASSIGNED
            task.assigned_agent_id = agent_id
            task.started_at = datetime.now()
            return True

    def update_status(self, task_id: str, status: TaskStatus, result: Optional[dict] = None) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            task.status = status
            if result is not None:
                task.result = result
            if status == TaskStatus.RUNNING and not task.started_at:
                task.started_at = datetime.now()
            if status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.TIMEOUT):
                task.completed_at = datetime.now()
            return True

    def add_log(self, task_id: str, log: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            task.logs.append(log)
            return True

    def list_pending(self) -> list[Task]:
        with self._lock:
            pending = [t for t in self._tasks.values() if t.status == TaskStatus.QUEUED]
            return sorted(pending, key=lambda t: (-t.priority, t.created_at))

    def list_by_agent(self, agent_id: str) -> list[Task]:
        with self._lock:
            return [t for t in self._tasks.values() if t.assigned_agent_id == agent_id]

    def list_all(
        self,
        status: Optional[str] = None,
        agent_id: Optional[str] = None,
        parent_task_id: Optional[str] = None,
        top_level: bool = False,
    ) -> list[Task]:
        if parent_task_id is not None and top_level:
            raise ValueError("parent_task_id and top_level are mutually exclusive")
        with self._lock:
            tasks = list(self._tasks.values())
            if status:
                tasks = [t for t in tasks if t.status == status]
            if agent_id:
                tasks = [t for t in tasks if t.assigned_agent_id == agent_id]
            if parent_task_id is not None:
                tasks = [t for t in tasks if t.parent_task_id == parent_task_id]
            if top_level:
                tasks = [t for t in tasks if t.parent_task_id is None]
            return sorted(tasks, key=lambda t: t.created_at)

    def requeue_assigned_to(self, agent_id: str) -> list[Task]:
        requeued = []
        with self._lock:
            for task in self._tasks.values():
                if task.assigned_agent_id == agent_id and task.status in (TaskStatus.ASSIGNED, TaskStatus.RUNNING):
                    task.status = TaskStatus.QUEUED
                    task.assigned_agent_id = None
                    task.started_at = None
                    requeued.append(task)
        return requeued

    def load(self, tasks: list[Task]):
        with self._lock:
            self._tasks = {task.task_id: task for task in tasks}
