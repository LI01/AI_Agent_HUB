"""E2E-7 — concurrent pipelines.

Run E2E-2 three times in parallel against the SAME hub. Each pipeline mints
its own `pipeline_id` and writes to its own `scratch/pipelines/{pid}/fib/`.

Pass criteria:
  - All N pipelines reach `passed=true`.
  - Per-pipeline pytest passes (asserted inside drive_pipeline).
  - No task is delivered to the wrong scratch dir: each pipeline's transcript
    only references its own pipeline_id, and each scratch dir holds only
    files written for that pipeline.
  - No deadlock: the whole batch finishes within DEADLINE_S.

Implementation: one shared harness (profile="full"), N driver threads each
calling drive_pipeline(handle, pipeline_id=...). Threads share planner/coder/
reviewer agents — the hub serializes dispatch.
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.e2e.harness import hub_with_agents  # noqa: E402
from scripts.e2e.run_pipeline import drive_pipeline  # noqa: E402

DEFAULT_N = 3
DEADLINE_S = 120


def run(n: int = DEFAULT_N) -> dict:
    summary: dict = {"scenario": "E2E-7", "n": n, "passed": False,
                     "pipelines": []}
    with hub_with_agents(profile="full") as h:
        results: dict[str, dict] = {}
        errors: dict[str, str] = {}
        threads: list[threading.Thread] = []

        pipeline_ids = [uuid.uuid4().hex for _ in range(n)]

        def runner(pid: str):
            try:
                results[pid] = drive_pipeline(h, pipeline_id=pid)
            except Exception as exc:  # noqa: BLE001
                errors[pid] = repr(exc)

        t_start = time.time()
        for pid in pipeline_ids:
            t = threading.Thread(target=runner, args=(pid,), name=f"drv-{pid[:6]}")
            t.start()
            threads.append(t)

        for t in threads:
            t.join(timeout=DEADLINE_S)

        elapsed = time.time() - t_start
        summary["elapsed_s"] = round(elapsed, 2)

        any_alive = any(t.is_alive() for t in threads)
        if any_alive:
            summary["failure"] = (
                f"deadlock: {sum(t.is_alive() for t in threads)} of {n} "
                f"driver threads still running after {DEADLINE_S}s"
            )
            return summary

        if errors:
            summary["failure"] = f"thread exceptions: {errors}"
            return summary

        # Per-pipeline assertions.
        all_pass = True
        for pid in pipeline_ids:
            r = results.get(pid, {})
            row = {"pipeline_id": pid, "passed": r.get("passed"),
                   "pytest_exit": r.get("pytest_exit_code"),
                   "task_count": len(r.get("transcript") or []),
                   "scratch_path": r.get("scratch_path")}
            if not r.get("passed"):
                row["failure"] = r.get("failure")
                all_pass = False
            summary["pipelines"].append(row)

        if not all_pass:
            summary["failure"] = "one or more pipelines failed"
            return summary

        # Cross-pipeline isolation check: every task in pipeline P's
        # transcript must reference only P's scratch_path / pipeline_id, and
        # each scratch dir must only hold files for its own pipeline.
        for pid, r in results.items():
            scratch = r["scratch_path"]
            for entry in r["transcript"]:
                result = entry.get("result") or {}
                if "path" in result:
                    p = result["path"]
                    if not p.startswith(scratch):
                        summary["failure"] = (
                            f"pipeline {pid}: child task path {p} not under "
                            f"this pipeline's scratch {scratch}"
                        )
                        return summary
            # Files actually exist where this pipeline expected them.
            fib = Path(scratch) / "fib.py"
            test = Path(scratch) / "test_fib.py"
            if not (fib.exists() and test.exists()):
                summary["failure"] = (
                    f"pipeline {pid}: missing fib.py or test_fib.py under {scratch}"
                )
                return summary

        # Confirm aggregate counts: 3 pipelines x (1 plan + 2 code + 1 review) = 12.
        total_tasks = sum(len(r["transcript"]) for r in results.values())
        summary["total_tasks"] = total_tasks
        summary["expected_tasks"] = n * 4
        if total_tasks != n * 4:
            summary["failure"] = (
                f"transcript task count {total_tasks} != expected {n * 4}"
            )
            return summary

        summary["passed"] = True
        return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", type=int, default=DEFAULT_N,
                        help="number of concurrent pipelines (default 3)")
    args = parser.parse_args()
    result = run(n=args.n)
    status = "PASS" if result["passed"] else "FAIL"
    print(f"\n=== E2E-7 {status} ===")
    for k, v in result.items():
        if k == "pipelines":
            print(f"  pipelines ({len(v)}):")
            for row in v:
                print(f"    - {row}")
        else:
            print(f"  {k}: {v}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
