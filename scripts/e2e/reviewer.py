"""Scripted reviewer agent. Runs `pytest <path> -q` and reports approved/issues."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_sdk import AgentHub  # noqa: E402

HUB, KEY = sys.argv[1], sys.argv[2]
PYTHON = sys.executable


def review(task: dict) -> dict:
    payload = task.get("payload", {})
    path = payload["path"]
    proc = subprocess.run(
        [PYTHON, "-m", "pytest", path, "-q"],
        capture_output=True, text=True, timeout=60,
    )
    approved = proc.returncode == 0
    issues = [] if approved else (proc.stdout + proc.stderr).strip().splitlines()[-40:]
    return {
        "approved": approved,
        "exit_code": proc.returncode,
        "path": path,
        "issues": issues,
    }


if __name__ == "__main__":
    AgentHub(HUB, "reviewer", ["review"], auth_token=KEY, task_handler=review).start()
