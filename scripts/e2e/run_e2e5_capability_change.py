"""E2E-5 — capability change on re-registration.

Bootstrap profile = `no-reviewer` (planner + coder). Coder starts with
capabilities=["code"]. Driver submits a `review` task — must stay `queued`
because no agent advertises `review`. Then the harness restarts the coder
process with `E2E_CODER_CAPABILITIES=code,review` (same agent_id, same key).
The queued review task should dispatch within 2s of reconnect.

Pass criteria:
  1. Review task is `queued` (no agent assigned) for at least ~1s before
     the capability change.
  2. After restart_agent(coder, env_overlay={...}), the task transitions
     to assigned/running and reaches completed in <=2s of reconnect.
  3. Result has `approved` (the coder.review handler) — proves the
     re-registered coder actually drained it, not the original planner.
"""
from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.e2e.harness import hub_with_agents  # noqa: E402
from scripts.e2e.hub_http import submit_task, get_task  # noqa: E402

GOAL = "add a fibonacci(n) function with tests"


def run() -> dict:
    summary: dict = {"scenario": "E2E-5", "passed": False}
    with hub_with_agents(profile="no-reviewer") as h:
        pipeline_id = uuid.uuid4().hex
        scratch = h.scratch_dir / "pipelines" / pipeline_id / "fib"
        scratch.mkdir(parents=True, exist_ok=True)
        scratch_str = str(scratch)
        summary.update(pipeline_id=pipeline_id, hub_url=h.hub_url,
                       scratch_path=scratch_str)

        # Pre-populate impl + test so the review pytest has something to chew.
        # (Driver submits as code tasks so we exercise the planner/coder path.)
        plan_id = submit_task(
            h.hub_url, h.driver_key, task_type="plan",
            payload={"goal": GOAL, "scratch_path": scratch_str,
                     "pipeline_id": pipeline_id},
        )
        # wait for plan
        for _ in range(100):
            body = get_task(h.hub_url, h.driver_key, plan_id)
            if body["status"] == "completed":
                break
            time.sleep(0.1)
        plan_result = body.get("result") or {}
        for sub in plan_result.get("subtasks", []):
            if sub["task_type"] != "code":
                continue
            sid = submit_task(
                h.hub_url, h.driver_key, task_type="code",
                payload={**sub["payload"], "pipeline_id": pipeline_id,
                         "scratch_path": scratch_str},
                parent_task_id=plan_id,
            )
            for _ in range(150):
                if get_task(h.hub_url, h.driver_key, sid)["status"] == "completed":
                    break
                time.sleep(0.1)

        # Submit the review task. No reviewer agent exists; coder only has
        # ['code']. Must stay queued.
        review_id = submit_task(
            h.hub_url, h.driver_key, task_type="review",
            payload={"path": scratch_str, "pipeline_id": pipeline_id,
                     "scratch_path": scratch_str},
            parent_task_id=plan_id,
        )
        summary["review_task_id"] = review_id

        # Verify it sits queued for ~1s.
        time.sleep(1.0)
        body = get_task(h.hub_url, h.driver_key, review_id)
        summary["pre_restart_status"] = body["status"]
        if body["status"] != "queued":
            summary["failure"] = (
                f"review task expected queued before re-register; "
                f"got status={body['status']} agent_id={body.get('agent_id')}"
            )
            return summary

        # Capability change: restart coder with code+review.
        t_restart = time.time()
        h.restart_agent("coder", env_overlay={"E2E_CODER_CAPABILITIES": "code,review"})
        t_reconnect = time.time()
        summary["restart_seconds"] = round(t_reconnect - t_restart, 3)

        # Within 2s of reconnect, task should dispatch (assigned/running) and
        # likely complete (pytest is fast).
        deadline = t_reconnect + 2.0
        dispatched_at: float | None = None
        completed_at: float | None = None
        last_status = None
        while time.time() < deadline:
            body = get_task(h.hub_url, h.driver_key, review_id)
            last_status = body["status"]
            if dispatched_at is None and last_status in ("assigned", "running",
                                                         "completed", "failed"):
                dispatched_at = time.time()
            if last_status in ("completed", "failed", "timeout"):
                completed_at = time.time()
                break
            time.sleep(0.05)

        summary["post_reconnect_status"] = last_status
        if dispatched_at is None:
            summary["failure"] = (
                f"review task did not dispatch within 2s of reconnect "
                f"(status={last_status})"
            )
            return summary
        summary["dispatch_latency_s"] = round(dispatched_at - t_reconnect, 3)
        if completed_at:
            summary["completion_latency_s"] = round(completed_at - t_reconnect, 3)

        # Drain to completion if not yet done.
        for _ in range(200):
            body = get_task(h.hub_url, h.driver_key, review_id)
            if body["status"] in ("completed", "failed", "timeout"):
                break
            time.sleep(0.05)

        if body["status"] != "completed":
            summary["failure"] = f"review ended status={body['status']}"
            return summary
        result = body.get("result") or {}
        summary["review_result_keys"] = sorted(result.keys())
        summary["reviewed_by"] = result.get("reviewed_by")
        summary["assigned_agent_id"] = body.get("assigned_agent_id")
        # Also check the queued-task field for status==queued (which the hub
        # nulls out per main.py:163). Once completed, assigned_agent_id is set.
        if "approved" not in result:
            summary["failure"] = f"review result missing 'approved': {result}"
            return summary
        if body.get("assigned_agent_id") != "coder":
            summary["failure"] = (
                f"review handled by agent_id={body.get('agent_id')}, "
                f"expected 'coder'"
            )
            return summary

        summary["passed"] = True
        return summary


def main() -> int:
    result = run()
    status = "PASS" if result["passed"] else "FAIL"
    print(f"\n=== E2E-5 {status} ===")
    for k, v in result.items():
        print(f"  {k}: {v}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
