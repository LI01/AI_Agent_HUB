from datetime import datetime
from threading import RLock
from typing import Optional

from .models import Agent, AgentStatus, MachineInfo


class AgentRegistry:
    def __init__(self):
        self._agents: dict[str, Agent] = {}
        self._lock = RLock()

    def register(
        self,
        agent_id: str,
        capabilities: list,
        machine_info: Optional[MachineInfo] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
        owner: Optional[str] = None,
        status: AgentStatus = AgentStatus.IDLE,
        created_at: Optional[datetime] = None,
    ) -> Agent:
        # Phase 2.2: normalize capabilities once before constructing the Agent
        # so all internal lookups see Capability instances. Lazy import to
        # avoid any circular-import risk.
        from hub.capabilities import normalize_capabilities
        normalized = normalize_capabilities(capabilities)
        with self._lock:
            existing = self._agents.get(agent_id)
            agent = Agent(
                agent_id=agent_id,
                name=name or (existing.name if existing else None) or agent_id,
                description=description if description is not None else (existing.description if existing else None),
                owner=owner if owner is not None else (existing.owner if existing else None),
                capabilities=normalized,
                status=status,
                machine_info=machine_info or (existing.machine_info if existing else MachineInfo()),
                created_at=created_at or (existing.created_at if existing else datetime.now()),
                last_heartbeat=datetime.now(),
            )
            self._agents[agent_id] = agent
            return agent

    def heartbeat(self, agent_id: str, status: AgentStatus) -> bool:
        with self._lock:
            if agent_id not in self._agents:
                return False
            self._agents[agent_id].status = status
            self._agents[agent_id].last_heartbeat = datetime.now()
            return True

    def get(self, agent_id: str) -> Optional[Agent]:
        with self._lock:
            return self._agents.get(agent_id)

    def list_all(self) -> list[Agent]:
        with self._lock:
            return list(self._agents.values())

    def find_available(self, task_type: str, min_version: int = 1) -> Optional[Agent]:
        """Pick the first IDLE agent advertising (task_type, version >= min_version).

        Tiebreak: registry insertion order. Empty-capabilities sentinel preserved
        (wildcard agent). Phase 2.2 §3.2.
        """
        from hub.capabilities import _agent_can_handle
        from .models import Task, TaskStatus

        # Build a minimal Task-like instance for the capability check.
        probe = Task(
            task_id="__probe__",
            submitted_by="__probe__",
            task_type=task_type or "",
            min_version=min_version or 1,
            status=TaskStatus.QUEUED,
        )
        with self._lock:
            for agent in self._agents.values():
                if agent.status != AgentStatus.IDLE:
                    continue
                if not agent.capabilities:
                    return agent  # wildcard
                if not task_type:
                    # No task_type → fall back to any idle agent (parity with v1).
                    return agent
                if _agent_can_handle(agent, probe):
                    return agent
            return None

    def unregister(self, agent_id: str) -> bool:
        with self._lock:
            if agent_id in self._agents:
                del self._agents[agent_id]
                return True
            return False

    def load(self, agents: list[Agent]):
        with self._lock:
            self._agents = {agent.agent_id: agent for agent in agents}
