"""Scripted coder agent. Writes hardcoded but-correct fib.py / test_fib.py.

Env vars:
  E2E_CODER_DELAY        seconds to sleep mid-task (E2E-3 kill window)
  E2E_CODER_CAPABILITIES comma-list of capabilities to register (default "code")
                         — E2E-5 flips this to "code,review" on restart
  E2E_CODER_BROKEN       if set/truthy, write a deliberately wrong fibonacci
                         on impl files (used by E2E-4 round 1 to force a
                         reviewer rejection; absence => correct impl)
"""
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_sdk import AgentHub  # noqa: E402

HUB, KEY = sys.argv[1], sys.argv[2]
PYTHON = sys.executable


FIB_SRC = '''\
def fibonacci(n):
    """Return the n-th Fibonacci number (0-indexed: fib(0)=0, fib(1)=1)."""
    if n < 0:
        raise ValueError("n must be non-negative")
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a
'''

FIB_BROKEN_SRC = '''\
def fibonacci(n):
    """Deliberately broken: off-by-one + skips negative-input check."""
    a, b = 1, 1
    for _ in range(n):
        a, b = b, a + b
    return a
'''

TEST_SRC = '''\
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from fib import fibonacci


def test_base_cases():
    assert fibonacci(0) == 0
    assert fibonacci(1) == 1


def test_small_values():
    assert [fibonacci(i) for i in range(7)] == [0, 1, 1, 2, 3, 5, 8]


def test_negative_raises():
    import pytest
    with pytest.raises(ValueError):
        fibonacci(-1)
'''


def _truthy(val: str | None) -> bool:
    return bool(val) and val.lower() not in ("0", "false", "no", "")


def code(task: dict) -> dict:
    payload = task.get("payload", {})
    goal = payload.get("goal", "")
    file_path = payload["file"]
    delay = float(os.environ.get("E2E_CODER_DELAY", "0") or 0)
    if delay > 0:
        time.sleep(delay)
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    # Decide by filename, not goal: the planner's "tests for ..." goal also
    # contains "test" so a goal-string check misroutes the implementation file.
    is_test = os.path.basename(file_path).startswith("test_")
    # E2E-4: payload.broken=true forces a wrong impl on this round (per-task
    # override); fallback to global E2E_CODER_BROKEN for whole-process mode.
    broken = bool(payload.get("broken")) or _truthy(os.environ.get("E2E_CODER_BROKEN"))
    if is_test:
        body = TEST_SRC
    elif broken:
        body = FIB_BROKEN_SRC
    else:
        body = FIB_SRC
    Path(file_path).write_text(body)
    return {"path": file_path, "wrote": True, "bytes": len(body), "broken": (broken and not is_test)}


def review(task: dict) -> dict:
    """E2E-5 only: when the coder is also reviewing, it runs pytest itself.
    Mirrors reviewer.py so a re-registered ['code','review'] coder can drain
    the queued review task without a real reviewer process."""
    payload = task.get("payload", {})
    path = payload["path"]
    proc = subprocess.run(
        [PYTHON, "-m", "pytest", path, "-q"],
        capture_output=True, text=True, timeout=60,
    )
    approved = proc.returncode == 0
    issues = [] if approved else (proc.stdout + proc.stderr).strip().splitlines()[-40:]
    return {"approved": approved, "exit_code": proc.returncode, "path": path,
            "issues": issues, "reviewed_by": "coder"}


def dispatch(task: dict) -> dict:
    ttype = task.get("task_type")
    if ttype == "review":
        return review(task)
    return code(task)


if __name__ == "__main__":
    caps_env = os.environ.get("E2E_CODER_CAPABILITIES", "code")
    capabilities = [c.strip() for c in caps_env.split(",") if c.strip()]
    AgentHub(HUB, "coder", capabilities, auth_token=KEY, task_handler=dispatch).start()
