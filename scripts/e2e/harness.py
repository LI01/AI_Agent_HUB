"""§14.5 harness: owns hub + agent process lifecycle.

`hub_with_agents(profile=...)` is the single entry point. It yields a
`HarnessHandle`. Cleanup terminates everything spawned.
"""
from __future__ import annotations

import contextlib
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHON = str(REPO_ROOT / ".venv" / "bin" / "python")

ROLE_SCRIPTS = {
    "planner": "planner.py",
    "coder": "coder.py",
    "reviewer": "reviewer.py",
}

PROFILES = {
    "full": ["planner", "coder", "reviewer"],
    "no-reviewer": ["planner", "coder"],
}


@dataclass
class HarnessHandle:
    hub_url: str
    driver_key: str
    agent_keys: Dict[str, str]
    scratch_dir: Path
    # internal handles for E2E-3 (kill/restart):
    _hub_proc: subprocess.Popen = field(default=None, repr=False)
    _agent_procs: Dict[str, subprocess.Popen] = field(default_factory=dict, repr=False)
    _agent_env: Dict[str, dict] = field(default_factory=dict, repr=False)

    def restart_agent(self, role: str, env_overlay: Optional[dict] = None) -> None:
        """Kill (if running) and respawn the named role's subprocess.

        `env_overlay` (optional) is merged on top of any existing per-role env
        registered at harness startup. E2E-5 uses this to flip the coder's
        declared capabilities mid-run without changing its agent_id or key.
        Persisted in `_agent_env` so subsequent restarts keep the overlay.
        """
        proc = self._agent_procs.get(role)
        if proc and proc.poll() is None:
            _kill(proc)
        if env_overlay:
            merged = dict(self._agent_env.get(role) or {})
            merged.update(env_overlay)
            self._agent_env[role] = merged
        self._agent_procs[role] = _spawn_agent(
            role, self.hub_url, self.agent_keys[role],
            extra_env=self._agent_env.get(role),
        )
        _wait_for_agents(self.hub_url, self.driver_key, [role], timeout=10)

    def kill_agent(self, role: str, signal_term: bool = True) -> None:
        proc = self._agent_procs.get(role)
        if not proc:
            return
        if signal_term:
            with contextlib.suppress(Exception):
                proc.terminate()
        else:
            with contextlib.suppress(Exception):
                proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _kill(proc: subprocess.Popen) -> None:
    if proc is None:
        return
    if proc.poll() is not None:
        return
    with contextlib.suppress(Exception):
        proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=2)


def _spawn_hub(port: int, db_path: str, admin_key: str, log_dir: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env["AGENT_HUB_DB_PATH"] = db_path
    env["AGENT_HUB_ADMIN_KEY"] = admin_key
    env["AGENT_HUB_TIMEOUT_SCAN_INTERVAL"] = "1"
    env["PYTHONPATH"] = str(REPO_ROOT)
    log_path = log_dir / "hub.log"
    fh = open(log_path, "w")
    proc = subprocess.Popen(
        [PYTHON, "-m", "uvicorn", "hub.main:app",
         "--port", str(port), "--host", "127.0.0.1", "--log-level", "warning"],
        env=env, cwd=str(REPO_ROOT),
        stdout=fh, stderr=subprocess.STDOUT,
    )
    proc._log_fh = fh  # keep the fd alive
    return proc


def _wait_for_health(port: int, timeout: float = 10) -> None:
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"{base}/health", timeout=0.5)
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"hub on :{port} did not become healthy in {timeout}s")


def _mint_key(hub: str, admin_key: str, name: str, **perms) -> str:
    body = {"name": name, **perms}
    r = httpx.post(
        f"{hub}/admin/keys",
        headers={"Authorization": f"Bearer {admin_key}"},
        json=body,
        timeout=5,
    )
    r.raise_for_status()
    return r.json()["api_key"]


