"""F1+F2 combined E2E (per design v3 §5).

Spins up hub + planner + coder + reviewer with the planner running in
``E2E_PLANNER_MODE=delegation`` so it drives the children itself via
``submit_child(...)``. Driver submits ONE parent ``plan`` task and waits
for it to complete, then verifies that:

    1. Parent plan task is ``completed`` and top-level (``parent_task_id`` is None).
    2. The 3 children (fib.py code, test_fib.py code, reviewer review) are
       all ``completed`` with ``parent_task_id == plan.task_id``.
    3. ``GET /tasks?parent_task_id=<plan_id>`` returns exactly those 3.
    4. ``GET /tasks?top_level=true`` includes the plan task and excludes the
       children.
    5. SSE replay (subscriber started BEFORE the plan task) shows
       ``parent_task_id`` populated for child task events and ``null`` for
       the plan task event.

This script does NOT use ``scripts.e2e.harness.hub_with_agents`` directly
because the harness mints the planner's API key without
``can_assign_tasks`` — which the F1 ``submit_child`` permission gate
requires. We reuse the harness's private helpers (port allocation, hub
spawning, agent spawning) but mint the planner key with the additional
``can_assign_tasks`` flag so the planner-as-agent can delegate.

Usage:
    .venv/bin/python scripts/e2e/run_e2e_delegation.py
"""
from __future__ import annotations

import argparse
import contextlib
import json
import queue
import secrets
import shutil
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

# Reuse the harness's private spawn/wait helpers without modifying the
# module. Underscore-private in Python is convention only.
from scripts.e2e.harness import (  # noqa: E402
    _free_port,
    _kill,
    _mint_key,
    _spawn_agent,
    _spawn_hub,
    _wait_for_agents,
    _wait_for_health,
    HarnessHandle,
)
from scripts.e2e.hub_http import submit_task, wait_task, get_task  # noqa: E402


GOAL = "add a fibonacci(n) function with tests"
ROLES = ["planner", "coder", "reviewer"]


# ---------- bring-up (lifted from harness.hub_with_agents) ----------------

@contextlib.contextmanager
def _hub_with_delegation_planner():
    """Equivalent of ``hub_with_agents(profile='full')`` but mints the
    planner's key with ``can_assign_tasks=True`` so it can call the F1
    ``submit_child`` SDK helper. Planner subprocess is started with
    ``E2E_PLANNER_MODE=delegation`` env so its handler delegates instead
    of returning subtask specs.
    """
    tmp_root = Path(tempfile.mkdtemp(prefix="e2e-deleg-"))
    db_path = str(tmp_root / "agent_hub.db")
    log_dir = tmp_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    scratch_dir = tmp_root / "scratch"
    scratch_dir.mkdir(parents=True, exist_ok=True)

    admin_key = secrets.token_urlsafe(24)
    port = _free_port()
    hub_url = f"http://127.0.0.1:{port}"

    hub_proc = _spawn_hub(port, db_path, admin_key, log_dir)
    agent_procs: dict = {}

    try:
        _wait_for_health(port, timeout=10)
        driver_key = _mint_key(
            hub_url, admin_key, "driver",
            can_assign_tasks=True, can_view_tasks=True, can_view_agents=True,
        )
        agent_keys: dict = {}
        for role in ROLES:
            extra: dict = {}
            if role == "planner":
                # Planner needs can_assign_tasks=True (F1 §2.7) so submit_child
                # passes the access_control.can_assign_task check. Empty
                # allowed_agents == "any target".
                extra["can_assign_tasks"] = True
            agent_keys[role] = _mint_key(
                hub_url, admin_key, f"{role}-key",
                can_register=True, can_view_tasks=True, can_view_agents=True,
                **extra,
            )

        agent_env: dict = {"planner": {"E2E_PLANNER_MODE": "delegation"}}
        for role in ROLES:
            agent_procs[role] = _spawn_agent(
                role, hub_url, agent_keys[role],
                extra_env=agent_env.get(role),
            )

        _wait_for_agents(hub_url, driver_key, ROLES, timeout=10)

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
        with contextlib.suppress(Exception):
            shutil.rmtree(tmp_root)


# ---------- HTTP + SSE helpers --------------------------------------------

