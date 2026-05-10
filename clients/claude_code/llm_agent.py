"""Phase 2.3 — Claude Code LLM-CLI adapter.

Subprocess-driven worker that connects to the agent hub via the SDK and shells
out to `claude -p --bare --output-format json` for each task. See
`design/phase2.3/design_v3.md` §3.0 / §3.2 / §3.4 for the spec.

Module form (python -m clients.claude_code.llm_agent) is required because
Python module names cannot contain hyphens; the legacy hyphenated directory
`clients/claude-code/` is left untouched.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from agent_sdk import AgentHub, TaskFailed
from clients.role_presets import (
    BUILTIN_PRESETS,
    get_preset,
    load_user_overrides,
)


CLI_NAME = "claude"


_PAYLOAD_SCHEMA_V1: dict[str, Any] = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string"},
        "max_budget_tokens": {"type": "integer"},
        "system_prompt_extra": {"type": "string"},
    },
    "required": ["prompt"],
    "additionalProperties": True,
}


# ---------------------------------------------------------------------------
# Small helpers (kept local per design §3.2: "duplicate-and-keep is fine for v1")
# ---------------------------------------------------------------------------


def compute_deadline(timeout: float) -> float:
    """Return the subprocess timeout in seconds for a task with `timeout` budget.

    Per design §3.2 (finding #12, verbatim): give the CLI a small headroom so
    the SDK can still emit a `failed` result before the hub itself times out.
    For very small `timeout` values, fall back to 80% with a 0.5s floor.
    """
    if timeout <= 5:
        return max(0.5, timeout * 0.8)
    return timeout - 5


def _build_claude_cmd(
    workdir: Path,
    role_prompt: Optional[str],
    prompt: str,
) -> list[str]:
    """Build the argv for the claude CLI invocation.

    Default: `claude -p --output-format json --add-dir <workdir>
                     [--append-system-prompt <role_prompt>] <prompt>`.
    Set `AGENT_HUB_CLAUDE_BARE=1` to also pass `--bare` for hermetic runs
    (skips hooks, auto-memory, CLAUDE.md auto-discovery, and **keychain
    reads** — requires `ANTHROPIC_API_KEY` or `apiKeyHelper`).
    `--append-system-prompt` is omitted when `role_prompt` is None.
    """
    cmd: list[str] = ["claude", "-p"]
    if os.environ.get("AGENT_HUB_CLAUDE_BARE") == "1":
        cmd.append("--bare")
    cmd += ["--output-format", "json", "--add-dir", str(workdir)]
    if role_prompt is not None:
        cmd += ["--append-system-prompt", role_prompt]
    cmd.append(prompt)
    return cmd


def _parse_claude_response(parsed: dict) -> tuple[str, int]:
    """Extract `(text, usage_total_tokens)` from a parsed claude JSON payload.

    Text: `parsed["response"]` then `parsed["result"]` (claude's
    `--output-format json` uses `result`; `response` is checked first for
    forward-compat).
    Usage: try `usage.total_tokens`, else `usage.input_tokens +
    usage.output_tokens`, else 0.
    """
    text = parsed.get("response")
    if text is None:
        text = parsed.get("result", "")
    if text is None:
        text = ""

    usage = parsed.get("usage") or {}
    total = 0
    if isinstance(usage, dict):
        if isinstance(usage.get("total_tokens"), int):
            total = usage["total_tokens"]
        else:
            inp = usage.get("input_tokens") or 0
            out = usage.get("output_tokens") or 0
            try:
                total = int(inp) + int(out)
            except (TypeError, ValueError):
                total = 0
    return text, total


# ---------------------------------------------------------------------------
# CLI argument plumbing
# ---------------------------------------------------------------------------


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m clients.claude_code.llm_agent",
        description="Claude Code LLM-CLI worker adapter.",
    )
    p.add_argument("--hub", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--role", default="generic")
    p.add_argument("--workdir", required=True)
    p.add_argument("--max-budget-tokens", type=int, default=None)
    p.add_argument("--capabilities", default=None)
    p.add_argument("--system-prompt-extra", default=None)
    return p


def _resolve_capabilities(args: argparse.Namespace) -> list[dict]:
    """Custom capability list takes precedence; else use role presets."""
    if args.capabilities:
        names = [c.strip() for c in args.capabilities.split(",") if c.strip()]
        return [
            {"name": n, "version": 1, "payload_schema": _PAYLOAD_SCHEMA_V1}
            for n in names
        ]
    role = args.role if args.role in BUILTIN_PRESETS else "generic"
    preset = get_preset(role)
    return [
        {"name": n, "version": 1, "payload_schema": _PAYLOAD_SCHEMA_V1}
        for n in preset["capabilities"]
    ]


# ---------------------------------------------------------------------------
# Per-task runner
# ---------------------------------------------------------------------------


def _run_cli_for_task(
    task: dict,
    workdir: Path,
    role_prompt: Optional[str],
    budget: Optional[int],
    role: str,
    hub: AgentHub,
) -> dict:
    """Shell out to `claude` for a single task; return the result dict.

    Raises `TaskFailed` for timeout, non-zero exit, and unparseable JSON so
    the SDK emits a `status="failed"` result frame to the hub.
    """
    payload = task.get("payload") or {}
    prompt = payload.get("prompt") or task.get("task") or ""
    timeout = int(task.get("timeout") or 300)
    deadline = compute_deadline(timeout)

    cmd = _build_claude_cmd(workdir, role_prompt, prompt)

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
            "cli": CLI_NAME,
            "role": role,
            "elapsed_s": deadline,
        })
    elapsed = round(time.monotonic() - t0, 2)

    if cp.returncode != 0:
        raise TaskFailed({
            "error": "cli_nonzero_exit",
            "exit_code": cp.returncode,
            "stderr": (cp.stderr or "")[-2000:],
            "cli": CLI_NAME,
            "role": role,
        })

    try:
        parsed = json.loads(cp.stdout)
    except (json.JSONDecodeError, ValueError):
        raise TaskFailed({
            "error": "cli_unparseable_json",
            "stdout_tail": (cp.stdout or "")[-2000:],
            "cli": CLI_NAME,
            "role": role,
        })

    text, usage_total = _parse_claude_response(parsed)
    usage = parsed.get("usage") if isinstance(parsed.get("usage"), dict) else {}

    result: dict[str, Any] = {
        "text": text,
        "raw": parsed,
        "usage": usage,
        "cli": CLI_NAME,
        "role": role,
        "elapsed_s": elapsed,
    }

    if budget and usage_total > budget:
        result["payload_schema_warn"] = (
            f"used tokens {usage_total} exceeded max_budget_tokens {budget}"
        )
        try:
            hub.send_activity_log(
                task.get("task_id"),
                "payload_schema_warn",
                {
                    "used_tokens": usage_total,
                    "budget": budget,
                    "cli": CLI_NAME,
                    "role": role,
                },
            )
        except Exception:
            # advisory only; never break the task on log failure
            pass

    return result


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def main() -> None:
    args = _build_argparser().parse_args()
    api_key = os.environ.get("AGENT_HUB_API_KEY")

    role_prompts, role_budgets = load_user_overrides()
    role_for_preset = args.role if args.role in BUILTIN_PRESETS else "generic"
    preset = get_preset(
        role_for_preset,
        role_prompts=role_prompts,
        role_budgets=role_budgets,
    )

    role_prompt = preset["prompt"]
    if args.system_prompt_extra:
        role_prompt = (role_prompt or "") + "\n\n" + args.system_prompt_extra

    budget = args.max_budget_tokens if args.max_budget_tokens else preset["budget"]

    workdir = Path(args.workdir).expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    hub = AgentHub(
        hub_url=args.hub,
        agent_id=args.id,
        capabilities=_resolve_capabilities(args),
        auth_token=api_key,
        unregister_on_stop=True,
        description=(
            f"cli={CLI_NAME};role={args.role};pipeline={workdir.parent.name}"
        ),
    )

    @hub.task_handler
    def handle(task: dict) -> dict:
        return _run_cli_for_task(task, workdir, role_prompt, budget, args.role, hub)

    def _shutdown(_signum, _frame):
        hub.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    hub.start()


if __name__ == "__main__":
    main()
