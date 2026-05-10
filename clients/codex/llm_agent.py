"""Phase 2.3 — codex CLI worker adapter.

Spawns `codex exec` per task. Reports parsed text + char-heuristic budget
estimate back via the agent_sdk. Designed to be runnable as:

    python -m clients.codex.llm_agent --hub <url> --id <agent_id> \
        --key <api_key> --role designer --workdir /tmp/x

Design ref: design/phase2.3/design_v3.md §3.1, §3.3.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from agent_sdk import AgentHub, TaskFailed
from clients._files import (
    InvalidPayloadFiles, materialize_files, readback_files,
)
from clients.role_presets import BUILTIN_PRESETS, get_preset, load_user_overrides


CLI_NAME = "codex"


def compute_deadline(timeout: float) -> float:
    """Per design §3 / finding #12.

    For tiny timeouts (<= 5s), allocate 80% of the timeout (min 0.5s) to the
    CLI so we have a chance to surface an error before the hub times out.
    For larger timeouts, leave a 5s buffer.
    """
    if timeout <= 5:
        return max(0.5, timeout * 0.8)
    return timeout - 5


def _parse_codex_output(stdout: str) -> str:
    """Per design §3.3 verbatim — handle both startswith and embedded markers."""
    text = stdout.strip()
    for marker in ("[assistant]:", "assistant:"):
        if text.startswith(marker):
            return text[len(marker):].strip()
        nl_marker = "\n" + marker
        if nl_marker in text:
            return text.rsplit(nl_marker, 1)[1].strip()
    return text


def _extract_prompt(task: dict) -> str:
    """Pull the prompt-equivalent string out of a task dict."""
    payload = task.get("payload") or {}
    if isinstance(payload, dict):
        candidate = payload.get("task")
        if isinstance(candidate, str) and candidate:
            return candidate
    candidate = task.get("task")
    if isinstance(candidate, str) and candidate:
        return candidate
    # Fall back to dumping the payload so the CLI at least gets *something*.
    try:
        return json.dumps(payload)
    except (TypeError, ValueError):
        return str(payload)


def _run_cli_for_task(task, workdir, role_prompt, budget, role, hub) -> dict:
    """Run codex exec for one task and return the result dict.

    Raises TaskFailed for timeout, nonzero exit, etc., so the SDK emits
    `status="failed"` with our structured result payload.
    """
    base_prompt = _extract_prompt(task)
    full_prompt = (
        (role_prompt + "\n\n" + base_prompt) if role_prompt else base_prompt
    )

    timeout = task.get("timeout")
    try:
        timeout_f = float(timeout) if timeout is not None else 300.0
    except (TypeError, ValueError):
        timeout_f = 300.0
    deadline = compute_deadline(timeout_f)

    # Phase 2.4 finding #5: explicit key-presence + is-not-None gate so
    # malformed shapes hit validate_files instead of silently skipping.
    payload = task.get("payload") or {}
    if isinstance(payload, dict) and "files" in payload \
            and payload["files"] is not None:
        try:
            materialize_files(workdir, payload["files"])
        except InvalidPayloadFiles as e:
            error = (
                "payload_too_large"
                if e.reason in {"file_too_large", "total_too_large"}
                else "invalid_payload_files"
            )
            raise TaskFailed({
                "error": error,
                "reason": e.reason,
                "detail": e.detail,
                "cli": CLI_NAME,
                "role": role,
            })

    cmd = [
        "codex", "exec",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "-C", str(workdir),
        full_prompt,
    ]

    t0 = time.monotonic()
    try:
        cp = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=deadline,
            cwd=str(workdir),
        )
    except subprocess.TimeoutExpired:
        raise TaskFailed({
            "error": "cli_timeout",
            "elapsed_s": deadline,
            "cli": CLI_NAME,
            "role": role,
        })
    elapsed = round(time.monotonic() - t0, 3)

    if cp.returncode != 0:
        raise TaskFailed({
            "error": "cli_nonzero_exit",
            "exit_code": cp.returncode,
            "stderr": (cp.stderr or "")[-2000:],
            "cli": CLI_NAME,
            "role": role,
        })

    parsed = _parse_codex_output(cp.stdout or "")
    result = {
        "text": parsed,
        "raw": cp.stdout or "",
        "cli": CLI_NAME,
        "role": role,
        "elapsed_s": elapsed,
    }

    # Phase 2.4: readback after success, before budget tracking.
    if isinstance(payload, dict) and "expect_files_back" in payload \
            and payload["expect_files_back"] is not None:
        try:
            files_out, missing, truncated = readback_files(
                workdir, payload["expect_files_back"],
            )
        except InvalidPayloadFiles as e:
            # Only invalid_expect_files_back can come from readback.
            raise TaskFailed({
                "error": "invalid_payload_files",
                "reason": e.reason,
                "detail": e.detail,
                "cli": CLI_NAME,
                "role": role,
            })
        if files_out:
            result["files"] = files_out
        if missing:
            result["files_missing"] = missing
        if truncated:
            result["files_truncated"] = truncated

    # Budget tracking — char heuristic, codex doesn't expose token usage natively.
    est_tokens = len(parsed) // 4
    if budget and est_tokens > budget:
        warn = {
            "reason": "budget_exceeded_estimate",
            "estimated_tokens": est_tokens,
            "budget": budget,
        }
        result["payload_schema_warn"] = warn
        try:
            hub.send_activity_log(
                task.get("task_id"),
                "payload_schema_warn",
                warn,
            )
        except Exception:
            # Advisory only — never fail the task because logging failed.
            pass
    return result


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="clients.codex.llm_agent",
        description="Codex CLI adapter — bridges `codex exec` into the Agent Hub.",
    )
    p.add_argument("--hub", required=True, help="Hub URL (e.g. http://localhost:8080)")
    p.add_argument("--id", required=True, help="Agent ID to register as")
    p.add_argument(
        "--key",
        default=None,
        help=(
            "API key for the hub. If omitted, falls back to "
            "the AGENT_HUB_API_KEY env var (preferred — avoids argv leakage)."
        ),
    )
    p.add_argument(
        "--role",
        required=True,
        help="Role name from clients.role_presets (e.g. designer, coder)",
    )
    p.add_argument("--workdir", required=True, help="Working directory for the CLI")
    p.add_argument(
        "--description",
        default=None,
        help="Optional description string sent at registration",
    )
    p.add_argument(
        "--max-budget-tokens",
        type=int,
        default=None,
        help="Override the role's default token budget",
    )
    p.add_argument(
        "--capabilities",
        default=None,
        help=(
            "Comma-separated capability names; overrides the role preset. "
            "Used with --role custom (design §3.0, §10(f))."
        ),
    )
    p.add_argument(
        "--system-prompt-extra",
        default=None,
        help=(
            "Extra system prompt text appended to the role prompt; for "
            "--role custom this acts as the role's system prompt (§3.0)."
        ),
    )
    return p


def main(argv: list[str] | None = None) -> None:
    args = _build_argparser().parse_args(argv)

    api_key = args.key or os.environ.get("AGENT_HUB_API_KEY")
    if not api_key:
        print(
            "[clients.codex.llm_agent] no API key: pass --key or set "
            "AGENT_HUB_API_KEY in the env",
            file=sys.stderr,
        )
        raise SystemExit(2)

    role_prompts, role_budgets = load_user_overrides()
    role_for_preset = args.role if args.role in BUILTIN_PRESETS else "generic"
    preset = get_preset(
        role_for_preset, role_prompts=role_prompts, role_budgets=role_budgets,
    )
    if args.capabilities:
        capabilities = [
            c.strip() for c in args.capabilities.split(",") if c.strip()
        ]
    else:
        capabilities = preset["capabilities"]
    role_prompt = preset["prompt"]
    if args.system_prompt_extra:
        role_prompt = (role_prompt or "") + "\n\n" + args.system_prompt_extra
    budget = (
        args.max_budget_tokens
        if args.max_budget_tokens is not None
        else preset["budget"]
    )

    workdir = Path(args.workdir).expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    description = args.description
    if description is None:
        description = (
            f"cli={CLI_NAME};role={args.role};pipeline={workdir.parent.name}"
        )

    agent = AgentHub(
        hub_url=args.hub,
        agent_id=args.id,
        capabilities=capabilities,
        auth_token=api_key,
        description=description,
        unregister_on_stop=True,
    )

    @agent.task_handler
    def task_handler(task):
        return _run_cli_for_task(
            task,
            workdir,
            role_prompt=role_prompt,
            budget=budget,
            role=args.role,
            hub=agent,
        )

    print(
        f"[clients.codex.llm_agent] starting agent_id={args.id} "
        f"role={args.role} workdir={workdir}",
        file=sys.stderr,
    )
    agent.start()


if __name__ == "__main__":
    main()
