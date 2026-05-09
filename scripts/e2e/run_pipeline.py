"""§14.6 driver. Implements E2E-2 (full pipeline) and E2E-3 (coder kill/restart).

Usage:
    python -m scripts.e2e.run_pipeline                # E2E-2
    python -m scripts.e2e.run_pipeline --kill-coder   # E2E-3
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.e2e.harness import hub_with_agents  # noqa: E402
from scripts.e2e.hub_http import submit_task, wait_task, get_task  # noqa: E402


GOAL = "add a fibonacci(n) function with tests"


def _arm_kill_coder(handle, watch_task_id_box: list) -> threading.Thread:
    """Background thread: kills+restarts coder as soon as the first code child
    is dispatched (status != 'queued'), then before it completes.

    The coder runs with E2E_CODER_DELAY=2.0 so there's a real ~2-second mid-task
    window. Driver kills with SIGKILL (no graceful WS close) so the hub sees a
    raw disconnect and exercises the FR-CON-6 requeue path. Restart happens via
    the harness; the requeued task should auto-dispatch on re-registration.
    """
    def runner():
        deadline = time.time() + 20
        first_id = None
        while time.time() < deadline and first_id is None:
            first_id = watch_task_id_box[0] if watch_task_id_box else None
            if first_id:
                break
            time.sleep(0.05)
        if not first_id:
            print("[kill-thread] no code task id appeared; aborting")
            return
        # wait until the task is assigned/running (not queued anymore)
        deadline = time.time() + 10
        while time.time() < deadline:
            body = get_task(handle.hub_url, handle.driver_key, first_id)
            status = body.get("status")
            if status in ("assigned", "running"):
                break
            if status in ("completed", "failed", "timeout"):
                print(f"[kill-thread] task already {status} before kill; aborting")
                return
            time.sleep(0.05)
        else:
            print("[kill-thread] task never reached assigned/running")
            return
        print(f"[kill-thread] coder mid-task (status={status}); SIGKILL")
        handle.kill_agent("coder", signal_term=False)
        # small delay then restart
        time.sleep(0.3)
        print("[kill-thread] restarting coder")
        try:
            handle.restart_agent("coder")
            print("[kill-thread] coder back online")
        except Exception as exc:
            print(f"[kill-thread] restart failed: {exc}")

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    return t


def drive_pipeline(h, pipeline_id: str | None = None,
                   kill_coder: bool = False) -> dict:
    """Drive one pipeline against an already-running harness handle `h`.

    Mints its own pipeline_id (or uses the supplied one) and per-pipeline
    scratch dir. Used by `run_pipeline()` (single-pipeline) and by E2E-7
    (concurrent pipelines on a shared hub).
    """
    transcript: list[dict] = []
    summary: dict = {"kill_coder": kill_coder, "passed": False}

    if pipeline_id is None:
        pipeline_id = uuid.uuid4().hex
    scratch_path = h.scratch_dir / "pipelines" / pipeline_id / "fib"
    scratch_path.mkdir(parents=True, exist_ok=True)
    scratch_str = str(scratch_path)
    summary.update(
        pipeline_id=pipeline_id,
        scratch_path=scratch_str,
        hub_url=h.hub_url,
    )

    # 1. plan
    plan_id = submit_task(
        h.hub_url, h.driver_key,
        task_type="plan",
        payload={
            "goal": GOAL,
            "scratch_path": scratch_str,
            "pipeline_id": pipeline_id,
        },
    )
    transcript.append({"role": "plan", "task_id": plan_id, "parent": None})
    print(f"[driver:{pipeline_id[:8]}] plan_id={plan_id}")

    # E2E-3: arm the kill-thread now; it watches a shared box for the first
    # code task id, then kills coder while that task is in flight.
    first_code_box: list = []
    kill_thread = _arm_kill_coder(h, first_code_box) if kill_coder else None

    plan_body = wait_task(h.hub_url, h.driver_key, plan_id, timeout=20)
    if plan_body["status"] != "completed":
        summary["failure"] = f"plan ended {plan_body['status']}: {plan_body.get('result')}"
        return summary
    plan_result = plan_body.get("result") or {}
    subtasks = plan_result.get("subtasks") or []
    if not subtasks:
        summary["failure"] = "planner returned no subtasks"
        return summary

    # 2. children
    for i, sub in enumerate(subtasks):
        sub_payload = {
            **sub.get("payload", {}),
            "pipeline_id": pipeline_id,
            "scratch_path": scratch_str,
        }
        child_id = submit_task(
            h.hub_url, h.driver_key,
            task_type=sub["task_type"],
            payload=sub_payload,
            parent_task_id=plan_id,
        )
        if i == 0 and kill_coder and sub["task_type"] == "code":
            first_code_box.append(child_id)
        transcript.append({
            "role": sub["task_type"],
            "task_id": child_id,
            "parent": plan_id,
            "index": i,
        })
        # generous timeout for the post-kill round AND for E2E-7 concurrency
        # (3 pipelines share one coder/reviewer => up to 3x serialization).
        timeout = 60 if kill_coder else 60
        body = wait_task(h.hub_url, h.driver_key, child_id, timeout=timeout)
        transcript[-1]["status"] = body["status"]
        transcript[-1]["result"] = body.get("result")
        print(f"[driver:{pipeline_id[:8]}] child[{i}] type={sub['task_type']} "
              f"id={child_id} status={body['status']}")
        if body["status"] != "completed":
            summary["failure"] = f"child {i} ({sub['task_type']}) status={body['status']}"
            summary["transcript"] = transcript
            return summary

    if kill_thread:
        kill_thread.join(timeout=5)

    # 3. assertions §14.7
    fib_py = scratch_path / "fib.py"
    test_py = scratch_path / "test_fib.py"
    if not fib_py.exists() or not test_py.exists():
        summary["failure"] = (
            f"missing files: fib.py={fib_py.exists()} test_fib.py={test_py.exists()}"
        )
        summary["transcript"] = transcript
        return summary

    # external pytest run on the scratch dir
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", scratch_str, "-q"],
        capture_output=True, text=True, timeout=60,
    )
    summary["pytest_exit_code"] = proc.returncode
    summary["pytest_tail"] = proc.stdout.strip().splitlines()[-5:]
    if proc.returncode != 0:
        summary["failure"] = "pytest <scratch_path> failed"
        summary["transcript"] = transcript
        return summary

    # reviewer assertion: last child is the review
    review_entry = transcript[-1]
    review_result = review_entry.get("result") or {}
    if not review_result.get("approved"):
        summary["failure"] = f"reviewer not approved: {review_result}"
        summary["transcript"] = transcript
        return summary

    # parent_task_id sanity (§14.7): assert hub stored parent_task_id on every child
    for entry in transcript[1:]:
        body = get_task(h.hub_url, h.driver_key, entry["task_id"])
        if body.get("parent_task_id") != plan_id:
            summary["failure"] = (
                f"child {entry['task_id']} parent_task_id="
                f"{body.get('parent_task_id')} expected {plan_id}"
            )
            summary["transcript"] = transcript
            return summary
        # E2E-7 isolation check: every child task's payload.pipeline_id must
        # match THIS pipeline's id (no cross-pipeline misrouting).
        body_payload = body.get("payload") or {}
        if body_payload.get("pipeline_id") != pipeline_id:
            summary["failure"] = (
                f"child {entry['task_id']} payload.pipeline_id="
                f"{body_payload.get('pipeline_id')} expected {pipeline_id}"
            )
            summary["transcript"] = transcript
            return summary

    summary["passed"] = True
    summary["transcript"] = transcript
    return summary


def run_pipeline(profile: str = "full", kill_coder: bool = False) -> dict:
    """Spin up a fresh hub+agents and drive a single pipeline. Wraps
    `drive_pipeline` for back-compat with the existing E2E-2/E2E-3 entry
    point."""
    agent_env = {"coder": {"E2E_CODER_DELAY": "2.0"}} if kill_coder else None
    with hub_with_agents(profile=profile, agent_env=agent_env) as h:
        result = drive_pipeline(h, kill_coder=kill_coder)
        result["profile"] = profile
        return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="full")
    parser.add_argument("--kill-coder", action="store_true",
                        help="E2E-3: kill+restart coder mid-pipeline")
    args = parser.parse_args()

    label = "E2E-3" if args.kill_coder else "E2E-2"
    result = run_pipeline(profile=args.profile, kill_coder=args.kill_coder)
    status = "PASS" if result["passed"] else "FAIL"
    print(f"\n=== {label} {status} ===")
    for k, v in result.items():
        if k == "transcript":
            print(f"  transcript ({len(v)} tasks):")
            for entry in v:
                print(f"    - {entry}")
        else:
            print(f"  {k}: {v}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
