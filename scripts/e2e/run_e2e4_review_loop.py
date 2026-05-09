"""E2E-4 — reviewer loop (max 3 rounds).

Round 1 the coder writes a deliberately broken fib.py (`payload.broken=true`),
so the reviewer's pytest fails and returns `approved=false, issues=[...]`. The
driver then re-submits a `code` task with `broken=false` and the issues attached
to payload, the coder rewrites the file, and the reviewer approves on round 2.

Pass: `approved=true` within <=3 rounds AND the impl-file content actually
differed between rounds (proves the loop is real, not a no-op).
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.e2e.harness import hub_with_agents  # noqa: E402
from scripts.e2e.hub_http import submit_task, wait_task  # noqa: E402

GOAL = "add a fibonacci(n) function with tests"
MAX_ROUNDS = 3


def run() -> dict:
    summary: dict = {"scenario": "E2E-4", "passed": False, "rounds": []}
    with hub_with_agents(profile="full") as h:
        pipeline_id = uuid.uuid4().hex
        scratch = h.scratch_dir / "pipelines" / pipeline_id / "fib"
        scratch.mkdir(parents=True, exist_ok=True)
        scratch_str = str(scratch)
        impl_file = f"{scratch_str}/fib.py"
        test_file = f"{scratch_str}/test_fib.py"

        summary.update(pipeline_id=pipeline_id, scratch_path=scratch_str,
                       hub_url=h.hub_url)

        # Plan once for traceability (gives us a parent_task_id).
        plan_id = submit_task(
            h.hub_url, h.driver_key, task_type="plan",
            payload={"goal": GOAL, "scratch_path": scratch_str,
                     "pipeline_id": pipeline_id},
        )
        wait_task(h.hub_url, h.driver_key, plan_id, timeout=20)

        # Always write the test file (correct) before the loop so reviewer
        # has something to run.
        test_id = submit_task(
            h.hub_url, h.driver_key, task_type="code",
            payload={"goal": "tests for " + GOAL, "file": test_file,
                     "pipeline_id": pipeline_id, "scratch_path": scratch_str},
            parent_task_id=plan_id,
        )
        wait_task(h.hub_url, h.driver_key, test_id, timeout=30)

        prev_impl_bytes: bytes | None = None
        last_issues: list = []
        approved = False

        for rnd in range(1, MAX_ROUNDS + 1):
            broken = (rnd == 1)  # round 1 broken, later rounds correct
            code_payload = {
                "goal": GOAL, "file": impl_file,
                "pipeline_id": pipeline_id, "scratch_path": scratch_str,
                "broken": broken, "round": rnd,
            }
            if last_issues:
                code_payload["prior_issues"] = last_issues[-10:]
            code_id = submit_task(
                h.hub_url, h.driver_key, task_type="code",
                payload=code_payload, parent_task_id=plan_id,
            )
            code_body = wait_task(h.hub_url, h.driver_key, code_id, timeout=30)
            impl_bytes = Path(impl_file).read_bytes()

            review_id = submit_task(
                h.hub_url, h.driver_key, task_type="review",
                payload={"path": scratch_str, "pipeline_id": pipeline_id,
                         "scratch_path": scratch_str, "round": rnd},
                parent_task_id=plan_id,
            )
            review_body = wait_task(h.hub_url, h.driver_key, review_id, timeout=60)
            result = review_body.get("result") or {}
            approved = bool(result.get("approved"))
            last_issues = list(result.get("issues") or [])

            round_rec = {
                "round": rnd,
                "code_task_id": code_id,
                "review_task_id": review_id,
                "broken_payload": broken,
                "impl_sha": hash(impl_bytes),
                "impl_bytes": len(impl_bytes),
                "approved": approved,
                "issue_count": len(last_issues),
            }
            summary["rounds"].append(round_rec)
            print(f"[E2E-4] round={rnd} broken={broken} "
                  f"impl_bytes={len(impl_bytes)} approved={approved} "
                  f"issues={len(last_issues)}")

            content_changed = (prev_impl_bytes is not None
                               and impl_bytes != prev_impl_bytes)
            if approved:
                # Sanity: round 1 must have failed AND content must differ
                # between the rejection round and the approval round, else
                # the loop is a fiction.
                if rnd == 1:
                    summary["failure"] = (
                        "round 1 approved — loop never exercised; "
                        "broken-impl mode did not produce a failure"
                    )
                    return summary
                if not content_changed:
                    summary["failure"] = (
                        f"approval at round {rnd} but impl content unchanged "
                        f"from prior round (sha={hash(impl_bytes)})"
                    )
                    return summary
                summary["passed"] = True
                summary["approved_round"] = rnd
                return summary
            prev_impl_bytes = impl_bytes

        summary["failure"] = (
            f"reviewer never approved within {MAX_ROUNDS} rounds; "
            f"last issues: {last_issues[:3]}"
        )
        return summary


def main() -> int:
    result = run()
    status = "PASS" if result["passed"] else "FAIL"
    print(f"\n=== E2E-4 {status} ===")
    for k, v in result.items():
        if k == "rounds":
            print(f"  rounds ({len(v)}):")
            for r in v:
                print(f"    - {r}")
        else:
            print(f"  {k}: {v}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
