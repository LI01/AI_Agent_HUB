"""Tiny HTTP helpers for the §14 driver. See test_plan.md §14.4."""
import time

import httpx


def submit_task(hub: str, key: str, **fields) -> str:
    r = httpx.post(
        f"{hub}/tasks",
        headers={"Authorization": f"Bearer {key}"},
        json=fields,
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["task_id"]


def get_task(hub: str, key: str, tid: str) -> dict:
    r = httpx.get(
        f"{hub}/tasks/{tid}",
        headers={"Authorization": f"Bearer {key}"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def wait_task(hub: str, key: str, tid: str, timeout: float = 30) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = get_task(hub, key, tid)
        status = body.get("status")
        if status in ("completed", "failed", "timeout"):
            return body
        time.sleep(0.2)
    raise TimeoutError(f"task {tid} did not finish in {timeout}s")