def _spawn_agent(role: str, hub: str, key: str, extra_env: Optional[dict] = None) -> subprocess.Popen:
    script = Path(__file__).parent / ROLE_SCRIPTS[role]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.Popen(
        [PYTHON, str(script), hub, key],
        env=env, cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return proc


def _wait_for_agents(hub: str, view_key: str, roles: List[str], timeout: float = 10) -> None:
    """Poll /agents until each named role appears as `idle`."""
    deadline = time.time() + timeout
    needed = set(roles)
    while time.time() < deadline:
        try:
            r = httpx.get(
                f"{hub}/agents",
                headers={"Authorization": f"Bearer {view_key}"},
                timeout=1,
            )
            if r.status_code == 200:
                idle = {a["agent_id"] for a in r.json()
                        if a.get("status") == "idle" and a["agent_id"] in needed}
                if idle == needed:
                    return
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"agents {roles} not all idle within {timeout}s")


@contextlib.contextmanager
def hub_with_agents(profile: str = "full", agent_env: Optional[Dict[str, dict]] = None):
    """Yield HarnessHandle. Profile in {'full','no-reviewer'}.

    `agent_env` maps role -> extra env vars passed to that agent's subprocess
    (and re-applied on restart_agent). Used by E2E-3 to inject E2E_CODER_DELAY.
    """
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}")
    roles = PROFILES[profile]

    tmp_root = Path(tempfile.mkdtemp(prefix="e2e-hub-"))
    db_path = str(tmp_root / "agent_hub.db")
    log_dir = tmp_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    scratch_dir = tmp_root / "scratch"
    scratch_dir.mkdir(parents=True, exist_ok=True)

    admin_key = secrets.token_urlsafe(24)
    port = _free_port()
    hub_url = f"http://127.0.0.1:{port}"

    hub_proc = _spawn_hub(port, db_path, admin_key, log_dir)
    agent_procs: Dict[str, subprocess.Popen] = {}

    try:
        _wait_for_health(port, timeout=10)
        driver_key = _mint_key(
            hub_url, admin_key, "driver",
            can_assign_tasks=True, can_view_tasks=True, can_view_agents=True,
        )
        agent_keys: Dict[str, str] = {}
        for role in roles:
            agent_keys[role] = _mint_key(
                hub_url, admin_key, f"{role}-key",
                can_register=True, can_view_tasks=True, can_view_agents=True,
            )
        agent_env = agent_env or {}
        for role in roles:
            agent_procs[role] = _spawn_agent(
                role, hub_url, agent_keys[role],
                extra_env=agent_env.get(role),
            )

        _wait_for_agents(hub_url, driver_key, roles, timeout=10)

        handle = HarnessHandle(
            hub_url=hub_url,
            driver_key=driver_key,
            agent_keys=agent_keys,
            scratch_dir=scratch_dir,
            _hub_proc=hub_proc,
            _agent_procs=agent_procs,
            _agent_env=dict(agent_env),
        )
        yield handle
    finally:
        for proc in list(agent_procs.values()):
            _kill(proc)
        _kill(hub_proc)
        # leave tmp_root for inspection on failure; uncomment to clean:
        with contextlib.suppress(Exception):
            shutil.rmtree(tmp_root)


if __name__ == "__main__":
    # Standalone E2E-1: bring up + tear down + report.
    t0 = time.time()
    profile = sys.argv[1] if len(sys.argv) > 1 else "full"
    with hub_with_agents(profile=profile) as h:
        elapsed = time.time() - t0
        roles = list(h.agent_keys)
        r = httpx.get(
            f"{h.hub_url}/agents",
            headers={"Authorization": f"Bearer {h.driver_key}"},
            timeout=2,
        )
        statuses = {a["agent_id"]: a["status"] for a in r.json()}
        print(f"E2E-1 PASS: bringup={elapsed:.2f}s profile={profile} "
              f"hub={h.hub_url} agents={statuses}")