def _list_tasks(hub: str, key: str, **params) -> list[dict]:
    r = httpx.get(
        f"{hub}/tasks",
        headers={"Authorization": f"Bearer {key}"},
        params=params,
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


class _SSEListener:
    """Background SSE consumer. Buffers received events into a thread-safe
    queue. Parses ``event: message\\ndata: <json>\\n\\n`` frames manually
    (sse_starlette emits exactly that shape, so a tiny parser is enough).
    """

    def __init__(self, hub: str, key: str):
        self.hub = hub
        self.key = key
        self.events: queue.Queue[dict] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # Wait until the "connected" frame so we know the subscription is
        # live before the caller submits work.
        if not self._ready.wait(timeout=5):
            raise RuntimeError("SSE listener did not become ready in 5s")

    def _run(self) -> None:
        try:
            with httpx.stream(
                "GET",
                f"{self.hub}/events",
                headers={"Authorization": f"Bearer {self.key}"},
                timeout=httpx.Timeout(connect=5, read=None, write=5, pool=5),
            ) as resp:
                resp.raise_for_status()
                data_buf: list[str] = []
                for line in resp.iter_lines():
                    if self._stop.is_set():
                        return
                    if line == "":
                        if data_buf:
                            payload = "\n".join(data_buf)
                            data_buf.clear()
                            try:
                                ev = json.loads(payload)
                            except json.JSONDecodeError:
                                continue
                            self.events.put(ev)
                            if ev.get("type") == "connected":
                                self._ready.set()
                        continue
                    if line.startswith(":"):  # comment / keep-alive
                        continue
                    if line.startswith("data:"):
                        data_buf.append(line[5:].lstrip())
        except Exception as exc:  # pragma: no cover — surfaced on stop()
            self.events.put({"type": "_listener_error", "error": str(exc)})

    def stop(self) -> None:
        self._stop.set()

    def drain(self) -> list[dict]:
        out: list[dict] = []
        while True:
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                return out


# ---------- driver --------------------------------------------------------

def run() -> dict:
    summary: dict = {"passed": False, "assertions": {}}

    with _hub_with_delegation_planner() as h:
        summary["hub_url"] = h.hub_url

        # 1. SSE listener BEFORE we submit anything.
        sse = _SSEListener(h.hub_url, h.driver_key)
        sse.start()

        # 2. Mint pipeline_id + scratch path.
        pipeline_id = uuid.uuid4().hex
        scratch_path = h.scratch_dir / "pipelines" / pipeline_id / "fib"
        scratch_path.mkdir(parents=True, exist_ok=True)
        scratch_str = str(scratch_path)
        summary.update(pipeline_id=pipeline_id, scratch_path=scratch_str)

        # 3. Submit one parent plan task. The planner (delegation mode)
        #    spawns coder + reviewer children and aggregates.
        plan_id = submit_task(
            h.hub_url, h.driver_key,
            task_type="plan",
            payload={
                "goal": GOAL,
                "scratch_path": scratch_str,
                "pipeline_id": pipeline_id,
            },
        )
        summary["plan_id"] = plan_id
        print(f"[driver] plan_id={plan_id} pipeline_id={pipeline_id[:8]}")

        # 4. Wait for parent. Planner makes 3 sequential submit_child calls
        #    (fib.py write, test_fib.py write, pytest run); 90s is plenty.
        plan_body = wait_task(h.hub_url, h.driver_key, plan_id, timeout=90)
        summary["plan_status"] = plan_body.get("status")
        summary["plan_result"] = plan_body.get("result")

        # Let SSE flush the trailing task_completed frame.
        time.sleep(0.5)
        sse.stop()
        events = sse.drain()
        summary["sse_event_count"] = len(events)

        # === Assertion 1: parent completed + top-level ===
        a1_ok = (
            plan_body.get("status") == "completed"
            and plan_body.get("parent_task_id") is None
        )
        summary["assertions"]["parent_completed_top_level"] = a1_ok
        if not a1_ok:
            summary["failure"] = (
                f"parent plan status={plan_body.get('status')} "
                f"parent_task_id={plan_body.get('parent_task_id')}"
            )
            return summary

        # Pull child ids out of the planner's aggregated result.
        plan_result = plan_body.get("result") or {}
        child_ids: list[str] = list(plan_result.get("child_task_ids") or [])
        summary["child_task_ids"] = child_ids
        if len(child_ids) != 3:
            summary["failure"] = (
                f"expected 3 child task ids from planner, got {len(child_ids)}"
            )
            return summary

        # === Assertion 2: each child completed + parent_task_id == plan_id ===
        children_bodies = [get_task(h.hub_url, h.driver_key, cid) for cid in child_ids]
        a2_failures = [
            (cb.get("task_id"), cb.get("status"), cb.get("parent_task_id"))
            for cb in children_bodies
            if cb.get("status") != "completed" or cb.get("parent_task_id") != plan_id
        ]
        a2_ok = not a2_failures
        summary["assertions"]["children_completed_with_parent"] = a2_ok
        if not a2_ok:
            summary["failure"] = f"children malformed: {a2_failures}"
            return summary

        # === Assertion 3: GET /tasks?parent_task_id=<plan_id> returns those 3 ===
        by_parent = _list_tasks(h.hub_url, h.driver_key, parent_task_id=plan_id)
        by_parent_ids = sorted(t["task_id"] for t in by_parent)
        a3_ok = by_parent_ids == sorted(child_ids)
        summary["assertions"]["parent_filter_returns_exactly_children"] = a3_ok
        if not a3_ok:
            summary["failure"] = (
                f"GET /tasks?parent_task_id={plan_id} returned "
                f"{by_parent_ids}, expected {sorted(child_ids)}"
            )
            return summary

        # === Assertion 4: GET /tasks?top_level=true includes plan, excludes children ===
        top_level = _list_tasks(h.hub_url, h.driver_key, top_level="true")
        top_level_ids = {t["task_id"] for t in top_level}
        a4_ok = (plan_id in top_level_ids) and not (set(child_ids) & top_level_ids)
        summary["assertions"]["top_level_filter_correct"] = a4_ok
        if not a4_ok:
            summary["failure"] = (
                f"top_level={top_level_ids}; plan_present={plan_id in top_level_ids}; "
                f"child_overlap={set(child_ids) & top_level_ids}"
            )
            return summary

        # === Assertion 5: SSE replay shows parent_task_id populated correctly ===
        plan_events = [
            e for e in events
            if e.get("type", "").startswith("task_")
            and (e.get("data") or {}).get("task_id") == plan_id
        ]
        child_events_by_id: dict[str, list[dict]] = {cid: [] for cid in child_ids}
        for e in events:
            if not e.get("type", "").startswith("task_"):
                continue
            tid = (e.get("data") or {}).get("task_id")
            if tid in child_events_by_id:
                child_events_by_id[tid].append(e)

        sse_failures: list[str] = []
        if not plan_events:
            sse_failures.append(f"no SSE task_* events for plan_id={plan_id}")
        else:
            for ev in plan_events:
                ptid = (ev.get("data") or {}).get("parent_task_id")
                if ptid is not None:
                    sse_failures.append(
                        f"plan event {ev.get('type')} had parent_task_id={ptid!r}, "
                        f"expected None"
                    )
        for cid, evs in child_events_by_id.items():
            if not evs:
                sse_failures.append(f"no SSE task_* events for child {cid}")
                continue
            for ev in evs:
                ptid = (ev.get("data") or {}).get("parent_task_id")
                if ptid != plan_id:
                    sse_failures.append(
                        f"child {cid} event {ev.get('type')} had "
                        f"parent_task_id={ptid!r}, expected {plan_id!r}"
                    )
        a5_ok = not sse_failures
        summary["assertions"]["sse_parent_task_id_populated"] = a5_ok
        summary["sse_failures"] = sse_failures
        if not a5_ok:
            summary["failure"] = "; ".join(sse_failures[:5])
            return summary

        summary["passed"] = True
        return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    result = run()
    status = "PASS" if result["passed"] else "FAIL"
    print(f"\n=== E2E-DELEGATION {status} ===")
    for k, v in result.items():
        if k == "assertions":
            print("  assertions:")
            for ak, av in v.items():
                print(f"    - {ak}: {av}")
        else:
            print(f"  {k}: {v}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
