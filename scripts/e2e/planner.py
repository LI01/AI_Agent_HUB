"""Scripted planner agent. Returns a hardcoded fibonacci breakdown.

Two modes (gated by ``E2E_PLANNER_MODE`` env var):

* ``subtasks`` (default): return a list of subtask specs and let an external
  driver submit them (``run_pipeline.py``).
* ``delegation``: use ``submit_child(...)`` over the SDK to drive coder +
  reviewer ourselves; aggregate and return the result. Used by
  ``run_e2e_delegation.py``.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_sdk import AgentHub  # noqa: E402

HUB, KEY = sys.argv[1], sys.argv[2]

# Module-level handle so the task handler can reach the agent for
# delegation-mode submit_child calls. Set in __main__ before .start().
_active_agent: AgentHub | None = None


def _subtask_specs(goal: str, scratch: str) -> list[dict]:
    return [
        {"task_type": "code",
         "payload": {"goal": goal, "file": f"{scratch}/fib.py"}},
        {"task_type": "code",
         "payload": {"goal": "tests for " + goal, "file": f"{scratch}/test_fib.py"}},
        {"task_type": "review",
         "payload": {"path": scratch}},
    ]


def plan(task: dict) -> dict:
    payload = task.get("payload", {})
    goal = payload.get("goal", "")
    scratch = payload["scratch_path"]
    pipeline_id = payload.get("pipeline_id")

    mode = os.environ.get("E2E_PLANNER_MODE", "subtasks")
    if mode != "delegation":
        return {"subtasks": _subtask_specs(goal, scratch)}

    # Delegation mode: drive the pipeline ourselves via submit_child.
    if _active_agent is None:
        raise RuntimeError("planner delegation mode requires _active_agent set")

    children: list[dict] = []
    for spec in _subtask_specs(goal, scratch):
        target = "reviewer" if spec["task_type"] == "review" else "coder"
        sub_payload = dict(spec["payload"])
        if pipeline_id is not None:
            sub_payload["pipeline_id"] = pipeline_id
        sub_payload["scratch_path"] = scratch
        completed = _active_agent.submit_child(
            target_agent=target,
            task_type=spec["task_type"],
            payload=sub_payload,
            timeout=60,
            wait_timeout=120.0,
        )
        status = completed.get("status")
        children.append({
            "task_type": spec["task_type"],
            "target_agent": target,
            "child_task_id": completed.get("child_task_id"),
            "status": status,
            "result": completed.get("result"),
        })
        if status != "completed":
            return {
                "plan": "delegation_failed",
                "failed_at": spec["task_type"],
                "children": children,
            }

    return {
        "plan": "delegation_complete",
        "children": children,
        "child_task_ids": [c["child_task_id"] for c in children],
    }


if __name__ == "__main__":
    _active_agent = AgentHub(
        HUB, "planner", ["plan"], auth_token=KEY, task_handler=plan
    )
    _active_agent.start()
